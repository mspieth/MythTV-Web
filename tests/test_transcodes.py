import json
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from mythtv_web.transcodes import CapacityError, TokenActiveError, TranscodeRegistry

USER = {"id": 1, "session_token": "test-session"}
COMMAND = [sys.executable, "-c", "import time; time.sleep(30)"]


def test_cancellation_requires_owner_recording_and_process_identity(tmp_path):
    registry = TranscodeRegistry(str(tmp_path), 2)
    job = registry.start(COMMAND, "a" * 32, 42, USER)
    try:
        assert not registry.cancel("a" * 32, 43, USER)
        assert not registry.cancel(
            "a" * 32, 42, {"id": 2, "session_token": "test-session"}
        )
        assert not registry.cancel("a" * 32, 42, {"id": 1, "session_token": "revoked"})
        assert job.process.poll() is None
        assert registry.cancel("a" * 32, 42, USER)
        assert job.process.wait(timeout=3) < 0
    finally:
        job.close()
        job.close()
    assert not job.path.exists()


def test_stale_registration_never_signals_an_unrelated_process(tmp_path):
    registry = TranscodeRegistry(str(tmp_path), 1)
    job = registry.start(COMMAND, "b" * 32, 42, USER)
    try:
        forged = {**job.metadata, "identity": {"start": "different"}}
        job.path.write_text(json.dumps(forged))
        assert not registry.cancel("b" * 32, 42, USER)
        assert job.process.poll() is None
    finally:
        job.close()


def test_duplicate_tokens_and_capacity_are_enforced_across_registries(tmp_path):
    first = TranscodeRegistry(str(tmp_path), 1)
    second = TranscodeRegistry(str(tmp_path), 1)
    job = first.start(COMMAND, "c" * 32, 42, USER)
    try:
        with pytest.raises(TokenActiveError):
            second.start(COMMAND, "c" * 32, 42, USER)
        with pytest.raises(CapacityError):
            second.start(COMMAND, "d" * 32, 42, USER)
    finally:
        job.close()
    replacement = second.start(COMMAND, "c" * 32, 42, USER)
    replacement.close()


def test_concurrent_starts_claim_only_one_slot(tmp_path):
    def start(token):
        try:
            return TranscodeRegistry(str(tmp_path), 1).start(COMMAND, token, 42, USER)
        except CapacityError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = list(executor.map(start, ["a" * 32, "b" * 32]))
    try:
        assert sum(job is not None for job in jobs) == 1
    finally:
        for job in jobs:
            if job:
                job.close()


def test_crashed_job_registration_does_not_consume_capacity(tmp_path):
    registry = TranscodeRegistry(str(tmp_path), 1)
    crashed = registry.start(COMMAND, "a" * 32, 42, USER)
    crashed.process.kill()
    crashed.process.wait()
    replacement = registry.start(COMMAND, "b" * 32, 43, USER)
    try:
        assert not crashed.path.exists()
    finally:
        crashed.close()
        replacement.close()
