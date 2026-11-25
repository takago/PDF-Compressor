"""PDF Compressor: single Uvicorn process, FIFO admission, two PDF subprocesses."""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import stat
import os
import re
import shutil
import sys
import time
import uuid
import zipfile
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('PDF_OPT_TMP', '/tmp/pdf-compressor')).resolve()
MAX_UPLOAD = int(os.environ.get('PDF_OPT_MAX_MB', '200')) * 1_000_000
TTL = 300
PROCESS_TIMEOUT = 120
MAX_RUNNING = 2
MAX_WAITING = 600
# A waiting upload must not accumulate on disk indefinitely if the queue stalls.
MAX_WAIT_SECONDS = 3600
logger = logging.getLogger('pdf_compressor')


@dataclass
class Job:
    id: str
    filename: str
    state: str = 'queued'
    created: float = field(default_factory=time.time)
    expires: float = 0
    result: dict = field(default_factory=dict)
    error: str = ''
    batch_id: str = ''
    relative_path: str = ''


@dataclass
class Batch:
    id: str
    job_ids: list[str]
    state: str = 'running'
    expires: float = 0
    archive: str = ''


batches: dict[str, Batch] = {}
queue: asyncio.Queue[str] | None = None
jobs: dict[str, Job] = {}
workers: list[asyncio.Task] = []
# Bound simultaneous uploads to avoid unlimited disk writes during bursts.
upload_slots: asyncio.Semaphore | None = None


def safe_filename(original: str, compressed: bool = True) -> str:
    base = str(original or 'document.pdf').replace('\\', '/').split('/')[-1]
    stem = base[:-4] if base.lower().endswith('.pdf') else base
    stem = ''.join(c for c in stem if c.isprintable() and c not in '/\\\x00\r\n\t' and ord(c) != 127).strip(' .')[:120]
    stem = stem or 'document'
    return stem + ('_compressed' if compressed else '') + '.pdf'


def safe_relative_pdf_path(path: str, uploaded_name: str) -> str:
    """Validate client-supplied folder path; return a safe ZIP member path.

    Reject traversal and ambiguous path forms rather than silently normalizing them.
    The path must terminate in the uploaded PDF's filename.
    """
    if not isinstance(path, str) or not path or len(path) > 1024:
        raise HTTPException(400, 'フォルダ内のファイルパスが不正です．')
    if "\\" in path or path.startswith('/') or ':' in path or '\x00' in path:
        raise HTTPException(400, 'フォルダ内のファイルパスが不正です．')
    parts = path.split('/')
    if len(parts) > 32 or any(part in ('', '.', '..') or len(part) > 180 or
                            any(ord(c) < 32 or ord(c) == 127 for c in part)
                            for part in parts):
        raise HTTPException(400, 'フォルダ内のファイルパスが不正です．')
    if parts[-1] != uploaded_name or not parts[-1].lower().endswith('.pdf'):
        raise HTTPException(400, 'フォルダ内のファイル名が一致しません．')
    safe_parts = [safe_path_component(x) for x in parts[:-1]]
    return '/'.join([*safe_parts, safe_filename(parts[-1], False)])


def safe_upload_path(path: str, filename: str) -> str:
    """Validate a folder-relative PDF or ZIP path without trusting ZIP names."""
    if not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path or path.startswith('/') or ':' in path or '\x00' in path:
        raise HTTPException(400, 'フォルダ内のファイルパスが不正です．')
    parts = path.split('/')
    if (len(parts) > 32 or any(x in ('', '.', '..') or len(x) > 180 or
            any(ord(c) < 32 or ord(c) == 127 for c in x) for x in parts)
            or parts[-1] != filename or not filename.lower().endswith(('.pdf', '.zip'))):
        raise HTTPException(400, 'フォルダ内のファイルパスが不正です．')
    return '/'.join([*(safe_path_component(x) for x in parts[:-1]),
                     safe_filename(filename, False) if filename.lower().endswith('.pdf')
                     else safe_path_component(filename)])


MAX_ZIP_DEPTH = 3
MAX_EXPANDED_BYTES = 500_000_000
MAX_ZIP_ENTRIES = 3000


