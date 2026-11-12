"""Conservative inline-image (BI ... ID ... EI) converter.

Only handles page-level, 8-bit, DeviceRGB/DeviceGray/DeviceCMYK,
raw, FlateDecode (without predictors), or DCTDecode. Unsupported images
are left byte-for-byte unchanged. Replaces accepted inline image with
an Image XObject reference in the *same* graphics context, so adjacent
text and form widgets are not rasterized.
"""
from __future__ import annotations

import io
import math
import re
import zlib
from dataclasses import dataclass

import fitz
from PIL import Image

from optimizer import MAX_PIXELS

BI_RE = re.compile(rb'(?<!\S)BI(?=\s)')
ID_RE = re.compile(rb'(?<!\S)ID(?=\s)')
EI_RE = re.compile(rb'(?<!\S)EI(?=\s|$)')
FIELDS = {
    b'W': b'Width', b'H': b'Height', b'BPC': b'BitsPerComponent',
    b'CS': b'ColorSpace', b'F': b'Filter', b'DP': b'DecodeParms',
    b'D': b'Decode', b'IM': b'ImageMask', b'I': b'Interpolate',
}
COLORS = {b'/RGB': ('RGB', 3), b'/DeviceRGB': ('RGB', 3),
          b'/G': ('L', 1), b'/DeviceGray': ('L', 1),
          b'/CMYK': ('CMYK', 4), b'/DeviceCMYK': ('CMYK', 4)}
FILTERS = {b'/Fl': 'flate', b'/FlateDecode': 'flate',
           b'/DCT': 'jpeg', b'/DCTDecode': 'jpeg'}


@dataclass
class InlineBlock:
    start: int
    end: int
    payload: bytes
    width: int
    height: int
    mode: str
    encoding: str


def parse_block(data: bytes, start: int) -> InlineBlock | None:
    """Conservative parser: reject unknown/ambiguous BI dictionaries."""
    id_match = ID_RE.search(data, start + 2, min(len(data), start + 4096))
    if not id_match:
        return None
    hdr = data[start + 2:id_match.start()].strip()
    # Avoid rewriting inline images with complex dictionaries (arrays, named CS,
    # DecodeParms, image masks, alternate decode ranges, etc.).
    if any(c in hdr for c in (b'[', b']', b'<<', b'>>', b'(', b')')):
        return None
    tokens = hdr.split()
    if len(tokens) % 2 or not tokens:
        return None
    params = {}
    for key, value in zip(tokens[::2], tokens[1::2]):
        if not key.startswith(b'/') or not value:
            return None
        key = FIELDS.get(key[1:], key[1:])
        if key in params:
            return None
        params[key] = value
    if any(k not in (b'Width', b'Height', b'BitsPerComponent', b'ColorSpace', b'Filter') for k in params):
        return None
    try:
        width, height = int(params[b'Width']), int(params[b'Height'])
        bpc = int(params[b'BitsPerComponent'])
    except (KeyError, ValueError):
        return None
    if bpc != 8 or width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        return None
    color = COLORS.get(params.get(b'ColorSpace'))
    if color is None:
        return None
    mode, components = color
    value = params.get(b'Filter')
    encoding = 'raw' if value is None else FILTERS.get(value)
    if not encoding:
        return None
    # PDF ID consumes exactly one whitespace character (CRLF counts as one).
    pos = id_match.end()
    if data[pos:pos + 2] == b'\r\n':
        pos += 2
    elif data[pos:pos + 1] in (b' ', b'\t', b'\r', b'\n', b'\x0c', b'\x00'):
        pos += 1
    else:
        return None
    need = width * height * components
    if encoding == 'raw':
        end_data = pos + need
        if end_data >= len(data):
            return None
        payload = data[pos:end_data]
    elif encoding == 'flate':
        decompressor = zlib.decompressobj()
        try:
            raw = decompressor.decompress(data[pos:], need + 1)
            if len(raw) != need or not decompressor.eof:
                # Some Flate images carry predictors or have malformed streams;
                # skip rather than silently changing their appearance.
                return None
            end_data = len(data) - len(decompressor.unused_data)
            payload = raw
        except zlib.error:
            return None
    else:  # JPEG: decode to pixels, then re-encode as needed.
        marker = data.find(b'\xff\xd9', pos)
        if marker < 0:
            return None
        end_data = marker + 2
        payload = data[pos:end_data]
    # Ensure a legal EI delimiter, not a substring inside binary data.
    tail = data[end_data:end_data + 8]
    m = re.match(rb'\s+EI(?=\s|$)', tail)
    if not m:
        return None
    return InlineBlock(start, end_data + m.end(), payload, width, height, mode, encoding)


