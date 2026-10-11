import subprocess
from types import SimpleNamespace

from mythtv_web.subtitles import SubtitleCache


def application(tmp_path):
    return SimpleNamespace(
        config={
            "MYTHTV_SUBTITLE_CACHE_DIR": str(tmp_path),
            "MYTHTV_FFMPEG_THREADS": 2,
        }
    )


def extractor(monkeypatch, returncode=0):
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, returncode, b"WEBVTT\n\n", b"")

    monkeypatch.setattr("mythtv_web.subtitles.subprocess.run", run)
    return commands


def test_completed_subtitles_are_shared_and_file_changes_invalidate(
    tmp_path, monkeypatch
):
    commands = extractor(monkeypatch)
    app = application(tmp_path)
    program = {"Recording": {"StatusName": "Recorded", "FileSize": 100}}
    first, second = SubtitleCache(), SubtitleCache()
    assert (
        first.get(app, program, 42, 3, "ffmpeg", "http://backend/recording")
        == b"WEBVTT\n\n"
    )
    assert (
        second.get(app, program, 42, 3, "ffmpeg", "http://backend/recording")
        == b"WEBVTT\n\n"
    )
    assert len(commands) == 1
    program["Recording"]["FileSize"] = 200
    first.get(app, program, 42, 3, "ffmpeg", "http://backend/recording")
    assert len(commands) == 2


def test_live_recordings_and_failed_extractions_are_not_cached(tmp_path, monkeypatch):
    commands = extractor(monkeypatch)
    cache = SubtitleCache()
    app = application(tmp_path)
    live = {"Recording": {"StatusName": "Recording"}}
    cache.get(app, live, 42, 3, "ffmpeg", "http://backend/recording")
    cache.get(app, live, 42, 3, "ffmpeg", "http://backend/recording")
    assert len(commands) == 2
    assert list(tmp_path.glob("*.vtt")) == []
    failed_commands = extractor(monkeypatch, returncode=1)
    assert cache.get(app, {}, 42, 3, "ffmpeg", "http://backend/recording") is None
    assert cache.get(app, {}, 42, 3, "ffmpeg", "http://backend/recording") is None
    assert len(failed_commands) == 2


def test_subtitle_cache_retention_is_bounded_and_expired_tracks_are_rebuilt(
    tmp_path, monkeypatch
):
    commands = extractor(monkeypatch)
    cache = SubtitleCache(keep=2)
    app = application(tmp_path)
    for stream in range(3):
        cache.get(app, {}, 42, stream, "ffmpeg", "http://backend/recording")
    assert len(list(tmp_path.glob("*.vtt"))) == 2
    future = max(path.stat().st_mtime for path in tmp_path.glob("*.vtt")) + 86401
    monkeypatch.setattr("mythtv_web.subtitles.time.time", lambda: future)
    cache.get(app, {}, 42, 2, "ffmpeg", "http://backend/recording")
    assert len(commands) == 4


def test_requests_for_the_same_track_share_one_extraction(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    commands = extractor(monkeypatch)
    app = application(tmp_path)

    def get(_index):
        return SubtitleCache().get(app, {}, 42, 3, "ffmpeg", "http://backend/recording")

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(get, range(2))) == [b"WEBVTT\n\n", b"WEBVTT\n\n"]
    assert len(commands) == 1
