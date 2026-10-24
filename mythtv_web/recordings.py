"""A shared disk cache with a parsed copy per worker."""

import fcntl
import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path

import requests

from .backend import program_list

PAGE_SIZE = 5000
CACHE_SECONDS = 120


def _fingerprint(stat) -> tuple:
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


class RecordingsCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._memory = None

    def _read(self, path: Path, backend_url: str) -> list[dict] | None:
        try:
            key = str(path), backend_url, _fingerprint(path.stat())
            if self._memory and self._memory[0] == key:
                cached_at, programs = self._memory[1:]
            else:
                with path.open() as stream:
                    # Match the data to this inode, even during atomic replacement.
                    key = (
                        str(path),
                        backend_url,
                        _fingerprint(os.fstat(stream.fileno())),
                    )
                    payload = json.load(stream)
                cached_at = payload["cached_at"]
                programs = payload["programs"]
                if payload.get("backend_url", backend_url) != backend_url:
                    return None
                if (
                    not isinstance(cached_at, (int, float))
                    or not math.isfinite(cached_at)
                    or not isinstance(programs, list)
                    or any(not isinstance(program, dict) for program in programs)
                ):
                    return None
                self._memory = key, cached_at, programs
            if 0 <= time.time() - cached_at <= CACHE_SECONDS:
                return programs
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            pass
        return None

    def load(self, app) -> list[dict]:
        path = Path(app.config["MYTHTV_RECORDINGS_CACHE_PATH"])
        backend_url = app.config["MYTHTV_BACKEND_URL"]
        with self._lock:
            programs = self._read(path, backend_url)
            if programs is not None:
                return programs
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_suffix(f"{path.suffix}.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                programs = self._read(path, backend_url)
                if programs is not None:
                    return programs
                programs = []
                start = 0
                total = None
                while total is None or start < total:
                    response = requests.get(
                        f"{backend_url}/Dvr/GetRecordedList",
                        params={
                            "StartIndex": start,
                            "Count": PAGE_SIZE,
                            "Descending": "true",
                        },
                        headers={"Accept": "application/json"},
                        timeout=app.config["MYTHTV_GROUPS_TIMEOUT"],
                    )
                    response.raise_for_status()
                    payload = program_list(response)
                    page = payload.get("Programs", [])
                    total = payload.get("TotalAvailable", len(page))
                    count = payload.get("Count", len(page))
                    if count != len(page) or (not page and start < total):
                        raise ValueError("MythTV returned an incomplete recording page")
                    programs.extend(page)
                    if not page:
                        break
                    start += count
                cached_at = time.time()
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w", dir=path.parent, prefix=f".{path.name}.", delete=False
                    ) as stream:
                        temporary = Path(stream.name)
                        json.dump(
                            {
                                "cached_at": cached_at,
                                "backend_url": backend_url,
                                "programs": programs,
                            },
                            stream,
                        )
                    os.replace(temporary, path)
                finally:
                    if temporary:
                        temporary.unlink(missing_ok=True)
                key = str(path), backend_url, _fingerprint(path.stat())
                self._memory = key, cached_at, programs
                return programs

    def invalidate(self, app) -> None:
        path = Path(app.config["MYTHTV_RECORDINGS_CACHE_PATH"])
        with self._lock:
            self._memory = None
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_suffix(f"{path.suffix}.lock").open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                path.unlink(missing_ok=True)