def expand_uploads(staged: list[tuple[Path, str]], target: Path) -> list[tuple[Path, str]]:
    """Expand archives safely into private temporary files; never extract ZIP paths directly."""
    expanded: list[tuple[Path, str]] = []
    total_expanded = 0
    total_entries = 0
    temp_index = 0
    seen: set[str] = set()

    def check_member_path(name: str) -> list[str]:
        if ('\\' in name or name.startswith('/') or ':' in name or '\x00' in name):
            raise HTTPException(400, 'ZIP内に危険なパスがあります．')
        parts = name.rstrip('/').split('/')
        if (not parts or len(parts) > 32 or any(not x or x in ('.', '..') or len(x) > 180 or
                any(ord(c) < 32 or ord(c) == 127 for c in x) for x in parts)):
            raise HTTPException(400, 'ZIP内に危険なパスがあります．')
        return [safe_path_component(x) for x in parts]

    def visit(source: Path, relative: str, depth: int, from_zip: bool = False) -> None:
        nonlocal total_expanded, total_entries, temp_index
        if relative.lower().endswith('.pdf'):
            if from_zip and relative in seen:
                raise HTTPException(400, 'ZIP内に同じパスのPDFがあります．')
            if len(expanded) >= MAX_BATCH_FILES:
                raise HTTPException(413, 'ZIP展開後のPDFは300件までです．')
            if source.stat().st_size > MAX_UPLOAD:
                raise HTTPException(413, 'ZIP内のPDFが1ファイルの容量上限を超えています．')
            if source.open('rb').read(5) != b'%PDF-':
                raise HTTPException(400, 'ZIP内にPDFとして読み込めないファイルがあります．')
            seen.add(relative)
            expanded.append((source, relative))
            return
        if depth >= MAX_ZIP_DEPTH:
            raise HTTPException(413, 'ZIPの入れ子は3階層までです．')
        archive_base = relative[:-4]
        try:
            with zipfile.ZipFile(source) as archive:
                for item in archive.infolist():
                    total_entries += 1
                    if total_entries > MAX_ZIP_ENTRIES:
                        raise HTTPException(413, 'ZIP内のファイル・フォルダ数が上限を超えました．')
                    parts = check_member_path(item.filename)
                    mode = (item.external_attr >> 16) & 0o170000
                    if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                        raise HTTPException(400, 'ZIP内の特殊ファイルには対応していません．')
                    if item.flag_bits & 1:
                        raise HTTPException(400, '暗号化ZIPには対応していません．')
                    if item.is_dir():
                        continue
                    suffix = parts[-1].lower()
                    if not suffix.endswith(('.pdf', '.zip')):
                        continue
                    member_rel = '/'.join([archive_base, *parts])
                    if len(member_rel) > 1024:
                        raise HTTPException(400, 'ZIP内のパスが長すぎます．')
                    if item.file_size > MAX_UPLOAD:
                        raise HTTPException(413, 'ZIP内のファイルが200 MBを超えています．')
                    if total_expanded + item.file_size > MAX_EXPANDED_BYTES:
                        raise HTTPException(413, 'ZIP展開後の合計容量が500 MBを超えています．')
                    temp_index += 1
                    out = target / f'extracted-{temp_index}'
                    actual = 0
                    with archive.open(item) as inp, out.open('wb') as dest:
                        while data := inp.read(1024 * 1024):
                            actual += len(data)
                            if (actual > MAX_UPLOAD or
                                    total_expanded + actual > MAX_EXPANDED_BYTES):
                                raise HTTPException(413, 'ZIP展開後の容量上限を超えました．')
                            dest.write(data)
                    total_expanded += actual
                    visit(out, member_rel, depth + 1 if suffix.endswith('.zip') else depth, True)
        except (zipfile.BadZipFile, EOFError, RuntimeError, OSError, NotImplementedError) as exc:
            raise HTTPException(400, 'ZIPが破損しているか，対応できない圧縮方式です．') from exc

    for source, relative in staged:
        if relative.lower().endswith('.zip'):
            visit(source, relative, 0)
        else:
            visit(source, relative, 0)
    if not expanded:
        raise HTTPException(400, '圧縮対象のPDFが見つかりませんでした．')
    return expanded


