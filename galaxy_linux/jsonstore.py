"""Kleine Helfer für atomar geschriebene JSON-Dateien."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: Path, data: Any, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Sperre über Prozessgrenzen (flock auf ``.<name>.lock`` daneben) – für Lesen-Ändern-Schreiben einer Datei, die
    auch andere galaxy-linux-Prozesse (Oberfläche, Kommandozeile) schreiben."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_name(f".{path.name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
