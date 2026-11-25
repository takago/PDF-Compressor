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


class MissingXrefError(UnsafePDF):
    """An unavailable xref prevented a complete signature safety check."""


def _is_missing_xref(exc: Exception) -> bool:
    message = str(exc).lower()
    return 'cannot find object in xref' in message or 'bad xref' in message


def _image_info(page: fitz.Page) -> list[dict]:
    """If only xref matching fails, retain image geometry without guessed refs."""
    try:
        return page.get_image_info(xrefs=True)
    except (RuntimeError, ValueError) as exc:
        if not _is_missing_xref(exc):
            raise
        return [dict(info, xref=0) for info in page.get_image_info(xrefs=False)]


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
    missing = 0
    for xref in range(1, doc.xref_length()):
        try:
            object_def = doc.xref_object(xref)
        except (RuntimeError, ValueError) as exc:
            if not _is_missing_xref(exc):
                raise
            missing += 1
            continue
        if '/ByteRange' in object_def:
            raise UnsafePDF('電子署名を含むPDFは変更しません．')
    if missing:
        # Never silently skip objects during the document-wide signature scan.
        # Normalize and repeat the complete check on the repaired document.
        raise MissingXrefError('xrefに参照できないオブジェクトがあります．')


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
        for info in _image_info(page):
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
        try:
            entries = page.get_images(full=True)
        except (RuntimeError, ValueError) as exc:
            if not _is_missing_xref(exc):
                raise
            entries = []
        for entry in entries:
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
        for info in _image_info(page):
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
        for info in _image_info(page):
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
    # PyMuPDF update_stream removes the previous /Filter entry.
    # Set it only AFTER writing the JPEG bytes.
    doc.xref_set_key(xref, "Filter", "/DCTDecode")


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





def make_font_candidate(src: Path, dst: Path) -> None:
    """Re-subset used glyphs without changing the source PDF."""
    with fitz.open(src) as doc:
        check_pdf(doc)
        doc.subset_fonts()
        doc.save(str(dst), garbage=4, deflate=True, deflate_fonts=True)
    verify(src, dst)


def optimize(src: Path, dst: Path) -> dict:
    input_size = src.stat().st_size
    # A repaired PDF must be normalized before any image operations. Never edit
    # the user's input; save the repaired structure as a temporary working PDF.
    repaired_path = dst.with_name('repaired_input.pdf')
    working_src = src
    repaired = False
    try:
        with fitz.open(src) as original:
            needs_normalization = bool(original.is_repaired)
            if not needs_normalization:
                try:
                    check_pdf(original)
                except MissingXrefError:
                    needs_normalization = True
            if needs_normalization:
                # A missing object might conceal a signature dictionary. In this
                # ambiguous case, do not rewrite a potentially signed original.
                if not original.is_repaired and (b'/ByteRange' in src.read_bytes()):
                    raise UnsafePDF('電子署名の可能性があるPDFのxrefは自動修復しません．')
                repaired = True
                original_fields = field_snapshot(original)
                original_content = content_snapshot(original)
                original_pages = original.page_count
                original_geometry = [(tuple(p.rect), p.rotation) for p in original]
                try:
                    original.save(repaired_path, garbage=3, deflate=True)
                except (RuntimeError, ValueError) as exc:
                    raise UnsafePDF('xrefを安全に再構築できませんでした．') from exc
                with fitz.open(repaired_path) as normalized:
                    check_pdf(normalized)
                    if normalized.is_repaired:
                        raise UnsafePDF('PDFの自動修復後も構造エラーが残っています．')
                    if (normalized.page_count != original_pages or
                        field_snapshot(normalized) != original_fields or
                        content_snapshot(normalized) != original_content or
                        [(tuple(p.rect), p.rotation) for p in normalized] != original_geometry):
                        raise UnsafePDF('PDFの自動修復で文字・フォーム・リンク・ページ構成の維持を確認できませんでした．')
                working_src = repaired_path
        return _optimize_checked(working_src, dst, input_size, repaired)
    finally:
        repaired_path.unlink(missing_ok=True)