def safe_path_component(name: str) -> str:
    return name.strip(' .')[:180] or 'folder'


def archive_filename(record: Job) -> str:
    """Preserve folder structure while using the actual output basename."""
    if record.relative_path:
        parent = record.relative_path.rpartition('/')[0]
        return (parent + '/' if parent else '') + record.filename
    return record.filename


def purge_expired():
    now = time.time()
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    for entry in DATA_DIR.iterdir():
        if not entry.is_dir() or not re.fullmatch(r'[0-9a-f]{32}', entry.name):
            continue
        record = jobs.get(entry.name)
        if record and record.state in ('queued', 'running'):
            continue
        if record and record.batch_id and batches.get(record.batch_id) and batches[record.batch_id].state == 'running':
            continue
        try:
            deadline_file = entry / 'expires.txt'
            deadline = (float(deadline_file.read_text(encoding='ascii'))
                        if deadline_file.exists() else entry.stat().st_mtime + MAX_WAIT_SECONDS)
            if now >= deadline:
                shutil.rmtree(entry)
        except (OSError, ValueError):
            continue
    for bid, batch in list(batches.items()):
        if batch.expires and now >= batch.expires:
            shutil.rmtree(DATA_DIR / ('batch-' + bid), ignore_errors=True)
        if batch.expires and now >= batch.expires + 120:
            batches.pop(bid, None)
    # Keep finished status metadata briefly after output expiry, but not forever.
    for ident, record in list(jobs.items()):
        if record.state in ('done', 'failed') and record.expires and now >= record.expires + 120:
            jobs.pop(ident, None)


async def janitor():
    while True:
        await asyncio.sleep(15)
        await asyncio.to_thread(purge_expired)


def mark_failed(record: Job, reason: str):
    record.state = 'failed'
    record.error = reason
    record.expires = time.time() + TTL
    shutil.rmtree(DATA_DIR / record.id, ignore_errors=True)


