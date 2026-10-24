import json
import time
from types import SimpleNamespace

import pytest

from mythtv_web.recordings import RecordingsCache


def application(tmp_path):
    return SimpleNamespace(
        config={
            "MYTHTV_RECORDINGS_CACHE_PATH": str(tmp_path / "recordings.json"),
            "MYTHTV_BACKEND_URL": "http://localhost:6544",
            "MYTHTV_GROUPS_TIMEOUT": 30,
        }
    )


def write_cache(tmp_path, programs, age=0):
    temporary = tmp_path / "replacement.json"
    temporary.write_text(
        json.dumps({"cached_at": time.time() - age, "programs": programs})
    )
    temporary.replace(tmp_path / "recordings.json")


def test_worker_reuses_parsed_data_and_notices_atomic_replacement(
    tmp_path, monkeypatch
):
    app = application(tmp_path)
    cache = RecordingsCache()
    write_cache(tmp_path, [{"Title": "First"}])
    first = cache.load(app)
    original = json.load
    reads = []

    def load(stream):
        reads.append(stream.name)
        return original(stream)

    monkeypatch.setattr("mythtv_web.recordings.json.load", load)
    assert cache.load(app) is first
    assert reads == []
    write_cache(tmp_path, [{"Title": "Second"}])
    assert cache.load(app) == [{"Title": "Second"}]
    assert len(reads) == 1


def test_invalidation_and_expiration_are_seen_by_other_workers(tmp_path, monkeypatch):
    app = application(tmp_path)
    first, second = RecordingsCache(), RecordingsCache()
    write_cache(tmp_path, [{"Title": "Old"}])
    first.load(app)
    second.load(app)
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramList": {
                    "Programs": [{"Title": "New"}],
                    "Count": 1,
                    "TotalAvailable": 1,
                }
            }

    def get(*_a, **_k):
        calls.append(True)
        return Response()

    monkeypatch.setattr("mythtv_web.recordings.requests.get", get)
    first.invalidate(app)
    assert second.load(app) == [{"Title": "New"}]
    assert first.load(app) == [{"Title": "New"}]
    assert len(calls) == 1
    write_cache(tmp_path, [{"Title": "Expired"}], age=121)
    assert first.load(app) == [{"Title": "New"}]
    assert len(calls) == 2
    future = time.time() + 121
    monkeypatch.setattr("mythtv_web.recordings.time.time", lambda: future)
    first.load(app)
    assert len(calls) == 3


def test_failed_pagination_does_not_publish_an_incomplete_cache(tmp_path, monkeypatch):
    app = application(tmp_path)

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ProgramList": {"Programs": [], "Count": 0, "TotalAvailable": 10}}

    monkeypatch.setattr(
        "mythtv_web.recordings.requests.get", lambda *_a, **_k: Response()
    )
    with pytest.raises(ValueError, match="incomplete"):
        RecordingsCache().load(app)
    assert not (tmp_path / "recordings.json").exists()


def test_cached_data_is_scoped_to_backend_url(tmp_path, monkeypatch):
    app = application(tmp_path)
    cache = RecordingsCache()
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ProgramList": {"Programs": [], "Count": 0, "TotalAvailable": 0}}

    def get(url, **_kwargs):
        calls.append(url)
        return Response()

    monkeypatch.setattr("mythtv_web.recordings.requests.get", get)
    cache.load(app)
    app.config["MYTHTV_BACKEND_URL"] = "http://other-local-backend:6544"
    cache.load(app)
    assert len(calls) == 2
