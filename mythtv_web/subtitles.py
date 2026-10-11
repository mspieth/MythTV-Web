"""Bounded, shared caching of extracted text subtitles."""

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

CACHE_SECONDS = 24 * 60 * 60


class SubtitleCache:
    def __init__(self, keep: int = 128):
        self.keep = keep

    @staticmethod
    def _extract(app, ffmpeg: str, source_url: str, stream_index: int) -> bytes | None:
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-threads",
                str(app.config["MYTHTV_FFMPEG_THREADS"]),
                "-i",
                source_url,
                "-map",
                f"0:{stream_index}",
                "-f",
                "webvtt",
                "pipe:1",
            ],
            capture_output=True,
            timeout=120,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None

    @staticmethod
    def _read(path: Path) -> bytes | None:
        try:
            if 0 <= time.time() - path.stat().st_mtime <= CACHE_SECONDS:
                return path.read_bytes()
        except OSError:
            pass
        return None

    def get(
        self,
        app,
        program: dict,
        recorded_id: int,
        stream_index: int,
        ffmpeg: str,
        source_url: str,
    ) -> bytes | None:
        recording = program.get("Recording") or {}
        if str(recording.get("StatusName", "")).casefold() == "recording":
            return self._extract(app, ffmpeg, source_url, stream_index)
        fingerprint = {
            "source": source_url,
            "recorded_id": recorded_id,
            "stream": stream_index,
            "filename": recording.get("FileName") or program.get("FileName"),
            "size": recording.get("FileSize") or program.get("FileSize"),
            "modified": recording.get("LastModified"),
            "start": recording.get("StartTs"),
            "end": recording.get("EndTs"),
        }
        key = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True).encode()
        ).hexdigest()
        directory = Path(app.config["MYTHTV_SUBTITLE_CACHE_DIR"])
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / f"{key}.vtt"
        cached = self._read(path)
        if cached is not None:
            return cached
        # Fixed stripes prevent accumulating a lock file for every recording.
        with (directory / f"{int(key[:2], 16) % 8}.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            cached = self._read(path)
            if cached is not None:
                return cached
            contents = self._extract(app, ffmpeg, source_url, stream_index)
            if contents is None:
                return None
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
                    temporary = Path(stream.name)
                    stream.write(contents)
                os.replace(temporary, path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            self._prune(directory)
            return contents

    def _prune(self, directory: Path) -> None:
        with (directory / "prune.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            files = []
            for path in directory.glob("*.vtt"):
                try:
                    age = time.time() - path.stat().st_mtime
                    if age < 0 or age > CACHE_SECONDS:
                        path.unlink(missing_ok=True)
                    else:
                        files.append((path.stat().st_mtime_ns, path))
                except FileNotFoundError:
                    continue
            for _, path in sorted(files)[: -self.keep]:
                path.unlink(missing_ok=True)
