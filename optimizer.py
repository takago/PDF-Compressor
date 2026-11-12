"""Compress only PDF image XObjects, preserving editable text and AcroForm fields."""
from __future__ import annotations

import io
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import fitz
from PIL import Image, ImageOps, UnidentifiedImageError

TARGET_BYTES = 1_000_000  # Decimal MB: safely below a 1 MB upload limit.
# A conservative first pass followed by progressively smaller images.
PRESETS = ((150, 85), (120, 80), (100, 75), (85, 68), (72, 62), (60, 55), (50, 48))
MAX_PIXELS = 45_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class UnsafePDF(ValueError):
    pass


@dataclass(frozen=True)
class ImageUse:
    page_number: int
    xref: int
    width_pt: float
    height_pt: float


def field_snapshot(doc: fitz.Document) -> list:
    fields = []
    for pno, page in enumerate(doc):
        for widget in page.widgets() or ():
            fields.append((pno, widget.field_name, widget.field_type,
                           widget.field_value, widget.field_flags,
                           tuple(round(float(v), 3) for v in widget.rect)))
    return fields


def content_snapshot(doc: fitz.Document) -> list:
    # PDF text/links and fields are not rewritten by this tool.  Verify anyway.
    return [(p.get_text('text'),
             tuple((ln.get('kind'), ln.get('uri'), tuple(round(float(v), 2) for v in ln['from']) if 'from' in ln else None) for ln in p.get_links()))
            for p in doc]


def check_pdf(doc: fitz.Document) -> None:
    if doc.needs_pass:
        raise UnsafePDF('パスワードで保護されたPDFには対応していません．')
    if doc.is_repaired:
        raise UnsafePDF('破損修復が必要なPDFは安全のため処理しません．')
    catalog = doc.pdf_catalog()
    kind, val = doc.xref_get_key(catalog, 'AcroForm')
    if kind == 'xref':
        ref = int(val.split()[0])
        xfa_type, _ = doc.xref_get_key(ref, 'XFA')
        if xfa_type != 'null':
            raise UnsafePDF('XFA形式のフォームは安全のため処理しません．')
    for page in doc:
        for widget in page.widgets() or ():
            if widget.field_type == fitz.PDF_WIDGET_TYPE_SIGNATURE:
                raise UnsafePDF('署名フィールドを含むPDFは変更しません．')
    # Signed documents can also include signatures in other form structures.
    for xref in range(1, doc.xref_length()):
        if '/ByteRange' in doc.xref_object(xref):
            raise UnsafePDF('電子署名を含むPDFは変更しません．')


def _safe_image_xref(doc: fitz.Document, xref: int) -> bool:
    """Reject masks, transparency and 1-bit images before lossy conversion."""
    try:
        if xref <= 0 or not doc.xref_is_image(xref):
            return False
        obj = doc.xref_object(xref)
        if ('/SMask' in obj or '/Mask ' in obj or '/ImageMask true' in obj):
            return False
        bpc_type, bpc_val = doc.xref_get_key(xref, 'BitsPerComponent')
        return bpc_type != 'int' or int(bpc_val) != 1
    except (RuntimeError, ValueError):
        return False


def inspect_images(doc: fitz.Document) -> tuple[list[ImageUse], dict]:
    """Find painted images and image resources through independent paths.

    PyMuPDF's get_image_info(xrefs=True) may yield xref=0 when its
    rendered-image digest cannot be matched to an actual Image XObject.
    Therefore xref=0 means *unresolved*, not necessarily inline.
    page.get_images(full=True) supplies independent resource xrefs.
    """
    uses: dict[int, ImageUse] = {}
    resources: set[int] = set()
    unresolved = 0
    counts = dict(displayed_images=0, inline_images=0, masked_images=0,
                  unsupported_images=0, eligible_images=0,
                  resource_images=0, resource_fallback_images=0,
                  unresolved_image_refs=0, document_image_xobjects=0,
                  document_fallback_images=0)
    for pn, page in enumerate(doc):
        for info in page.get_image_info(xrefs=True):
            counts['displayed_images'] += 1
            xref = int(info.get('xref') or 0)
            if not xref:
                unresolved += 1
                continue
            if not _safe_image_xref(doc, xref):
                counts['masked_images'] += 1
                continue
            uses.setdefault(xref, ImageUse(pn, xref, 0, 0))
        # Resource enumeration is independent of rendered bitmap digest matching.
        # This includes referenced images from nested Form XObjects.
        for entry in page.get_images(full=True):
            xref = int(entry[0])
            if xref <= 0:
                continue
            resources.add(xref)
            if not _safe_image_xref(doc, xref):
                counts['masked_images'] += 1
                continue
            if xref not in uses:
                uses[xref] = ImageUse(pn, xref, 0, 0)
                counts['resource_fallback_images'] += 1
    # Image XObjects can also be referenced from annotation/widget appearance
    # streams or indirectly nested resources.  page.get_images() does not
    # necessarily enumerate them.  Discover image objects at document scope.
    for xref in range(1, doc.xref_length()):
        try:
            if not doc.xref_is_image(xref):
                continue
            counts['document_image_xobjects'] += 1
            if xref in uses or not _safe_image_xref(doc, xref):
                continue
            uses[xref] = ImageUse(0, xref, 0, 0)
            counts['document_fallback_images'] += 1
        except (ValueError, RuntimeError):
            continue
    counts['resource_images'] = len(resources)
    # These cannot confidently be called inline images without checking BI/ID/EI.
    counts['unresolved_image_refs'] = unresolved
    counts['inline_images'] = unresolved
    counts['eligible_images'] = len(uses)
    return list(uses.values()), counts