def jpeg_for_block(block: InlineBlock, width: int, height: int, quality: int) -> bytes | None:
    try:
        if block.encoding == 'jpeg':
            with Image.open(io.BytesIO(block.payload)) as loaded:
                im = loaded.copy()
        else:
            im = Image.frombytes(block.mode, (block.width, block.height), block.payload)
        if im.size != (block.width, block.height):
            return None
        if im.mode == 'CMYK':
            im = im.convert('RGB')
        elif im.mode not in ('L', 'RGB'):
            return None
        im.thumbnail((max(1, width), max(1, height)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, 'JPEG', quality=quality, optimize=True, progressive=False)
        return out.getvalue()
    except (OSError, ValueError, MemoryError):
        return None


def add_xobject(doc: fitz.Document, owner_xref: int, image: bytes, name: str) -> None:
    """Register a new JPEG image in the page's effective resource dictionary."""
    res_type, res_value = doc.xref_get_key(owner_xref, 'Resources')
    if res_type != 'xref':
        # A form may use an inline resource dictionary, or an inherited page
        # resource dictionary. Never rewrite unknown resource layouts.
        raise ValueError('Resources is not an indirect reference')
    res_xref = int(res_value.split()[0])
    xo_type, xo_val = doc.xref_get_key(res_xref, 'XObject')
    if xo_type == 'xref':
        target = int(xo_val.split()[0])
    elif xo_type == 'null' or xo_type == 'dict':
        target = res_xref
    else:
        raise ValueError('unsupported XObject dictionary')
    # Avoid overwriting any existing resource.
    existing_type, _ = doc.xref_get_key(target, ('XObject/' if target == res_xref else '') + name)
    if existing_type != 'null':
        raise ValueError('resource name already exists')
    xref = doc.get_new_xref()
    with Image.open(io.BytesIO(image)) as im:
        w, h = im.size
        cs = '/DeviceGray' if im.mode == 'L' else '/DeviceRGB'
    doc.update_object(xref, f'<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {cs} /BitsPerComponent 8 /Filter /DCTDecode >>')
    doc.update_stream(xref, image, compress=False)
    path = ('XObject/' if target == res_xref else '') + name
    doc.xref_set_key(target, path, f'{xref} 0 R')


def _candidate_streams(doc: fitz.Document, page: fitz.Page):
    """Visit page streams and *displayed* Form XObject streams once."""
    for xref in page.get_contents():
        yield xref, page.xref
    for item in page.get_xobjects():
        # get_xobjects includes nested and direct Form XObjects.
        xref = int(item[0])
        if xref > 0 and doc.xref_is_stream(xref):
            yield xref, xref


def compress_inline_images(doc: fitz.Document, dpi: int, quality: int) -> int:
    """Rewrite supported inline images in page / Form XObject streams.

    Unsupported dictionary encodings or resource layouts are left unmodified.
    """
    changed = 0
    visited = set()
    for page in doc:
        inline = [i for i in page.get_image_info(xrefs=True) if not i.get('xref')]
        if not inline:
            continue
        max_w = max((abs(fitz.Rect(i['bbox']).width) for i in inline), default=0)
        max_h = max((abs(fitz.Rect(i['bbox']).height) for i in inline), default=0)
        if max_w <= 0 or max_h <= 0:
            continue
        for stream_xref, owner_xref in _candidate_streams(doc, page):
            if stream_xref in visited:
                continue
            visited.add(stream_xref)
            raw_stream = doc.xref_stream(stream_xref)
            if not BI_RE.search(raw_stream):
                continue
            replacements = []
            cursor = 0
            while (found := BI_RE.search(raw_stream, cursor)) is not None:
                block = parse_block(raw_stream, found.start())
                if block is None:
                    cursor = found.end()
                    continue
                cursor = block.end
                target_w = min(block.width, max(1, math.ceil(max_w * dpi / 72)))
                target_h = min(block.height, max(1, math.ceil(max_h * dpi / 72)))
                encoded = jpeg_for_block(block, target_w, target_h, quality)
                if encoded is None or len(encoded) + 250 >= block.end - block.start:
                    continue
                name = f'PDFOptInline{stream_xref}_{len(replacements)}'
                try:
                    add_xobject(doc, owner_xref, encoded, name)
                except (ValueError, RuntimeError, KeyError):
                    continue
                replacements.append((block.start, block.end, f'/{name} Do'.encode('ascii')))
            if not replacements:
                continue
            updated = bytearray()
            pos = 0
            for start, end, data in replacements:
                updated.extend(raw_stream[pos:start])
                updated.extend(data)
                pos = end
            updated.extend(raw_stream[pos:])
            doc.update_stream(stream_xref, bytes(updated))
            changed += len(replacements)
    return changed


def diagnose_inline_images(doc: fitz.Document) -> dict:
    """Return only aggregate counts; never expose PDF content or images."""
    result = {'inline_blocks_found': 0, 'inline_blocks_parseable': 0,
              'inline_in_forms': 0, 'inline_unsupported': 0,
              'inline_unknown_location': 0}
    visited = set()
    for page in doc:
        for stream_xref, owner_xref in _candidate_streams(doc, page):
            if stream_xref in visited:
                continue
            visited.add(stream_xref)
            stream = doc.xref_stream(stream_xref)
            cursor = 0
            while (found := BI_RE.search(stream, cursor)) is not None:
                result['inline_blocks_found'] += 1
                if owner_xref != page.xref:
                    result['inline_in_forms'] += 1
                block = parse_block(stream, found.start())
                if block is None:
                    result['inline_unsupported'] += 1
                    cursor = found.end()
                else:
                    result['inline_blocks_parseable'] += 1
                    cursor = block.end
    found = result['inline_blocks_found']
    displayed = sum(1 for page in doc for i in page.get_image_info(xrefs=True) if not i.get('xref'))
    result['inline_unknown_location'] = max(0, displayed - found)
    return result