async def process_one(record: Job):
    directory = DATA_DIR / record.id
    src, dst, result_file = directory / 'input.pdf', directory / 'output.pdf', directory / 'result.json'
    proc = await asyncio.create_subprocess_exec(
        sys.executable, '-u', str(ROOT / 'worker.py'), str(src), str(dst), str(result_file),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=str(ROOT))
    try:
        out, err = await asyncio.wait_for(proc.communicate(), PROCESS_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise ValueError('PDFの圧縮が時間制限を超えました．')
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
        raise
    try:
        raw = result_file.read_text(encoding='utf-8') if result_file.is_file() else out.decode('utf-8')
        result = json.loads(raw)
        if not isinstance(result, dict) or not isinstance(result.get('ok'), bool):
            raise ValueError('invalid worker result')
    except (ValueError, UnicodeDecodeError, OSError) as exc:
        logger.error('Worker invalid result (exit=%s, stdout=%s bytes, stderr=%s, issue=%s)',
                     proc.returncode, len(out), err.decode(errors='replace')[:300], exc)
        raise ValueError('PDF処理プログラムから結果を取得できませんでした．') from exc
    if proc.returncode or not result['ok'] or not dst.is_file():
        raise ValueError(str(result.get('error') or 'PDFを処理できませんでした．'))
    src.unlink(missing_ok=True)
    finished = time.time()
    record.expires = finished + TTL
    (directory / 'expires.txt').write_text(str(record.expires), encoding='ascii')
    record.filename = safe_filename(record.filename, result.get('output_bytes', 0) < result.get('input_bytes', 0))
    record.result = {k: v for k, v in result.items() if k != 'ok'}
    record.state = 'done'


async def queue_worker(number: int):
    assert queue is not None
    while True:
        ident = await queue.get()
        try:
            record = jobs.get(ident)
            if record is None or record.state != 'queued':
                continue
            if time.time() - record.created >= MAX_WAIT_SECONDS:
                mark_failed(record, '待機時間の上限を超えました．もう一度アップロードしてください．')
                continue
            record.state = 'running'
            try:
                await process_one(record)
            except Exception as exc:
                logger.warning('Compression job %s failed: %s', ident, type(exc).__name__)
                mark_failed(record, str(exc) if isinstance(exc, ValueError) else 'PDFの圧縮中にエラーが発生しました．')
        finally:
            record = jobs.get(ident)
            if record and record.batch_id and record.batch_id in batches:
                try:
                    batch_state(batches[record.batch_id])
                except HTTPException:
                    pass
            queue.task_done()


@asynccontextmanager
async def lifespan(application: FastAPI):
    global queue, upload_slots, admission_lock
    DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    jobs.clear()
    batches.clear()
    await asyncio.to_thread(purge_expired)
    queue = asyncio.Queue(maxsize=MAX_WAITING)
    upload_slots = asyncio.Semaphore(8)
    admission_lock = asyncio.Lock()
    workers[:] = [asyncio.create_task(queue_worker(i)) for i in range(MAX_RUNNING)]
    cleaner = asyncio.create_task(janitor())
    try:
        yield
    finally:
        cleaner.cancel()
        for task in workers:
            task.cancel()
        await asyncio.gather(cleaner, *workers, return_exceptions=True)
        workers.clear()
        queue = None
        upload_slots = None
        admission_lock = None


app = FastAPI(title='PDF Compressor', lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')


@app.get('/')
async def index():
    return FileResponse(ROOT / 'static' / 'index.html', media_type='text/html',
                        headers={'Cache-Control': 'no-store'})


@app.post('/api/compress', status_code=202)
async def compress(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith('.pdf'):
        raise HTTPException(400, 'PDFファイルを指定してください．')
    if queue is None or upload_slots is None:
        raise HTTPException(503, 'サービスの初期化が完了していません．')
    if queue.full():
        raise HTTPException(503, '現在混雑しています．少し経ってから再度お試しください．')
    ident = uuid.uuid4().hex
    directory = DATA_DIR / ident
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    size = 0
    try:
        async with upload_slots:
            with (directory / 'input.pdf').open('wb') as handle:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, f'アップロードできるPDFは{MAX_UPLOAD // 1_000_000} MBまでです．')
                    await asyncio.to_thread(handle.write, chunk)
            with (directory / 'input.pdf').open('rb') as handle:
                if handle.read(5) != b'%PDF-':
                    raise HTTPException(400, 'PDF形式ではありません．')
        record = Job(id=ident, filename=file.filename)
        jobs[ident] = record
        try:
            queue.put_nowait(ident)
        except asyncio.QueueFull:
            jobs.pop(ident, None)
            raise HTTPException(503, '現在混雑しています．少し経ってから再度お試しください．')
        return {'id': ident, 'status_url': f'/api/status/{ident}'}
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        await file.close()



# Upload a batch as one request, registering all jobs together.
# Cap a single batch at 300 files while preserving room for other users in the 600-job queue.
MAX_BATCH_FILES = 300
MAX_BATCH_BYTES = 500_000_000
admission_lock: asyncio.Lock | None = None


@app.post('/api/batches', status_code=202)
async def create_batch(files: list[UploadFile] = File(...), paths: str | None = Form(None)):
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(400, '一度にアップロードできるPDF・ZIPは300件までです．')
    if not files or (len(files) == 1 and paths is None and
                     not (files[0].filename or '').lower().endswith('.zip')):
        raise HTTPException(400, '一括処理は複数PDF，フォルダ，またはZIPを指定してください．')
    if any(not f.filename or not f.filename.lower().endswith(('.pdf', '.zip')) for f in files):
        raise HTTPException(400, 'PDFとZIP以外のファイルが含まれています．')
    relative_paths = [''] * len(files)
    if paths is not None:
        try:
            supplied = json.loads(paths)
        except (ValueError, TypeError):
            raise HTTPException(400, 'フォルダ情報が正しくありません．')
        if not isinstance(supplied, list) or len(supplied) != len(files):
            raise HTTPException(400, 'ファイルの件数とフォルダ情報が一致しません．')
        relative_paths = [safe_upload_path(path, f.filename or '')
                          for path, f in zip(supplied, files)]
        if len(set(relative_paths)) != len(relative_paths):
            raise HTTPException(400, '同じパスのファイルが重複しています．')
    if queue is None or upload_slots is None or admission_lock is None:
        raise HTTPException(503, 'サービスの初期化が完了していません．')
    bid = uuid.uuid4().hex
    written: list[str] = []
    records: list[Job] = []
    async with admission_lock:
        if queue.qsize() >= MAX_WAITING:
            raise HTTPException(503, '待機キューが混雑しています．後ほど再試行してください．')
        try:
            with tempfile.TemporaryDirectory(prefix='pdf-batch-', dir=DATA_DIR) as staging_name:
                stage = Path(staging_name)
                staged = []
                total = 0
                async with upload_slots:
                    for index, f in enumerate(files):
                        path = stage / f'upload-{index}'
                        size = 0
                        with path.open('wb') as stream:
                            while chunk := await f.read(1024 * 1024):
                                size += len(chunk)
                                total += len(chunk)
                                if size > MAX_UPLOAD or total > MAX_BATCH_BYTES:
                                    raise HTTPException(413, 'アップロード容量の上限を超えました．')
                                await asyncio.to_thread(stream.write, chunk)
                        if f.filename.lower().endswith('.pdf') and path.open('rb').read(5) != b'%PDF-':
                            raise HTTPException(400, 'PDF形式ではないファイルが含まれています．')
                        staged.append((path, relative_paths[index] or
                                       (safe_filename(f.filename, False) if f.filename.lower().endswith('.pdf')
                                        else safe_path_component(f.filename))))
                extracted = await asyncio.to_thread(expand_uploads, staged, stage)
                if queue.qsize() + len(extracted) > MAX_WAITING:
                    raise HTTPException(503, '展開後のPDF件数が待機キューの空き容量を超えています．')
                for source, relative in extracted:
                    ident = uuid.uuid4().hex
                    directory = DATA_DIR / ident
                    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
                    written.append(ident)
                    shutil.copyfile(source, directory / 'input.pdf')
                    output_name = relative.rsplit('/', 1)[-1]
                    records.append(Job(id=ident, filename=output_name, batch_id=bid,
                                       relative_path=relative if (paths is not None or any(
                                           (f.filename or '').lower().endswith('.zip') for f in files)) else ''))
            batches[bid] = Batch(id=bid, job_ids=[r.id for r in records])
            for record in records:
                jobs[record.id] = record
                queue.put_nowait(record.id)
        except Exception:
            batches.pop(bid, None)
            for r in records:
                jobs.pop(r.id, None)
            for ident in written:
                shutil.rmtree(DATA_DIR / ident, ignore_errors=True)
            raise
        finally:
            for f in files:
                await f.close()
    return {'id': bid, 'status_url': f'/api/batches/{bid}', 'download_url': f'/api/batches/{bid}/download',
            'total': len(records)}


def batch_state(batch: Batch):
    records = [jobs.get(x) for x in batch.job_ids]
    if any(r is None for r in records):
        raise HTTPException(410, '一部のジョブ情報が失われました．')
    counters = Counter(r.state for r in records)
    complete = not counters['queued'] and not counters['running']
    if complete and batch.state == 'running':
        batch.state = 'ready'
        batch.expires = time.time() + TTL
        for r in records:
            r.expires = batch.expires
            folder = DATA_DIR / r.id
            if folder.is_dir():
                (folder / 'expires.txt').write_text(str(batch.expires), encoding='ascii')
    return records, counters


@app.get('/api/batches/{bid}')
async def batch_status(bid: str):
    batch = batches.get(bid)
    if batch is None:
        raise HTTPException(404, '一括処理が見つかりません．')
    records, counts = batch_state(batch)
    if batch.expires and batch.expires <= time.time():
        raise HTTPException(410, '保存期限が終了しました．')
    finished = batch.state == 'ready'
    successful = [r for r in records if r.state == 'done']
    response = {'id': bid, 'status': 'done' if finished else 'running',
                'total': len(records), 'queued': counts['queued'], 'running': counts['running'],
                'completed': counts['done'], 'failed': counts['failed'],
                'files': [{'name': r.relative_path or safe_filename(r.filename, False), 'status': r.state,
                           **({'input_bytes': r.result.get('input_bytes', 0),
                               'output_bytes': r.result.get('output_bytes', 0),
                               'filename': archive_filename(r)} if r.state == 'done' else {}),
                           **({'error': r.error} if r.state == 'failed' else {})}
                          for r in records]}
    if finished:
        response['download_url'] = f'/api/batches/{bid}/download' if successful else None
        response['expires_seconds'] = max(0, int(batch.expires-time.time()))
    return JSONResponse(response, headers={'Cache-Control': 'no-store'})


@app.get('/api/batches/{bid}/download')
async def batch_download(bid: str):
    batch = batches.get(bid)
    if batch is None:
        raise HTTPException(404, '一括処理が見つかりません．')
    records, _ = batch_state(batch)
    if batch.state != 'ready':
        raise HTTPException(409, 'まだ処理中です．')
    if batch.expires <= time.time():
        raise HTTPException(410, '保存期限が終了しました．')
    done = [r for r in records if r.state == 'done']
    if not done:
        raise HTTPException(422, 'すべてのPDFの処理に失敗しました．')
    folder = DATA_DIR / ('batch-' + bid)
    folder.mkdir(mode=0o700, exist_ok=True)
    path = folder / 'compressed_pdfs.zip'
    if not path.exists():
        temp = folder / 'compressed_pdfs.tmp'
        try:
            used_names = set()
            with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for record in done:
                    src = DATA_DIR / record.id / 'output.pdf'
                    if not src.is_file():
                        raise HTTPException(410, '圧縮済みPDFがありません．')
                    filename = archive_filename(record)
                    stem = filename[:-4]
                    suffix = 2
                    while filename in used_names:
                        filename = f'{stem}_{suffix}.pdf'
                        suffix += 1
                    used_names.add(filename)
                    archive.write(src, filename)
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
    return FileResponse(path, filename='compressed_pdfs.zip', media_type='application/zip',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@app.get('/api/status/{ident}')
async def status(ident: str):
    if not re.fullmatch(r'[0-9a-f]{32}', ident) or ident not in jobs:
        raise HTTPException(404, 'ジョブが見つかりません．')
    record = jobs[ident]
    response = {'id': ident, 'status': record.state}
    if record.state == 'queued':
        response['ahead'] = sum(1 for r in jobs.values() if r.state in ('queued', 'running')
                                and r.created < record.created)
    elif record.state == 'done':
        if record.expires <= time.time():
            raise HTTPException(410, '保存期限が終了しました．')
        response.update(record.result)
        response.update(filename=record.filename, download_url=f'/api/download/{ident}',
                        expires_seconds=max(0, int(record.expires - time.time())))
    elif record.state == 'failed':
        response['error'] = record.error
    return JSONResponse(response, headers={'Cache-Control': 'no-store'})


@app.get('/api/download/{ident}')
async def download(ident: str):
    if not re.fullmatch(r'[0-9a-f]{32}', ident):
        raise HTTPException(404, 'ファイルが見つかりません．')
    record = jobs.get(ident)
    if record is None or record.state != 'done':
        raise HTTPException(404, 'ファイルが見つかりません．')
    if record.expires <= time.time():
        raise HTTPException(410, '保存期限が終了しました．')
    output = DATA_DIR / ident / 'output.pdf'
    if not output.is_file():
        raise HTTPException(404, 'ファイルが見つかりません．')
    return FileResponse(output, filename=record.filename, media_type='application/pdf',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@app.exception_handler(HTTPException)
async def simple_error(request, exc):
    if exc.status_code >= 400:
        logger.warning('Request failed: %s %s -> %s', request.method, request.url.path, exc.status_code)
    return JSONResponse({'error': exc.detail}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    logger.warning('Invalid upload request: %s %s', request.method, request.url.path)
    return JSONResponse({'error': 'PDFファイルを受信できませんでした．アップロード形式を確認してください．'}, status_code=422)