def image_uses(doc: fitz.Document) -> list[ImageUse]:
    return inspect_images(doc)[0]


def desired_sizes(doc: fitz.Document, uses: list[ImageUse], dpi: int) -> dict[int, tuple[int, int]]:
    requested = {use.xref for use in uses}
    dimensions = {xref: [0.0, 0.0] for xref in requested}
    for page in doc:
        for info in page.get_image_info(xrefs=True):
            xref = int(info.get('xref') or 0)
            if xref in dimensions:
                bbox = fitz.Rect(info['bbox'])
                dimensions[xref][0] = max(dimensions[xref][0], abs(bbox.width))
                dimensions[xref][1] = max(dimensions[xref][1], abs(bbox.height))
        # Some valid Image XObjects are reported with xref=0 in image_info.
        # Ask the page resource tree for their bounding boxes directly.
        for xref in requested:
            try:
                for rect in page.get_image_rects(xref):
                    dimensions[xref][0] = max(dimensions[xref][0], abs(rect.width))
                    dimensions[xref][1] = max(dimensions[xref][1], abs(rect.height))
            except (RuntimeError, ValueError):
                continue
    result = {}
    # If get_image_info() has xref=0 and resource enumeration is empty,
    # these bboxes are useful even without a reliable image-to-xref mapping.
    # Use the largest unknown placement as a conservative estimate; never
    # associate different images with arbitrary page positions.
    unknown_w = unknown_h = 0.0
    for page in doc:
        for info in page.get_image_info(xrefs=True):
            if not info.get('xref'):
                rect = fitz.Rect(info['bbox'])
                unknown_w = max(unknown_w, abs(rect.width))
                unknown_h = max(unknown_h, abs(rect.height))
    for xref, (w, h) in dimensions.items():
        if w == 0 or h == 0:
            w, h = unknown_w, unknown_h
        if w > 0 and h > 0:
            result[xref] = (max(1, math.ceil(w * dpi / 72)),
                            max(1, math.ceil(h * dpi / 72)))
        else:
            # No reliable page placement; preserve dimensions, recompress only.
            try:
                img = doc.extract_image(xref)
                result[xref] = (int(img['width']), int(img['height']))
            except (KeyError, RuntimeError, ValueError):
                continue
    return result


def _replace_image_object(doc: fitz.Document, xref: int, jpeg: bytes) -> None:
    """Replace an Image XObject *in place*, without touching page streams.

    Page.replace_image() adds an image to a page before copying its object,
    which is unnecessary when images live only in annotation appearances.
    """
    if not _safe_image_xref(doc, xref):
        raise ValueError('unsupported image object')
    with Image.open(io.BytesIO(jpeg)) as image:
        w, h = image.size
        if image.mode not in ('RGB', 'L'):
            raise ValueError('unsupported JPEG color space')
        cs = '/DeviceGray' if image.mode == 'L' else '/DeviceRGB'
    # Only simple Device* images without external decoding instructions are
    # supported.  Explicit Decode arrays may invert channel meaning.
    for key in ('Decode', 'DecodeParms', 'Alternates', 'SMaskInData'):
        kind, _ = doc.xref_get_key(xref, key)
        if kind != 'null':
            raise ValueError('complex image decoding')
    doc.update_object(xref, f'<< /Type /XObject /Subtype /Image /Width {w} /Height {h} '
                            f'/ColorSpace {cs} /BitsPerComponent 8 /Filter /DCTDecode >>')
    doc.update_stream(xref, jpeg, compress=False)


