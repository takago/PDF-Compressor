"""Privacy-preserving aggregate diagnostics for image origins.

No image bytes, strings, text, filenames, object contents, or stream data are
reported. The scan is best effort; findings are not proof of a display path.
"""
from __future__ import annotations

import fitz
from inline_images import BI_RE

MAX_STREAMS = 2000
MAX_STREAM_BYTES = 8_000_000
MAX_TOTAL_BYTES = 64_000_000


def diagnose_unlocated_images(doc: fitz.Document) -> dict[str, int | str]:
    page_contents: set[int] = set()
    form_xrefs: set[int] = set()
    for page in doc:
        page_contents.update(int(x) for x in page.get_contents())
        for item in page.get_xobjects():
            form_xrefs.add(int(item[0]))

    counts: dict[str, int | str] = {
        'scan_streams_checked': 0,
        'scan_page_content_hits': 0,
        'scan_form_hits': 0,
        'scan_other_stream_hits': 0,
        'scan_image_xobjects': 0,
        'scan_streams_skipped': 0,
        'scan_total_bytes': 0,
        'scan_limit_reached': 0,
    }
    total = 0
    for xref in range(1, doc.xref_length()):
        if counts['scan_streams_checked'] >= MAX_STREAMS or total >= MAX_TOTAL_BYTES:
            counts['scan_limit_reached'] = 1
            break
        try:
            if not doc.xref_is_stream(xref):
                continue
            # Count image objects independently of the xref=0 lookup.
            if doc.xref_get_key(xref, 'Subtype')[1] == '/Image':
                counts['scan_image_xobjects'] += 1
            raw_len = doc.xref_stream_raw(xref)
            # A compressed stream may be small on disk but huge decoded.
            # Reading raw content first gives a conservative upper bound only;
            # decoded size still has to be capped after decoding.
            if len(raw_len) > MAX_STREAM_BYTES:
                counts['scan_streams_skipped'] += 1
                continue
            data = doc.xref_stream(xref)
            if len(data) > MAX_STREAM_BYTES:
                counts['scan_streams_skipped'] += 1
                continue
            total += len(data)
            counts['scan_streams_checked'] += 1
            if BI_RE.search(data) is not None:
                if xref in page_contents:
                    counts['scan_page_content_hits'] += 1
                elif xref in form_xrefs:
                    counts['scan_form_hits'] += 1
                else:
                    counts['scan_other_stream_hits'] += 1
        except (RuntimeError, ValueError, OverflowError, MemoryError):
            counts['scan_streams_skipped'] += 1
    counts['scan_total_bytes'] = total
    if counts['scan_other_stream_hits']:
        category = 'other_stream_candidates'
    elif counts['scan_page_content_hits'] or counts['scan_form_hits']:
        category = 'known_content_candidates'
    elif counts['scan_image_xobjects']:
        category = 'image_resources_not_mapped'
    elif counts['scan_limit_reached'] or counts['scan_streams_skipped']:
        category = 'scan_incomplete'
    else:
        category = 'unresolved_no_bi_stream'
    counts['scan_category'] = category
    return counts
