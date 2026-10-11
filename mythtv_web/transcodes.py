"""Shared FFmpeg job registration and safe cancellation on Linux."""

import fcntl
import json
import os
import secrets
import signal
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path


class TokenActiveError(Exception):
    pass


class CapacityError(Exception):
    pass


def _process_identity(pid: int) -> dict | None:
    try:
        # The command in parentheses can contain spaces or parentheses.
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return {
            "start": stat[19],
            "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "executable": str(Path(f"/proc/{pid}/exe").resolve(strict=True)),
        }
    except (OSError, IndexError, ValueError):
        return None


def _stop_process(process) -> None:
    try:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()


@dataclass
class Job:
    registry: "TranscodeRegistry"
    path: Path
    metadata: dict
    process: subprocess.Popen
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _finished: bool = False

    def close(self) -> None:
        with self._lock:
            if self._finished:
                return
            self._finished = True
            try:
                _stop_process(self.process)
            finally:
                with self.registry.locked():
                    current = self.registry.read(self.path)
                    if current and current.get("job_id") == self.metadata["job_id"]:
                        self.path.unlink(missing_ok=True)


class TranscodeRegistry:
    def __init__(self, directory: str, limit: int):
        self.directory = Path(directory)
        self.limit = limit

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.directory / "jobs.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    @staticmethod
    def read(path: Path) -> dict | None:
        try:
            result = json.loads(path.read_text())
            if isinstance(result, dict) and type(result.get("pid")) is int:
                return result
        except (OSError, ValueError):
            pass
        return None

    def start(
        self, command: list[str], token: str, recorded_id: int, user: dict
    ) -> Job:
        path = self.directory / f"{token}.json"
        with self.locked():
            active = 0
            for registered in self.directory.glob("*.json"):
                metadata = self.read(registered)
                identity = _process_identity(metadata["pid"]) if metadata else None
                if identity and identity == metadata.get("identity"):
                    active += 1
                    if registered == path:
                        raise TokenActiveError
                else:
                    registered.unlink(missing_ok=True)
            if active >= self.limit:
                raise CapacityError
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            try:
                identity = _process_identity(process.pid)
                if identity is None:
                    raise OSError("Cannot identify the FFmpeg process")
                metadata = {
                    "job_id": secrets.token_hex(16),
                    "pid": process.pid,
                    "identity": identity,
                    "recorded_id": recorded_id,
                    "user_id": user["id"],
                    "session_token": user["session_token"],
                }
                with path.open("x", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(metadata, stream)
                return Job(self, path, metadata, process)
            except Exception:
                _stop_process(process)
                path.unlink(missing_ok=True)
                raise

    def cancel(self, token: str, recorded_id: int, user: dict) -> bool:
        path = self.directory / f"{token}.json"
        with self.locked():
            metadata = self.read(path)
            if not metadata or (
                metadata.get("recorded_id") != recorded_id
                or metadata.get("user_id") != user["id"]
                or metadata.get("session_token") != user["session_token"]
            ):
                return False
            # A pidfd pins this process even if its numerical PID is reused.
            # Never fall back to signalling a bare PID.
            descriptor = os.pidfd_open(metadata["pid"])
            try:
                identity = _process_identity(metadata["pid"])
                if identity is None or identity != metadata.get("identity"):
                    path.unlink(missing_ok=True)
                    return False
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                return True
            finally:
                os.close(descriptor)
