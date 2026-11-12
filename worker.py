"""Isolated PDF processor. Communicate through an atomic JSON file, not stdout."""
from __future__ import annotations

import json
import os
import resource
import sys
from pathlib import Path

# Limits are defensive; use a container for hostile/untrusted PDFs.
resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
resource.setrlimit(resource.RLIMIT_CPU, (110, 110))
resource.setrlimit(resource.RLIMIT_FSIZE, (200 * 1024**2, 200 * 1024**2))
os.environ.setdefault('OMP_NUM_THREADS', '1')

from optimizer import optimize  # noqa: E402


def main():
    if len(sys.argv) != 4:
        raise SystemExit('Usage: worker.py INPUT OUTPUT RESULT_JSON')
    src, dst, result_path = (Path(value) for value in sys.argv[1:4])
    try:
        result = {'ok': True, **optimize(src, dst)}
        code = 0
    except Exception as exc:
        result = {'ok': False, 'error': str(exc) or type(exc).__name__}
        code = 1
    # Atomic file hand-off avoids accidentally treating missing stdout as a PDF error.
    temp_path = result_path.with_suffix('.json.tmp')
    with temp_path.open('w', encoding='utf-8') as handle:
        json.dump(result, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, result_path)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(code)


if __name__ == '__main__':
    main()
