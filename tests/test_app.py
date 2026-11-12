from pathlib import Path
import time
from io import BytesIO

import fitz
from PIL import Image
from fastapi.testclient import TestClient

import app
from optimizer import optimize, field_snapshot


def sample_pdf(path: Path):
    im = Image.effect_noise((2400, 1600), 75).convert('RGB')
    buf = BytesIO()
    im.save(buf, format='JPEG', quality=95)
    with fitz.open() as doc:
        page = doc.new_page(width=595, height=842)
        page.insert_text((40, 50), 'Searchable text is maintained.')
        page.insert_image(fitz.Rect(40, 70, 555, 520), stream=buf.getvalue())
        field = fitz.Widget()
        field.field_name = 'comment'
        field.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        field.rect = fitz.Rect(40, 560, 300, 600)
        field.field_value = 'Hello form data'
        page.add_widget(field)
        page.insert_link({'kind': fitz.LINK_URI, 'from': fitz.Rect(40, 40, 180, 55), 'uri': 'https://example.org'})
        doc.save(path)


def test_preserves_text_form_links_and_changes_image(tmp_path):
    source = tmp_path / 'source.pdf'
    dest = tmp_path / 'output.pdf'
    sample_pdf(source)
    result = optimize(source, dest)
    assert result['output_bytes'] <= result['input_bytes']
    assert result['compressed_images'] == 1
    with fitz.open(source) as a, fitz.open(dest) as b:
        assert a[0].get_text('text') == b[0].get_text('text')
        assert field_snapshot(a) == field_snapshot(b)
        assert a[0].get_links()[0]['uri'] == b[0].get_links()[0]['uri']
        assert b[0].get_images()