def _optimize_checked(working_src: Path, dst: Path,
                      input_size: int, repaired: bool) -> dict:
    with fitz.open(working_src) as doc:
        check_pdf(doc)
        _, image_stats = inspect_images(doc)
        if image_stats['inline_images']:
            from inline_images import diagnose_inline_images
            try:
                image_stats.update(diagnose_inline_images(doc))
            except (RuntimeError, ValueError) as exc:
                if not _is_missing_xref(exc):
                    raise
                image_stats['image_diagnostics_skipped'] = True
            if image_stats.get('inline_unknown_location'):
                from stream_diagnostics import diagnose_unlocated_images
                try:
                    image_stats.update(diagnose_unlocated_images(doc))
                except (RuntimeError, ValueError) as exc:
                    if not _is_missing_xref(exc):
                        raise
                    image_stats['image_diagnostics_skipped'] = True
        n_images = image_stats['displayed_images']
        pages = doc.page_count

    font_candidate = dst.with_name('font_candidate.pdf')
    image_candidate = dst.with_name('candidate.pdf')
    best_size = working_src.stat().st_size
    best_data = None
    best_count = 0
    best_dpi = None
    best_inline_count = 0
    font_optimized = False
    font_bytes_saved = 0
    font_warning = None
    image_src = working_src

    try:
        # Always attempt font optimization, even if the original is under
        # 1MB: these PDFs are intended as final, submitted documents.
        try:
            make_font_candidate(working_src, font_candidate)
            new_size = font_candidate.stat().st_size
            if new_size < best_size:
                font_optimized = True
                font_bytes_saved = best_size - new_size
                best_size = new_size
                best_data = font_candidate.read_bytes()
                image_src = font_candidate
        except (RuntimeError, ValueError, OSError, MemoryError) as exc:
            # Keep the original and continue with image optimization. A
            # failed/unsafe font rewrite must never be returned.
            font_warning = str(exc)[:200]

        # Always attempt JPEG optimization too. Each preset starts from the
        # same structurally verified font baseline, avoiding cumulative quality loss.
        for dpi, quality in PRESETS:
            count, _, inline_count = make_candidate(image_src, image_candidate, dpi, quality)
            size = image_candidate.stat().st_size
            if size < best_size:
                verify(working_src, image_candidate)
                best_data = image_candidate.read_bytes()
                best_size, best_count, best_dpi = size, count, dpi
                best_inline_count = inline_count
            # Stop once target reached AND a JPEG pass was attempted.
            if size <= TARGET_BYTES:
                break

        if best_data is None:
            dst.write_bytes(working_src.read_bytes())
        else:
            dst.write_bytes(best_data)
    finally:
        font_candidate.unlink(missing_ok=True)
        image_candidate.unlink(missing_ok=True)

    output_size = dst.stat().st_size
    if best_data is None and working_src.stat().st_size <= TARGET_BYTES:
        reason = 'already_under_target'
        note = '元のPDFがすでに1 MB以内で，追加の削減はありませんでした．'
    elif best_count == 0 and not font_optimized and n_images == 0:
        reason = 'no_displayed_images'
        note = '表示画像がなく，フォント最適化でも削減できませんでした．'
    elif best_count == 0 and not font_optimized and image_stats['eligible_images'] == 0:
        reason = 'unsupported_image_structure'
        note = '画像の構造を安全に変換できませんでした．'
    elif output_size <= TARGET_BYTES:
        reason = 'achieved'
        note = '1 MB以内になりました．'
    elif output_size < input_size:
        reason = 'above_target'
        note = '軽量化しましたが，1 MB以内にはできませんでした．'
    else:
        reason = 'no_reduction'
        note = '安全にファイルサイズを削減できませんでした．'
    if font_optimized:
        note = f'フォントを最適化しました（{font_bytes_saved} bytes削減）．' + note
    if best_count:
        note = f'画像{best_count}件を再圧縮しました．' + note
    if font_warning:
        note += ' フォント最適化は検証に失敗したため採用していません．'
    if repaired:
        note = 'PDFの構造を自動修復しました．' + note
    return dict(input_bytes=input_size, output_bytes=output_size,
                pages=pages, image_count=n_images, compressed_images=best_count,
                achieved=output_size <= TARGET_BYTES,
                note=note, dpi=best_dpi, reason=reason,
                compressed_inline_images=best_inline_count, repaired=repaired,
                font_optimized=font_optimized, font_bytes_saved=font_bytes_saved,
                font_warning=font_warning, **image_stats)


def main():
    src, dst = map(Path, sys.argv[1:3])
    try:
        print(json.dumps({'ok': True, **optimize(src, dst)}, ensure_ascii=False), flush=True)
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