def as_jpeg(source: bytes, target: tuple[int, int], quality: int) -> bytes | None:
    try:
        with Image.open(io.BytesIO(source)) as im:
            if im.width * im.height > MAX_PIXELS:
                return None
            # Keep alpha / transparency images unchanged to preserve their appearance.
            if im.info.get('transparency') is not None or 'A' in im.getbands():
                return None
            if im.mode in ('P', 'CMYK', 'YCbCr'):
                im = im.convert('RGB')
            elif im.mode not in ('RGB', 'L'):
                return None
            # Shrink, but NEVER enlarge a low resolution image.
            im.thumbnail(target, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            im.save(out, format='JPEG', quality=quality, optimize=True, progressive=True)
            return out.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None


def make_candidate(src: Path, dst: Path, dpi: int, quality: int) -> tuple[int, int, int]:
    with fitz.open(src) as doc:
        check_pdf(doc)
        uses = image_uses(doc)
        sizes = desired_sizes(doc, uses, dpi)
        from inline_images import compress_inline_images
        count = 0
        for use in uses:
            try:
                extracted = doc.extract_image(use.xref)
                if not extracted or 'image' not in extracted or use.xref not in sizes:
                    continue
                compressed = as_jpeg(extracted['image'], sizes[use.xref], quality)
                if compressed is None or len(compressed) >= len(extracted['image']):
                    continue
                _replace_image_object(doc, use.xref, compressed)
                count += 1
            except (KeyError, RuntimeError, ValueError, OSError):
                # Leave unsupported or problematic images untouched.
                continue
        inline_count = compress_inline_images(doc, dpi, quality)
        count += inline_count
        doc.save(dst, garbage=3, deflate=True)
        return count, len(uses), inline_count


def verify(src: Path, dst: Path) -> None:
    with fitz.open(src) as before, fitz.open(dst) as after:
        if before.page_count != after.page_count:
            raise UnsafePDF('ページ数が変化したため処理を中止しました．')
        if field_snapshot(before) != field_snapshot(after):
            raise UnsafePDF('フォーム入力の維持を確認できないため処理を中止しました．')
        if content_snapshot(before) != content_snapshot(after):
            raise UnsafePDF('文字またはリンクの維持を確認できないため処理を中止しました．')


def optimize(src: Path, dst: Path) -> dict:
    input_size = src.stat().st_size
    with fitz.open(src) as doc:
        check_pdf(doc)
        _, image_stats = inspect_images(doc)
        if image_stats['inline_images']:
            from inline_images import diagnose_inline_images
            image_stats.update(diagnose_inline_images(doc))
            if image_stats['inline_unknown_location']:
                from stream_diagnostics import diagnose_unlocated_images
                image_stats.update(diagnose_unlocated_images(doc))
        n_images = image_stats['displayed_images']
        pages = doc.page_count
    # An already-small PDF is not re-encoded needlessly. Report that clearly.
    if input_size <= TARGET_BYTES:
        dst.write_bytes(src.read_bytes())
        return dict(input_bytes=input_size, output_bytes=input_size, pages=pages,
                    image_count=n_images, compressed_images=0, achieved=True,
                    note='元のPDFがすでに1 MB以内のため，画像は変更していません．', dpi=None,
                    reason='already_under_target', compressed_inline_images=0, **image_stats)
    best_size = input_size
    best_path = None
    best_count = 0
    best_dpi = None
    best_inline_count = 0
    candidate = dst.with_name('candidate.pdf')
    try:
        for dpi, quality in PRESETS:
            count, _, inline_count = make_candidate(src, candidate, dpi, quality)
            size = candidate.stat().st_size
            if size < best_size:
                verify(src, candidate)
                best_path = candidate.read_bytes()
                best_size, best_count, best_dpi = size, count, dpi
                best_inline_count = inline_count
            if size <= TARGET_BYTES:
                break
        if best_path is None:
            dst.write_bytes(src.read_bytes())
        else:
            dst.write_bytes(best_path)
    finally:
        candidate.unlink(missing_ok=True)
    output_size = dst.stat().st_size
    if best_count == 0:
        if n_images == 0:
            note = 'ページ内に表示された画像を検出できませんでした．ベクター図形や注釈の外観である可能性があります．'
            reason = 'no_displayed_images'
        elif image_stats['eligible_images'] == 0:
            if image_stats.get('inline_images'):
                found = image_stats.get('inline_blocks_found', 0)
                parseable = image_stats.get('inline_blocks_parseable', 0)
                in_forms = image_stats.get('inline_in_forms', 0)
                if not found:
                    note = (f"表示画像{image_stats['inline_images']}件の参照元を特定できませんでした．"
                            '画像XObjectとの対応付けもできていません．'
                            'JPEG品質やdpiを変更しても解決しません．')
                else:
                    note = (f"インライン画像{image_stats['inline_images']}件を検出しましたが，圧縮できませんでした．"
                            f"描画命令内で発見{found}件（対応形式{parseable}件，フォーム内部{in_forms}件）．"
                            '未対応の形式または圧縮後にサイズが減らなかった可能性があります．')
            else:
                note = (f"画像は{n_images}件ありますが，今回対応できる画像は0件でした．"
                        f"マスク等{image_stats['masked_images']}件．")
            reason = 'unsupported_image_structure'
        else:
            note = ('画像XObjectは検出されましたが，JPEG再圧縮でファイルサイズが減りませんでした．'
                    '画像が既に圧縮済みの可能性があります．')
            reason = 'no_image_replaced'
    elif output_size <= TARGET_BYTES:
        note = f'{best_count}個の画像を圧縮し，1 MB以内になりました．'
        reason = 'achieved'
    else:
        note = f'{best_count}個の画像を圧縮しましたが，画像だけでは1 MB以内にできませんでした．'
        reason = 'above_target'
    return dict(input_bytes=input_size, output_bytes=output_size,
                pages=pages, image_count=n_images, compressed_images=best_count,
                achieved=output_size <= TARGET_BYTES,
                note=note, dpi=best_dpi, reason=reason,
                compressed_inline_images=best_inline_count, **image_stats)


def main():
    src, dst = map(Path, sys.argv[1:3])
    try:
        print(json.dumps({'ok': True, **optimize(src, dst)}, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