def test_web_upload_and_download(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    file = tmp_path / 'sample.pdf'
    sample_pdf(file)
    with TestClient(app.app) as client:
        response = client.post('/api/compress', files={'file': ('sample.pdf', file.read_bytes(), 'application/pdf')})
        assert response.status_code == 202, response.text
        accepted = response.json()
        for _ in range(150):
            response = client.get(accepted['status_url'])
            data = response.json()
            if data['status'] in ('done', 'failed'):
                break
            time.sleep(0.05)
        assert data['status'] == 'done', data
        assert data['filename'] == 'sample_compressed.pdf'
        assert data['input_bytes'] >= data['output_bytes']
        assert data['pages'] == 1
        downloaded = client.get(data['download_url'])
        assert downloaded.status_code == 200
        assert downloaded.content.startswith(b'%PDF-')
        assert not (tmp_path / 'jobs' / data['id'] / 'input.pdf').exists()


def test_reject_wrong_extension(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    with TestClient(app.app) as client:
        response = client.post('/api/compress', files={'file': ('x.txt', b'not a PDF', 'text/plain')})
        assert response.status_code == 400


def test_missing_upload_field_returns_human_error(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    with TestClient(app.app) as client:
        response = client.post('/api/compress', data={'wrong': 'field'})
        assert response.status_code == 422
        assert 'error' in response.json()


def test_invalid_pdf_returns_explanation(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    with TestClient(app.app) as client:
        response = client.post('/api/compress', files={'file': ('bad.pdf', b'%PDF-invalid', 'application/pdf')})
        assert response.status_code == 202
        ident = response.json()['id']
        for _ in range(100):
            result = client.get(f'/api/status/{ident}').json()
            if result['status'] in ('done', 'failed'):
                break
            time.sleep(.05)
        assert result['status'] == 'failed', result
        assert result.get('error')


def test_worker_result_survives_discarded_stdout(tmp_path):
    """The server can read the atomic result even when worker stdout is lost."""
    import subprocess
    import sys
    source = tmp_path / 'source.pdf'
    output = tmp_path / 'output.pdf'
    status = tmp_path / 'result.json'
    sample_pdf(source)
    proc = subprocess.run(
        [sys.executable, str(Path(app.__file__).parent / 'worker.py'),
         str(source), str(output), str(status)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr.decode(errors='replace')
    import json
    result = json.loads(status.read_text(encoding='utf-8'))
    assert result['ok'] is True
    assert output.exists()


def test_already_under_target_has_clear_reason(tmp_path):
    source = tmp_path / 'small.pdf'
    dest = tmp_path / 'output.pdf'
    with fitz.open() as d:
        d.new_page().insert_text((50, 50), 'Hello')
        d.save(source)
    result = optimize(source, dest)
    assert result['reason'] == 'already_under_target'
    assert result['compressed_images'] == 0
    assert dest.read_bytes() == source.read_bytes()


def test_no_images_has_clear_reason(tmp_path):
    source = tmp_path / 'large.pdf'
    dest = tmp_path / 'output.pdf'
    with fitz.open() as d:
        d.new_page().insert_text((50, 50), 'Hello')
        d.embfile_add('random.bin', __import__('os').urandom(1_100_000))
        d.save(source, deflate=False)
    result = optimize(source, dest)
    assert result['compressed_images'] == 0
    assert result['reason'] == 'no_displayed_images'


def test_detects_inline_image_and_reports_unsupported_structure(tmp_path):
    from optimizer import inspect_images
    src = tmp_path / 'inline.pdf'
    out = tmp_path / 'result.pdf'
    with fitz.open() as d:
        page = d.new_page()
        # An inline image lives in a page content stream, not an XObject.
        pixels = bytes([255, 0, 0] * 4)
        content = (b'q 100 0 0 100 40 40 cm\n'
                   b'BI /W 2 /H 2 /BPC 8 /CS /RGB ID\n'
                   + pixels + b'\nEI\nQ\n')
        xref = d.get_new_xref()
        d.update_object(xref, '<<>>')
        d.update_stream(xref, content)
        page.set_contents(xref)
        # Force compression attempt while preserving the page content.
        d.embfile_add('large.bin', __import__('os').urandom(1_100_000))
        d.save(src)
    with fitz.open(src) as doc:
        _, stats = inspect_images(doc)
    assert stats['displayed_images'] >= 1
    assert stats['inline_images'] >= 1
    result = optimize(src, out)
    assert result['reason'] == 'unsupported_image_structure'
    assert result['inline_images'] >= 1


def test_compresses_raw_inline_preserving_text_and_form(tmp_path):
    import os
    import zlib
    src, dest = tmp_path / 'inline-raw.pdf', tmp_path / 'inline-out.pdf'
    w, h = 950, 850
    pixels = os.urandom(w * h * 3)
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((35, 35), 'Text must remain editable')
        widget = fitz.Widget()
        widget.field_name = 'name'
        widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        widget.field_value = 'Sample value'
        widget.rect = fitz.Rect(40, 600, 280, 635)
        page.add_widget(widget)
        extra = (b'q 250 0 0 220 40 100 cm\nBI /W %d /H %d /BPC 8 /CS /RGB ID\n' % (w, h)) + pixels + b'\nEI\nQ\n'
        doc.update_stream(page.get_contents()[0], doc.xref_stream(page.get_contents()[0]) + b'\n' + extra)
        doc.save(src)
    result = optimize(src, dest)
    assert result['compressed_images'] >= 1, result
    assert result['output_bytes'] < result['input_bytes']
    with fitz.open(src) as before, fitz.open(dest) as after:
        assert field_snapshot(before) == field_snapshot(after)
        assert before[0].get_text('text') == after[0].get_text('text')
        assert len(after[0].get_image_info(xrefs=True)) == 1
        assert after[0].get_image_info(xrefs=True)[0]['xref'] > 0
        assert after[0].get_pixmap().samples != b''


def test_compresses_flate_inline(tmp_path):
    import os
    import zlib
    src, dest = tmp_path / 'inline-flate.pdf', tmp_path / 'inline-flate-out.pdf'
    w, h = 920, 840
    pixels = os.urandom(w * h * 3)
    with fitz.open() as doc:
        page = doc.new_page()
        content = (b'q 300 0 0 280 40 100 cm\nBI /W %d /H %d /BPC 8 /CS /RGB /F /Fl ID\n' % (w,h)) + zlib.compress(pixels, 1) + b'\nEI\nQ\n'
        xref = doc.get_new_xref()
        doc.update_object(xref, '<<>>')
        doc.update_stream(xref, content)
        page.set_contents(xref)
        doc.save(src)
    result = optimize(src, dest)
    assert result['compressed_images'] >= 1, result
    assert result['output_bytes'] < result['input_bytes']
    with fitz.open(dest) as doc:
        assert doc[0].get_image_info(xrefs=True)[0]['xref'] > 0


def test_compresses_inline_inside_nested_form_xobject(tmp_path):
    import os
    from inline_images import diagnose_inline_images
    src, dst = tmp_path / 'form.pdf', tmp_path / 'form-out.pdf'
    a = fitz.open()
    page = a.new_page()
    w, h = 900, 800
    xref = a.get_new_xref()
    a.update_object(xref, '<<>>')
    a.update_stream(xref, (b'q 200 0 0 200 30 30 cm\nBI /W %d /H %d /BPC 8 /CS /RGB ID\n' % (w, h)) + os.urandom(w * h * 3) + b'\nEI\nQ')
    page.set_contents(xref)
    b = fitz.open()
    bpage = b.new_page()
    bpage.show_pdf_page(bpage.rect, a, 0)
    bpage.insert_text((30, 25), 'Do not change this text')
    b.embfile_add('payload', os.urandom(1_100_000))
    b.save(src)
    a.close()
    b.close()
    with fitz.open(src) as doc:
        diagnostics = diagnose_inline_images(doc)
        assert diagnostics['inline_in_forms'] >= 1
    result = optimize(src, dst)
    assert result['compressed_inline_images'] >= 1, result
    with fitz.open(src) as old, fitz.open(dst) as new:
        assert old[0].get_text('text') == new[0].get_text('text')
        assert any(i['xref'] > 0 for i in new[0].get_image_info(xrefs=True))
        # Ensure image still actually renders on the resulting page.
        assert len(new[0].get_pixmap().samples) > 0


def test_aggregate_diagnostic_does_not_expose_content(tmp_path):
    from stream_diagnostics import diagnose_unlocated_images
    src = tmp_path / 'diagnostic.pdf'
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((70, 70), 'Very private form content')
        doc.save(src)
    with fitz.open(src) as doc:
        result = diagnose_unlocated_images(doc)
        assert result['scan_streams_checked'] >= 1
        assert result['scan_category'] == 'unresolved_no_bi_stream'
        assert 'Very private' not in str(result)


def test_aggregate_diagnostic_sees_other_stream_bi(tmp_path):
    from stream_diagnostics import diagnose_unlocated_images
    src = tmp_path / 'other.pdf'
    with fitz.open() as doc:
        doc.new_page().insert_text((30, 40), 'safe')
        xref = doc.get_new_xref()
        doc.update_object(xref, '<<>>')
        doc.update_stream(xref, b'BI /W 1 /H 1 /BPC 8 /CS /RGB ID xyz EI')
        doc.save(src)
    with fitz.open(src) as doc:
        result = diagnose_unlocated_images(doc)
        assert result['scan_other_stream_hits'] >= 1
        assert result['scan_category'] == 'other_stream_candidates'


def test_xref_zero_with_real_image_xobject_uses_resource_fallback(tmp_path, monkeypatch):
    """Simulate MuPDF's digest-to-xref matching failure for a real image XObject."""
    from optimizer import inspect_images
    source = tmp_path / 'source.pdf'
    output = tmp_path / 'out.pdf'
    sample_pdf(source)
    actual = fitz.Page.get_image_info

    def unresolved(page, *args, **kwargs):
        records = actual(page, *args, **kwargs)
        if kwargs.get('xrefs'):
            return [dict(info, xref=0) for info in records]
        return records

    monkeypatch.setattr(fitz.Page, 'get_image_info', unresolved)
    with fitz.open(source) as doc:
        uses, stats = inspect_images(doc)
        assert stats['displayed_images'] == 1
        assert stats['resource_images'] == 1
        assert stats['resource_fallback_images'] == 1
        assert len(uses) == 1
    result = optimize(source, output)
    assert result['compressed_images'] == 1, result
    assert result['output_bytes'] < result['input_bytes']
    with fitz.open(source) as before, fitz.open(output) as after:
        assert field_snapshot(before) == field_snapshot(after)
        assert before[0].get_text() == after[0].get_text()


def test_image_xobject_inside_annotation_appearance_without_page_resources(tmp_path):
    """Document-wide image objects must be found even when page resources are empty."""
    from optimizer import inspect_images
    import os
    src = tmp_path / 'annotation.pdf'
    dst = tmp_path / 'annotation-out.pdf'
    im = Image.frombytes('RGB', (1100, 950), os.urandom(1100 * 950 * 3))
    bio = BytesIO()
    im.save(bio, format='JPEG', quality=98)
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((25, 28), 'Keep this text')
        image_xref = page.insert_image(fitz.Rect(30, 45, 400, 430), stream=bio.getvalue())
        annot = page.add_rect_annot(fitz.Rect(30, 45, 400, 430))
        ap_type, ap_value = doc.xref_get_key(annot.xref, 'AP/N')
        assert ap_type == 'xref'
        ap_xref = int(ap_value.split()[0])
        doc.xref_set_key(ap_xref, 'Resources', f'<< /XObject << /TestIm {image_xref} 0 R >> >>')
        doc.update_stream(ap_xref, b'q 370 0 0 385 0 0 cm /TestIm Do Q')
        # Remove the page-level image and its resource mapping.
        for cx in page.get_contents():
            stream = doc.xref_stream(cx)
            doc.update_stream(cx, stream.replace(b'/fzImg0 Do', b' '))
        doc.xref_set_key(page.xref, 'Resources', '<< >>')
        doc.save(src)
    with fitz.open(src) as doc:
        assert not doc[0].get_images(full=True)
        uses, stats = inspect_images(doc)
        assert stats['document_fallback_images'] >= 1, stats
        assert any(use.xref == image_xref for use in uses)
    result = optimize(src, dst)
    assert result['compressed_images'] >= 1, result
    assert result['output_bytes'] < result['input_bytes']
    with fitz.open(src) as original, fitz.open(dst) as compressed:
        assert original[0].get_text() == compressed[0].get_text()
        assert list(original[0].annots()) and list(compressed[0].annots())
        before_images = [x for x in range(1, original.xref_length()) if original.xref_is_image(x)]
        after_images = [x for x in range(1, compressed.xref_length()) if compressed.xref_is_image(x)]
        assert len(before_images) == len(after_images) == 1
        assert len(compressed.extract_image(after_images[0])['image']) < len(original.extract_image(before_images[0])['image'])
        assert compressed[0].get_image_info(xrefs=True)[0]['width'] < original[0].get_image_info(xrefs=True)[0]['width']


def test_filename_sanitization():
    assert app.safe_filename('folder/講義資料.pdf') == '講義資料_compressed.pdf'
    assert app.safe_filename('lecture.pdf', False) == 'lecture.pdf'
    assert app.safe_filename('../test.pdf') == 'test_compressed.pdf'


def test_fifo_two_concurrent_jobs(tmp_path, monkeypatch):
    """With blocking fake PDF workers, only two jobs run and the third waits."""
    import asyncio
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    launched = []
    release = Event()
    async def fake_process(job):
        launched.append(job.id)
        while not release.is_set():
            await asyncio.sleep(0.02)
        job.result = {'input_bytes': 3000, 'output_bytes': 1000, 'compressed_images': 1}
        job.filename = app.safe_filename(job.filename)
        job.expires = time.time() + 300
        job.state = 'done'

    monkeypatch.setattr(app, 'process_one', fake_process)
    with TestClient(app.app) as client:
        pdf = b'%PDF-1234'
        ids = []
        for i in range(3):
            r = client.post('/api/compress', files={'file': (f'x{i}.pdf', pdf, 'application/pdf')})
            assert r.status_code == 202
            ids.append(r.json()['id'])
        for _ in range(100):
            if len(launched) == 2:
                break
            time.sleep(.01)
        assert launched == ids[:2]
        states = [client.get(f'/api/status/{x}').json() for x in ids]
        assert [s['status'] for s in states] == ['running', 'running', 'queued']
        assert states[2]['ahead'] == 2
        release.set()
        for _ in range(100):
            if len(launched) == 3:
                break
            time.sleep(.01)
        assert launched == ids


def test_expired_download_denied(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    pdf = tmp_path / 'small.pdf'
    with fitz.open() as doc:
        doc.new_page()
        doc.save(pdf)
    with TestClient(app.app) as client:
        r = client.post('/api/compress', files={'file': ('small.pdf', pdf.read_bytes(), 'application/pdf')})
        ident = r.json()['id']
        for _ in range(100):
            info = client.get(f'/api/status/{ident}').json()
            if info['status'] == 'done':
                break
            time.sleep(.05)
        assert info['status'] == 'done'
        assert info['filename'] == 'small.pdf'
        app.jobs[ident].expires = time.time() - 1
        assert client.get(f'/api/download/{ident}').status_code == 410


def wait_for_batch(client, ident, rounds=150):
    for _ in range(rounds):
        r = client.get(f'/api/batches/{ident}')
        assert r.status_code == 200, r.text
        data = r.json()
        if data['status'] == 'done':
            return data
        time.sleep(.05)
    raise AssertionError('batch did not complete')


def test_batch_zip_multiple_pdfs_with_duplicate_names(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    src = tmp_path / 'small.pdf'
    with fitz.open() as doc:
        doc.new_page().insert_text((20, 30), 'One page')
        doc.save(src)
    pdf = src.read_bytes()
    with TestClient(app.app) as client:
        r = client.post('/api/batches', files=[
            ('files', ('sample.pdf', pdf, 'application/pdf')),
            ('files', ('sample.pdf', pdf, 'application/pdf')),
            ('files', ('sample_2.pdf', pdf, 'application/pdf')),
        ])
        assert r.status_code == 202, r.text
        bid = r.json()['id']
        data = wait_for_batch(client, bid)
        assert data['total'] == 3
        assert data['completed'] == 3
        assert data['failed'] == 0
        z = client.get(data['download_url'])
        assert z.status_code == 200
        with zipfile.ZipFile(BytesIO(z.content)) as archive:
            names = archive.namelist()
            assert len(names) == len(set(names)) == 3
            assert set(names) == {'sample.pdf', 'sample_2.pdf', 'sample_2_2.pdf'}
            assert all(archive.read(name).startswith(b'%PDF-') for name in names)
        assert app.batches[bid].expires > time.time()


def test_batch_partial_failure_provides_successful_zip(tmp_path, monkeypatch):
    import zipfile
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    src = tmp_path / 'one.pdf'
    with fitz.open() as d:
        d.new_page()
        d.save(src)
    with TestClient(app.app) as client:
        r = client.post('/api/batches', files=[
            ('files', ('ok.pdf', src.read_bytes(), 'application/pdf')),
            ('files', ('bad.pdf', b'%PDF-invalid-content', 'application/pdf')),
        ])
        assert r.status_code == 202, r.text
        result = wait_for_batch(client, r.json()['id'])
        assert result['completed'] == 1
        assert result['failed'] == 1
        z = client.get(result['download_url'])
        assert z.status_code == 200
        with zipfile.ZipFile(BytesIO(z.content)) as archive:
            assert archive.namelist() == ['ok.pdf']


def test_batch_rejects_invalid_upload_without_queueing(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    with TestClient(app.app) as client:
        r = client.post('/api/batches', files=[
            ('files', ('valid.pdf', b'%PDF-valid', 'application/pdf')),
            ('files', ('invalid.pdf', b'notpdf', 'application/pdf')),
        ])
        assert r.status_code == 400
        assert not app.batches
        assert not app.jobs


def test_batch_state_marks_expiry_even_without_browser_polling(tmp_path, monkeypatch):
    monkeypatch.setattr(app, 'DATA_DIR', tmp_path / 'jobs')
    src = tmp_path / 'one.pdf'
    with fitz.open() as d:
        d.new_page()
        d.save(src)
    with TestClient(app.app) as client:
        r = client.post('/api/batches', files=[
            ('files', ('one.pdf', src.read_bytes(), 'application/pdf')),
            ('files', ('two.pdf', src.read_bytes(), 'application/pdf')),
        ])
        bid = r.json()['id']
        for _ in range(150):
            if app.batches[bid].state == 'ready':
                break
            time.sleep(.05)
        assert app.batches[bid].state == 'ready'
        assert app.batches[bid].expires > time.time()
