import json
import os
import signal
import sqlite3
import subprocess
import time

import pytest
import requests
from flask import Flask

from mythtv_web import create_app


@pytest.fixture(autouse=True)
def authenticated_api_clients(monkeypatch, tmp_path):
    monkeypatch.setenv("MYTHTV_AUTH_DATABASE", str(tmp_path / "users.sqlite"))
    monkeypatch.setenv("MYTHTV_SUBTITLE_CACHE_DIR", str(tmp_path / "subtitles"))
    original_test_client = Flask.test_client

    def test_client(app, *args, **kwargs):
        client = original_test_client(app, *args, **kwargs)
        with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
            connection.execute("UPDATE users SET must_change_password = 0 WHERE id = 1")
            token = connection.execute(
                "SELECT session_token FROM users WHERE id = 1"
            ).fetchone()[0]
        with client.session_transaction() as user_session:
            user_session["user_id"] = 1
            user_session["session_token"] = token
        return client

    monkeypatch.setattr(Flask, "test_client", test_client)


def test_home_renders_recordings_library():
    response = create_app().test_client().get("/")

    assert response.status_code == 200
    assert b"Recordings" in response.data
    assert b"MythTV Web" in response.data
    assert b"MYTHTV" not in response.data
    assert b"MYTH BACKEND" not in response.data
    assert b"MythTV Backend" in response.data
    assert b"/static/mythtv.png" in response.data
    assert all(
        f'data-view="{view}"'.encode() in response.data
        for view in ("recordings", "videos", "live", "upcoming", "guide", "status")
    )
    assert b"/static/build/library.js" in response.data
    assert b"browser-wasm-test" not in response.data
    assert b"browser-wasm-stream" not in response.data
    assert response.headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert response.headers["Cross-Origin-Embedder-Policy"] == "require-corp"


def test_videos_are_proxied_from_mythtv_video_service(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "VideoMetadataInfoList": {
                    "VideoMetadataInfos": [{"Id": 17, "Title": "Local film"}],
                    "TotalAvailable": 1,
                    "AsOf": "2026-10-11T00:00:00Z",
                }
            }

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    response = create_app().test_client().get("/api/videos")

    assert response.status_code == 200
    assert response.json["videos"] == [{"Id": 17, "Title": "Local film"}]
    assert response.json["total"] == 1
    assert captured["url"] == "http://127.0.0.1:6544/Video/GetVideoList"
    assert captured["params"] == {
        "Sort": "title",
        "StartIndex": 0,
        "Count": 0,
        "CollapseSubDirs": "false",
    }


def test_video_stream_forwards_byte_range_to_mythtv(monkeypatch):
    captured = {}

    class Response:
        status_code = 206
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": "4",
            "Content-Range": "bytes 0-3/8",
            "Accept-Ranges": "bytes",
        }

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"test"

        def close(self):
            pass

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    response = (
        create_app()
        .test_client()
        .get("/api/videos/17/stream", headers={"Range": "bytes=0-3"})
    )

    assert response.status_code == 206
    assert response.data == b"test"
    assert response.headers["Content-Range"] == "bytes 0-3/8"
    assert captured["url"] == "http://127.0.0.1:6544/Content/GetVideo"
    assert captured["params"] == {"Id": 17}
    assert captured["headers"] == {"Range": "bytes=0-3"}
    assert captured["stream"] is True


def test_video_playback_info_converts_mythtv_frames_to_seconds(monkeypatch):
    requested = []

    class Response:
        def __init__(self, frames):
            self.frames = frames

        def raise_for_status(self):
            pass

        def json(self):
            return {"long": str(self.frames)}

    def fake_get(url, **kwargs):
        requested.append((url, kwargs))
        return Response(750 if url.endswith("GetSavedBookmark") else 1500)

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    monkeypatch.setattr(
        "mythtv_web._probe_video",
        lambda _app, _video_id: {
            "frame_rate": 25.0,
            "duration": 120.0,
            "subtitles": [{"index": 2, "language": "eng", "title": "English"}],
        },
    )

    response = create_app().test_client().get("/api/videos/17/playback")

    assert response.status_code == 200
    assert response.json == {
        "duration": 120.0,
        "frame_rate": 25.0,
        "last_position": 60.0,
        "bookmark": 30.0,
        "subtitles": [{"index": 2, "language": "eng", "title": "English"}],
    }
    assert [call[0].rsplit("/", 1)[-1] for call in requested] == [
        "GetSavedBookmark",
        "GetLastPlayPos",
    ]


def test_video_position_saves_seconds_as_mythtv_frames(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bool": True}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    monkeypatch.setattr(
        "mythtv_web._probe_video",
        lambda _app, _video_id: {"frame_rate": 25.0},
    )

    response = (
        create_app()
        .test_client()
        .post("/api/videos/17/position", json={"position": 45.5})
    )

    assert response.status_code == 200
    assert response.json == {"saved": True}
    assert captured["url"] == "http://127.0.0.1:6544/Video/SetLastPlayPos"
    assert captured["params"] == {"Id": 17, "Offset": 1138}


def test_video_preview_extracts_and_caches_thumbnail(monkeypatch, tmp_path):
    app = create_app({"MYTHTV_VIDEO_THUMBNAIL_DIR": str(tmp_path)})
    captured = []

    monkeypatch.setattr("mythtv_web.shutil.which", lambda command: "/usr/bin/ffmpeg")

    def fake_run(command, **kwargs):
        captured.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=b"jpeg-thumbnail")

    monkeypatch.setattr("mythtv_web.subprocess.run", fake_run)
    client = app.test_client()

    response = client.get("/api/video-preview/31390")
    cached_response = client.get("/api/video-preview/31390")

    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.data == b"jpeg-thumbnail"
    assert response.headers["Cache-Control"] == "public, max-age=86400"
    assert cached_response.data == b"jpeg-thumbnail"
    assert len(captured) == 1
    assert "http://127.0.0.1:6544/Content/GetVideo?Id=31390" in captured[0][0]
    assert captured[0][0][captured[0][0].index("-ss") + 1] == "15"


def test_video_preview_rejects_zero_id_without_running_ffmpeg(monkeypatch):
    monkeypatch.setattr(
        "mythtv_web.shutil.which",
        lambda command: (_ for _ in ()).throw(AssertionError("ffmpeg should not run")),
    )

    response = create_app().test_client().get("/api/video-preview/0")

    assert response.status_code == 400


def test_video_watched_status_uses_mythtv_video_service(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bool": True}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    response = (
        create_app()
        .test_client()
        .post("/api/videos/17/watched", json={"watched": True})
    )

    assert response.status_code == 200
    assert response.json == {"watched": True}
    assert captured["url"] == ("http://127.0.0.1:6544/Video/UpdateVideoWatchedStatus")
    assert captured["json"] == {"Id": 17, "Watched": True}


def test_home_uses_forwarded_prefix_for_assets_and_navigation():
    response = (
        create_app()
        .test_client()
        .get("/", headers={"X-Forwarded-Prefix": "/mythtv-web"})
    )

    assert response.status_code == 200
    assert b'content="/mythtv-web"' in response.data
    assert b'href="/mythtv-web/static/library.css"' in response.data
    assert b'href="/mythtv-web/"' in response.data
    assert b'src="/mythtv-web/static/build/library.js"' in response.data


def test_home_includes_accessible_fullscreen_playback_controls():
    response = create_app().test_client().get("/")

    assert response.status_code == 200
    assert b'data-video-mode="all"' in response.data
    assert b'data-video-mode="folders"' in response.data
    assert b'id="video-breadcrumb"' in response.data
    assert b'id="jump-back-ten-minutes"' in response.data
    assert b'id="jump-forward-ten-minutes"' in response.data
    assert b'id="player-toggle"' in response.data
    assert b'id="player-mute"' in response.data
    assert b'aria-label="Mute"' in response.data
    assert b'id="player-volume" type="range"' in response.data
    assert b'aria-label="Volume"' in response.data
    assert b'id="player-record-program"' not in response.data
    assert (
        b'id="skip-ad"' in response.data
        and b'id="skip-ad" type="button"' in response.data
    )
    assert b'id="player-exit-fullscreen"' in response.data
    assert b'aria-label="Exit fullscreen"' in response.data
    speed_options = response.data.split(b'id="playback-speed"', 1)[1].split(
        b"</select>", 1
    )[0]
    expected_speeds = [
        "0.5",
        "0.6",
        "0.7",
        "0.8",
        "0.9",
        "1.0",
        "1.05",
        "1.1",
        "1.15",
        "1.2",
        "1.25",
        "1.3",
        "1.4",
        "1.5",
        "1.6",
        "1.7",
        "1.8",
        "1.9",
        "2.0",
    ]
    assert speed_options.count(b"<option") == len(expected_speeds)
    assert all(
        f'value="{speed}"'.encode() in speed_options for speed in expected_speeds
    )
    assert b'value="1.0" selected>1.0' in speed_options
    control_order = [
        response.data.index(f'id="{control}"'.encode())
        for control in (
            "jump-back-ten-minutes",
            "jump-back",
            "player-toggle",
            "jump-forward",
            "jump-forward-ten-minutes",
        )
    ]
    assert control_order == sorted(control_order)
    assert (
        b'id="skip-ad" type="button" aria-label="Skip commercial break" title="Skip to the end of the next commercial break" disabled hidden'
        in response.data
    )


def test_backend_status_is_proxied_as_json(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"BackendStatus": {"Version": "36.0", "Encoders": []}}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    response = create_app().test_client().get("/api/backend-status")

    assert response.status_code == 200
    assert response.json["status"]["Version"] == "36.0"
    assert captured["url"] == "http://127.0.0.1:6544/Status/GetBackendStatus"
    assert captured["headers"]["Accept"] == "application/json"


def test_upcoming_recordings_are_proxied_with_pagination(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramList": {
                    "Programs": [{"Title": "Scheduled program"}],
                    "StartIndex": 5,
                    "Count": 1,
                    "TotalAvailable": 11,
                }
            }

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    response = create_app().test_client().get("/api/upcoming?start=5&count=1")

    assert response.status_code == 200
    assert response.json == {
        "programs": [{"Title": "Scheduled program"}],
        "start": 5,
        "count": 1,
        "total": 11,
    }
    assert captured["url"] == "http://127.0.0.1:6544/Dvr/GetUpcomingList"
    assert captured["params"] == {"StartIndex": 5, "Count": 1}


def test_guide_is_proxied_for_a_valid_window(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramGuide": {
                    "Channels": [{"ChanId": 1001, "Programs": []}],
                    "StartTime": "2026-10-04T15:00:00Z",
                    "EndTime": "2026-10-04T16:00:00Z",
                    "TotalAvailable": 1,
                }
            }

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    response = (
        create_app()
        .test_client()
        .get("/api/guide?start=2026-10-04T15:00:00Z&end=2026-10-04T16:00:00Z")
    )

    assert response.status_code == 200
    assert response.json["channels"] == [{"ChanId": 1001, "Programs": []}]
    assert captured["url"] == "http://127.0.0.1:6544/Guide/GetProgramGuide"
    assert captured["params"] == {
        "StartTime": "2026-10-04T15:00:00Z",
        "EndTime": "2026-10-04T16:00:00Z",
    }


def test_channel_icon_is_proxied_from_mythtv(monkeypatch):
    captured = {}

    class Response:
        content = b"icon bytes"
        headers = {"Content-Type": "image/png"}

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"
    response = app.test_client().get("/api/channel-icon?filename=one_ca.png")

    assert response.status_code == 200
    assert response.data == b"icon bytes"
    assert response.content_type == "image/png"
    assert captured["url"] == "http://backend:6544/Guide/GetChannelIcon"
    assert captured["params"] == {"FileName": "one_ca.png"}


def test_channel_icon_proxy_rejects_path_traversal(monkeypatch):
    def unexpected_get(*args, **kwargs):
        raise AssertionError("invalid icon paths must not reach MythTV")

    monkeypatch.setattr("mythtv_web.requests.get", unexpected_get)
    response = (
        create_app().test_client().get("/api/channel-icon?filename=..%2Fsecret.png")
    )

    assert response.status_code == 400


def test_recording_artwork_proxy_accepts_mythtv_artwork_urls(monkeypatch):
    captured = {}

    class Response:
        content = b"artwork bytes"
        headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"
    image_url = (
        "/Content/GetRecordingArtwork?InetRef=tt1234&Type=coverart&Season=2"
        "&Width=320&Height=480"
    )
    response = app.test_client().get(
        f"/api/artwork?url={image_url.replace('&', '%26')}"
    )

    assert response.status_code == 200
    assert response.data == b"artwork bytes"
    assert captured["url"] == "http://backend:6544/Content/GetRecordingArtwork"
    assert captured["params"] == {
        "InetRef": "tt1234",
        "Type": "coverart",
        "Season": "2",
    }


def test_guide_rejects_a_window_over_six_hours(monkeypatch):
    def unexpected_get(*args, **kwargs):
        raise AssertionError("invalid guide ranges must not reach MythTV")

    monkeypatch.setattr("mythtv_web.requests.get", unexpected_get)
    response = (
        create_app()
        .test_client()
        .get("/api/guide?start=2026-10-04T00:00:00Z&end=2026-10-04T07:00:00Z")
    )

    assert response.status_code == 400


def test_schedule_rejects_non_string_start_time(monkeypatch):
    def unexpected_get(*args, **kwargs):
        raise AssertionError("invalid schedule times must not reach MythTV")

    monkeypatch.setattr("mythtv_web.requests.get", unexpected_get)
    response = (
        create_app()
        .test_client()
        .post(
            "/api/schedules",
            json={"channel_id": 1001, "start_time": 12345},
        )
    )

    assert response.status_code == 400


def test_schedule_forwards_options_and_defaults_to_record_one(monkeypatch):
    captured = {"calls": []}

    class LookupResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "RecRule": {
                    "Id": 0,
                    "Title": "Example",
                    "ChanId": 1001,
                    "CallSign": "10 HD",
                }
            }

    class CreateResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"uint": 314}

    def fake_get(url, **kwargs):
        captured["calls"].append(("get", url, kwargs))
        return LookupResponse()

    def fake_post(url, **kwargs):
        captured["calls"].append(("post", url, kwargs))
        return CreateResponse()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    client = create_app().test_client()
    response = client.post(
        "/api/schedules",
        json={
            "channel_id": 1001,
            "start_time": "2026-10-04T15:00:00Z",
            "rule": {"Type": "Record All", "RecPriority": 7, "Id": 999},
        },
    )
    default_response = client.post(
        "/api/schedules",
        json={"channel_id": 1001, "start_time": "2026-10-04T15:00:00Z"},
    )

    assert response.status_code == 200
    assert response.json == {"scheduled": True, "existing": False, "rule_id": 314}
    assert captured["calls"][1][2]["json"]["Type"] == "Record All"
    assert captured["calls"][1][2]["json"]["RecPriority"] == 7
    assert captured["calls"][1][2]["json"]["Id"] == 0
    assert captured["calls"][1][2]["json"]["ChanId"] == 1001
    assert captured["calls"][1][2]["json"]["Station"] == "10 HD"
    assert "CallSign" not in captured["calls"][1][2]["json"]
    assert captured["calls"][1][1] == "http://127.0.0.1:6544/Dvr/AddRecordSchedule"
    assert default_response.status_code == 200
    assert captured["calls"][3][2]["json"]["Type"] == "Record One"


def test_schedule_options_load_rule_and_mythtv_choices(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("GetRecordSchedule"):
            return Response({"RecRule": {"Id": 1135, "Type": "Record All"}})
        return Response({url.rsplit("/", 1)[-1]: []})

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"
    response = app.test_client().get("/api/schedules/options?record_id=1135")

    assert response.status_code == 200
    assert response.json["rule"] == {"Id": 1135, "Type": "Record All"}
    assert len(calls) == 6
    assert calls[0][1]["params"] == {"RecordId": 1135}
    group_call = next(call for call in calls if call[0].endswith("GetRecGroupList"))
    assert group_call[1]["params"] == {"UsedBy": "schedule"}


def test_schedule_update_posts_mutable_rule_options(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return Response(
            {
                "RecRule": {
                    "Id": 1135,
                    "Type": "Record All",
                    "RecPriority": 1,
                    "CallSign": "10 HD",
                }
            }
        )

    def fake_post(url, **kwargs):
        calls.append(("post", url, kwargs))
        return Response({"bool": True})

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    response = (
        create_app()
        .test_client()
        .post(
            "/api/schedules/1135",
            json={"rule": {"Type": "Record Weekly", "RecPriority": 5}},
        )
    )

    assert response.status_code == 200
    assert response.json == {"updated": True, "rule_id": 1135}
    assert calls[1][1] == "http://127.0.0.1:6544/Dvr/UpdateRecordSchedule"
    assert calls[1][2]["json"]["RecordId"] == 1135
    assert calls[1][2]["json"]["Type"] == "Record Weekly"
    assert calls[1][2]["json"]["RecPriority"] == 5
    assert calls[1][2]["json"]["Station"] == "10 HD"
    assert "CallSign" not in calls[1][2]["json"]


def test_schedule_delete_requires_explicit_confirmation(monkeypatch):
    def unexpected_post(*args, **kwargs):
        raise AssertionError("unconfirmed schedule removal must not reach MythTV")

    monkeypatch.setattr("mythtv_web.requests.post", unexpected_post)
    response = (
        create_app()
        .test_client()
        .post("/api/schedules/1135/delete", json={"confirm": False})
    )

    assert response.status_code == 400


def test_schedule_delete_removes_confirmed_rule_without_app_identity(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bool": True}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    response = (
        create_app()
        .test_client()
        .post("/api/schedules/1135/delete", json={"confirm": True})
    )

    assert response.status_code == 200
    assert response.json == {"deleted": True, "rule_id": 1135}
    assert captured["url"] == "http://127.0.0.1:6544/Dvr/RemoveRecordSchedule"
    assert captured["json"] == {"RecordId": 1135}


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/schedules/1135/delete", {"confirm": True}),
        ("delete", "/api/recordings/39615", {"confirmed": True}),
    ],
)
def test_guest_cannot_delete_recordings_or_rules(monkeypatch, method, path, payload):
    def unexpected_post(*args, **kwargs):
        raise AssertionError("guest deletion must not reach MythTV")

    monkeypatch.setattr("mythtv_web.requests.post", unexpected_post)
    app = create_app()
    client = app.test_client()
    with sqlite3.connect(app.config["AUTH_DATABASE_PATH"]) as connection:
        connection.execute("UPDATE users SET role = 'guest' WHERE id = 1")

    response = getattr(client, method)(path, json=payload)

    assert response.status_code == 403
    assert response.json == {"error": "Guest accounts cannot delete items"}


def test_recording_delete_requires_explicit_confirmation_without_identity(monkeypatch):
    def unexpected_post(*args, **kwargs):
        raise AssertionError("unconfirmed recording deletion must not reach MythTV")

    monkeypatch.setattr("mythtv_web.requests.post", unexpected_post)
    response = (
        create_app()
        .test_client()
        .delete("/api/recordings/39615", json={"confirmed": False})
    )

    assert response.status_code == 400
    assert response.json == {"error": "Explicit confirmation is required"}


def test_confirmed_inkigayo_recording_delete_allows_internal_client(
    monkeypatch, tmp_path
):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bool": True}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(
        json.dumps(
            {
                "cached_at": time.time(),
                "programs": [
                    {
                        "Title": "Inkigayo",
                        "StartTime": "2016-01-01T00:00:59Z",
                        "Recording": {"RecordedId": 39615, "RecGroup": "Seasons"},
                        "Channel": {"ChanId": 814},
                    }
                ],
            }
        )
    )
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().delete(
        "/api/recordings/39615", json={"confirmed": True}
    )

    assert response.status_code == 200
    assert response.json == {"deleted": True}
    assert captured["url"] == "http://127.0.0.1:6544/Dvr/DeleteRecording"
    assert captured["json"] == {
        "RecordedId": 39615,
        "ChanId": 814,
        "StartTime": "2016-01-01T00:00:59Z",
        "ForceDelete": False,
        "AllowRerecord": False,
    }
    assert not cache_path.exists()


def test_program_list_is_proxied_with_pagination(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramList": {
                    "Programs": [{"Title": "Example show"}],
                    "StartIndex": 10,
                    "Count": 1,
                    "TotalAvailable": 25,
                    "AsOf": "2026-10-03T02:00:00Z",
                }
            }

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"
    response = app.test_client().get("/api/programs?start=10&count=1")

    assert response.status_code == 200
    assert response.json == {
        "programs": [{"Title": "Example show"}],
        "start": 10,
        "count": 1,
        "total": 25,
        "as_of": "2026-10-03T02:00:00Z",
    }
    assert captured["url"] == "http://backend:6544/Dvr/GetRecordedList"
    assert captured["params"] == {"StartIndex": 10, "Count": 1, "Descending": "true"}
    assert captured["headers"] == {"Accept": "application/json"}


def test_program_list_reports_unavailable_backend(monkeypatch):
    def failed_get(*args, **kwargs):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr("mythtv_web.requests.get", failed_get)
    response = create_app().test_client().get("/api/programs")

    assert response.status_code == 502
    assert response.json == {"error": "MythTV backend is unavailable"}


def test_program_groups_aggregate_titles_and_reuse_full_list_cache(
    monkeypatch, tmp_path
):
    requests_made = []
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecGroup": "Seasons"},
            "SubTitle": "Episode two",
            "Season": 3,
            "Episode": 7,
            "Description": "A detailed episode description.",
            "StartTime": "2026-10-03T10:00:00Z",
            "ProgramFlagNames": "WATCHED",
            "Channel": {"ChannelName": "Channel 1"},
            "Artwork": {
                "ArtworkInfos": [
                    {
                        "Type": "coverart",
                        "URL": "/Content/GetImageFile?StorageGroup=Coverart&FileName=%2Fcover.jpg",
                    },
                    {
                        "Type": "fanart",
                        "URL": "/Content/GetImageFile?StorageGroup=Fanart&FileName=%2Ffanart.jpg",
                    },
                ]
            },
        },
        {
            "Title": "Example SERIES",
            "Recording": {"RecGroup": "Seasons"},
            "SubTitle": "Episode one",
            "StartTime": "2026-10-02T10:00:00Z",
            "ProgramFlagNames": "NONE",
            "Channel": {"ChannelName": "Channel 1"},
        },
        {
            "Title": "One-off film",
            "Recording": {"RecGroup": "Seasons", "RecordedId": 42},
            "StartTime": "2026-10-01T10:00:00Z",
            "ProgramFlagNames": "WATCHED",
            "Artwork": {
                "ArtworkInfos": [
                    {
                        "Type": "fanart",
                        "URL": "/Content/GetImageFile?StorageGroup=Fanart&FileName=%2Ffilm-fanart.jpg",
                    }
                ]
            },
        },
        {
            "Title": "8 Out Of 10 Cats Does Countdown",
            "Recording": {"RecGroup": "Default"},
            "SubTitle": "A numbered-title recording",
        },
        {
            "Title": "Deleted series",
            "Recording": {"RecGroup": "Deleted"},
        },
    ]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramList": {
                    "Programs": programs,
                    "Count": len(programs),
                    "TotalAvailable": len(programs),
                }
            }

    def fake_get(url, **kwargs):
        requests_made.append((url, kwargs))
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(tmp_path / "recordings.json")
    client = app.test_client()

    groups_response = client.get("/api/program-groups")
    detail_response = client.get("/api/program-group?title=Example%20series")

    assert groups_response.status_code == 200
    assert groups_response.json["total"] == 4
    assert [group["title"] for group in groups_response.json["groups"]] == [
        "8 Out Of 10 Cats Does Countdown",
        "Example series",
        "One-off film",
    ]
    series_group = next(
        group
        for group in groups_response.json["groups"]
        if group["title"] == "Example series"
    )
    assert series_group["recording_count"] == 2
    assert series_group["watched_count"] == 1
    assert series_group["artwork_url"] == (
        "/Content/GetImageFile?StorageGroup=Coverart&FileName=%2Fcover.jpg"
    )
    assert series_group["artwork_type"] == "coverart"
    film_group = next(
        group
        for group in groups_response.json["groups"]
        if group["title"] == "One-off film"
    )
    assert film_group["artwork_url"] == (
        "/Content/GetImageFile?StorageGroup=Fanart&FileName=%2Ffilm-fanart.jpg"
    )
    assert film_group["artwork_type"] == "fanart"
    assert film_group["preview_url"] == "/api/preview?recorded_id=42"
    assert (
        next(
            group
            for group in groups_response.json["groups"]
            if group["title"] == "8 Out Of 10 Cats Does Countdown"
        )["recording_count"]
        == 1
    )
    assert detail_response.status_code == 200
    assert detail_response.json["total"] == 2
    assert [program["SubTitle"] for program in detail_response.json["programs"]] == [
        "Episode one",
        "Episode two",
    ]
    assert detail_response.json["programs"][1]["Season"] == 3
    assert detail_response.json["programs"][1]["Episode"] == 7
    assert detail_response.json["programs"][1]["Description"] == (
        "A detailed episode description."
    )
    assert len(requests_made) == 1
    assert requests_made[0][1]["params"]["Count"] == 5000


def test_program_group_requires_a_title():
    response = create_app().test_client().get("/api/program-group")

    assert response.status_code == 400
    assert response.json == {"error": "A program title is required"}


def test_program_groups_sort_naturally_and_ignore_leading_articles(
    monkeypatch, tmp_path
):
    titles = ["The 10 Ball", "An Banana", "A 2 Ball", "The Apple"]
    programs = [
        {"Title": title, "Recording": {"RecGroup": "Seasons"}} for title in titles
    ]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ProgramList": {
                    "Programs": programs,
                    "Count": len(programs),
                    "TotalAvailable": len(programs),
                }
            }

    monkeypatch.setattr("mythtv_web.requests.get", lambda *args, **kwargs: Response())
    app = create_app()
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(tmp_path / "recordings.json")

    response = app.test_client().get("/api/program-groups")

    assert [group["title"] for group in response.json["groups"]] == [
        "A 2 Ball",
        "The 10 Ball",
        "The Apple",
        "An Banana",
    ]


def test_preview_proxy_uses_mythtv_recording_preview_endpoint(monkeypatch):
    captured = {}

    class Response:
        content = b"preview image"
        headers = {"Content-Type": "image/jpeg"}

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"

    response = app.test_client().get("/api/preview?recorded_id=935")

    assert response.status_code == 200
    assert response.data == b"preview image"
    assert response.content_type == "image/jpeg"
    assert captured["url"] == "http://backend:6544/Content/GetPreviewImage"
    assert captured["params"] == {"RecordedId": 935, "Width": 640, "Format": "JPG"}


def test_preview_proxy_rejects_invalid_recording_id(monkeypatch):
    def unexpected_get(*args, **kwargs):
        raise AssertionError("backend should not be called for invalid recording IDs")

    monkeypatch.setattr("mythtv_web.requests.get", unexpected_get)

    response = create_app().test_client().get("/api/preview?recorded_id=-1")

    assert response.status_code == 400
    assert response.json == {"error": "A valid recording ID is required"}


def test_cheap_seats_playback_normalizes_mythtv_markers_and_resume_positions(
    monkeypatch, tmp_path
):
    programs = [
        {
            "Title": "The Cheap Seats",
            "Recording": {"RecordedId": 39599, "RecGroup": "Default"},
            "Channel": {"ChanId": 1001},
            "StartTime": "2026-09-29T10:36:01Z",
        }
    ]
    requests_made = []
    monkeypatch.setattr(
        "mythtv_web.shutil.which",
        lambda program: "/usr/bin/ffprobe" if program == "ffprobe" else None,
    )
    monkeypatch.setattr(
        "mythtv_web.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            '{"streams": [{"index": 0, "codec_type": "video", '
            '"codec_name": "h264", "profile": "High", "level": 40, '
            '"pix_fmt": "yuv420p", "avg_frame_rate": "25/1"}, '
            '{"index": 3, "codec_type": "subtitle", '
            '"codec_name": "subrip", "tags": {"language": "eng"}}]}',
            "",
        ),
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        requests_made.append((url, kwargs))
        if url.endswith("GetRecordedCommBreak"):
            return Response(
                {
                    "CutList": {
                        "Cuttings": [
                            {"Mark": 4, "Offset": 682600},
                            {"Mark": 5, "Offset": 831560},
                            {"Mark": 4, "Offset": 1318960},
                            {"Mark": 5, "Offset": 1492200},
                        ]
                    }
                }
            )
        if url.endswith("GetRecordedCutList"):
            return Response({"CutList": {"Cuttings": []}})
        if url.endswith("GetLastPlayPos"):
            offset_type = kwargs["params"].get("OffsetType")
            return Response({"long": "-1" if offset_type == "Duration" else "1125"})
        if url.endswith("GetSavedBookmark"):
            offset_type = kwargs["params"].get("OffsetType")
            if offset_type == "Duration":
                return Response({"long": "-1"})
            return Response({"long": "1500"})
        raise AssertionError(f"Unexpected MythTV request: {url}")

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().get("/api/playback/39599")

    assert response.status_code == 200
    assert response.json["comm_breaks"] == [
        {"Start": 682600, "End": 831560},
        {"Start": 1318960, "End": 1492200},
    ]
    assert response.json["cut_list"] == []
    assert response.json["last_position"] == 45000
    assert response.json["bookmark"] == 60000
    assert response.json["media"]["video_frame_rate"] == 25
    assert response.json["media"]["video_profile"] == "High"
    assert response.json["media"]["video_level"] == 40
    assert response.json["media"]["video_pixel_format"] == "yuv420p"
    assert response.json["media"]["subtitles"] == [
        {"index": 3, "codec": "subrip", "language": "eng", "title": ""}
    ]
    assert len(requests_made) == 6
    assert all(call[1]["params"]["ChanId"] == 1001 for call in requests_made)
    assert requests_made[4][1]["params"]["OffsetType"] == "Position"
    assert requests_made[5][1]["params"]["OffsetType"] == "Position"


def test_live_playback_status_refreshes_end_time_and_commercial_markers(
    monkeypatch, tmp_path
):
    programs = [
        {
            "Title": "Example series",
            "StartTime": "2026-10-03T10:00:00Z",
            "EndTime": "2026-10-03T11:00:00Z",
            "Recording": {
                "RecordedId": 42,
                "RecGroup": "Seasons",
                "StatusName": "Recording",
            },
            "Channel": {"ChanId": 123},
        }
    ]

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    requests_made = []

    def fake_get(url, **kwargs):
        requests_made.append((url, kwargs))
        if url.endswith("Dvr/GetRecorded"):
            return Response(
                {
                    "Program": {
                        "EndTime": "2026-10-03T11:20:00Z",
                        "Recording": {"StatusName": "Recording"},
                    }
                }
            )
        if url.endswith("Dvr/GetRecordedCommBreak"):
            return Response(
                {
                    "CutList": {
                        "Cuttings": [
                            {"Mark": 4, "Offset": 120000},
                            {"Mark": 5, "Offset": 180000},
                        ]
                    }
                }
            )
        raise AssertionError(f"Unexpected MythTV request: {url}")

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().get("/api/playback/42/live-status")

    assert response.status_code == 200
    assert response.json == {
        "in_progress": True,
        "end_time": "2026-10-03T11:20:00Z",
        "comm_breaks": [{"Start": 120000, "End": 180000}],
    }
    assert requests_made[0][1]["params"] == {"RecordedId": 42}
    assert requests_made[1][1]["params"] == {
        "RecordedId": 42,
        "OffsetType": "Duration",
        "IncludeFps": "false",
    }


def test_recording_stream_forwards_byte_range(monkeypatch, tmp_path):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
        }
    ]
    captured = {}

    class Response:
        status_code = 206
        headers = {
            "Content-Type": "video/x-matroska",
            "Content-Range": "bytes 0-2/100",
            "Content-Length": "3",
            "Accept-Ranges": "bytes",
        }

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"abc"

        def close(self):
            captured["closed"] = True

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().get(
        "/api/playback/42/stream", headers={"Range": "bytes=0-2"}
    )

    assert response.status_code == 206
    assert response.data == b"abc"
    assert response.headers["Content-Range"] == "bytes 0-2/100"
    assert captured["headers"] == {"Range": "bytes=0-2"}
    assert captured["url"] == "http://127.0.0.1:6544/Content/GetRecording?RecordedId=42"
    assert captured["closed"]


def test_playback_position_saves_duration_milliseconds(monkeypatch, tmp_path):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
            "Channel": {"ChanId": 123},
            "StartTime": "2026-10-03T10:00:00Z",
        }
    ]
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bool": True}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().post(
        "/api/playback/42/position", json={"position": 45.5}
    )

    assert response.status_code == 200
    assert response.json == {"saved": True}
    assert captured["url"] == "http://127.0.0.1:6544/Dvr/SetLastPlayPos"
    assert captured["params"]["Offset"] == 45500
    assert captured["params"]["OffsetType"] == "Duration"


def test_playback_position_reports_mythtv_rejection(monkeypatch, tmp_path):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
            "Channel": {"ChanId": 123},
            "StartTime": "2026-10-03T10:00:00Z",
        }
    ]

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bool": False}

    monkeypatch.setattr("mythtv_web.requests.post", lambda *args, **kwargs: Response())
    monkeypatch.setattr(
        "mythtv_web._probe_recording",
        lambda *args, **kwargs: {"video_frame_rate": None},
    )
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().post(
        "/api/playback/42/position", json={"position": 45.5}
    )

    assert response.status_code == 502
    assert response.json == {"error": "MythTV did not save playback position"}


def test_playback_position_falls_back_to_raw_frames_without_seek_table(
    monkeypatch, tmp_path
):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
            "Channel": {"ChanId": 123},
            "StartTime": "2026-10-03T10:00:00Z",
        }
    ]
    responses = iter([{"bool": False}, {"bool": True}])
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return next(responses)

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    monkeypatch.setattr(
        "mythtv_web._probe_recording",
        lambda *args, **kwargs: {"video_frame_rate": 25},
    )
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().post(
        "/api/playback/42/position", json={"position": 45.5}
    )

    assert response.status_code == 200
    assert response.json == {"saved": True}
    assert calls[0][1]["params"]["OffsetType"] == "Duration"
    assert calls[0][1]["params"]["Offset"] == 45500
    assert calls[1][1]["params"]["Offset"] == 1138
    assert "OffsetType" not in calls[1][1]["params"]


def test_playback_bookmark_saves_mythtv_saved_bookmark(monkeypatch, tmp_path):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
            "Channel": {"ChanId": 123},
            "StartTime": "2026-10-03T10:00:00Z",
        }
    ]
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"bool": True}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.post", fake_post)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)

    response = app.test_client().post(
        "/api/playback/42/bookmark", json={"position": 45.5}
    )

    assert response.status_code == 200
    assert response.json == {"saved": True}
    assert captured["url"] == "http://127.0.0.1:6544/Dvr/SetSavedBookmark"
    assert captured["params"] == {
        "RecordedId": 42,
        "ChanId": 123,
        "StartTime": "2026-10-03T10:00:00Z",
        "OffsetType": "Duration",
        "Offset": 45500,
    }


def test_transcode_cancel_terminates_only_a_tokenized_process(monkeypatch, tmp_path):
    token = "a" * 32
    transcode_dir = tmp_path / "transcodes"
    transcode_dir.mkdir()
    captured = {}

    def fake_signal(descriptor, process_signal):
        captured["descriptor"] = descriptor
        captured["signal"] = process_signal

    identity = {"start": "123", "boot": "boot", "executable": "/usr/bin/ffmpeg"}
    monkeypatch.setattr("mythtv_web.transcodes._process_identity", lambda _: identity)
    descriptor = os.open("/dev/null", os.O_RDONLY)
    monkeypatch.setattr("mythtv_web.transcodes.os.pidfd_open", lambda _: descriptor)
    monkeypatch.setattr("mythtv_web.transcodes.signal.pidfd_send_signal", fake_signal)
    app = create_app()
    app.config["MYTHTV_TRANSCODE_DIR"] = str(transcode_dir)
    client = app.test_client()
    with client.session_transaction() as saved:
        owner_token = saved["session_token"]
    (transcode_dir / f"{token}.json").write_text(
        json.dumps(
            {
                "pid": 4321,
                "identity": identity,
                "recorded_id": 42,
                "user_id": 1,
                "session_token": owner_token,
            }
        )
    )

    response = client.post("/api/playback/42/transcode/cancel", json={"token": token})
    invalid_response = client.post(
        "/api/playback/42/transcode/cancel", json={"token": "../../etc/passwd"}
    )

    assert response.status_code == 200
    assert response.json == {"cancelled": True}
    assert captured == {"descriptor": descriptor, "signal": signal.SIGTERM}
    assert invalid_response.status_code == 400


def test_transcode_stream_removes_process_registration_after_completion(
    monkeypatch, tmp_path
):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
        }
    ]
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    token = "b" * 32

    class Stdout:
        chunks = [b"fragment", b""]

        def read1(self, _size):
            return self.chunks.pop(0)

        def close(self):
            pass

    class Process:
        pid = 9876
        stdout = Stdout()

        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    process = Process()
    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr("mythtv_web.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr("mythtv_web.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "mythtv_web.transcodes._process_identity", lambda _: {"start": "123"}
    )
    app = create_app()
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)
    app.config["MYTHTV_TRANSCODE_DIR"] = str(tmp_path / "transcodes")

    response = app.test_client().get(
        f"/api/playback/42/transcode?start=123.5&token={token}", buffered=False
    )
    process_path = tmp_path / "transcodes" / f"{token}.json"

    assert response.status_code == 200
    assert commands[0][commands[0].index("-ss") + 1] == "123.5"
    assert commands[0][commands[0].index("-readrate") + 1] == "2"
    assert commands[0][commands[0].index("-readrate_initial_burst") + 1] == "2"
    assert json.loads(process_path.read_text())["pid"] == 9876
    assert commands[0][commands[0].index("-threads:v") + 1] == "2"
    assert b"".join(response.response) == b"fragment"
    assert process.terminated
    assert not process_path.exists()


def test_video_transcode_streams_mythtv_video_as_mp4(monkeypatch, tmp_path):
    token = "c" * 32

    class Stdout:
        chunks = [b"fragment", b""]

        def read1(self, _size):
            return self.chunks.pop(0)

        def close(self):
            pass

    class Process:
        pid = 8765
        stdout = Stdout()

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    process = Process()
    commands = []

    def fake_popen(command, **kwargs):
        commands.append(command)
        return process

    monkeypatch.setattr("mythtv_web.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr("mythtv_web.subprocess.Popen", fake_popen)
    monkeypatch.setattr(
        "mythtv_web.transcodes._process_identity", lambda _: {"start": "123"}
    )
    app = create_app()
    app.config["MYTHTV_TRANSCODE_DIR"] = str(tmp_path / "transcodes")

    response = app.test_client().get(
        f"/api/videos/16509/transcode?start=12.5&token={token}", buffered=False
    )
    process_path = tmp_path / "transcodes" / f"{token}.json"

    assert response.status_code == 200
    assert response.content_type == "video/mp4"
    assert commands[0][commands[0].index("-ss") + 1] == "12.5"
    assert commands[0][commands[0].index("-readrate") + 1] == "2"
    assert commands[0][commands[0].index("-readrate_initial_burst") + 1] == "2"
    assert commands[0][commands[0].index("-i") + 1] == (
        "http://127.0.0.1:6544/Content/GetVideo?Id=16509"
    )
    assert json.loads(process_path.read_text())["pid"] == 8765
    assert b"".join(response.response) == b"fragment"
    assert process.terminated
    assert not process_path.exists()


@pytest.mark.parametrize(
    ("codec", "start", "expected_decoder_options"),
    [
        ("subrip", 0, []),
        ("subrip", 814.3, ["-ss", "814.3"]),
        ("dvb_teletext", 0, ["-fix_sub_duration", "-txt_format", "text"]),
        (
            "dvb_teletext",
            814.3,
            ["-ss", "814.3", "-fix_sub_duration", "-txt_format", "text"],
        ),
    ],
)
def test_subtitle_endpoint_extracts_only_known_subtitle_tracks(
    monkeypatch, tmp_path, codec, start, expected_decoder_options
):
    programs = [
        {
            "Title": "Example series",
            "Recording": {"RecordedId": 42, "RecGroup": "Seasons"},
        }
    ]
    monkeypatch.setattr("mythtv_web.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "mythtv_web._probe_recording",
        lambda app, recorded_id: {
            "video_codec": "hevc",
            "audio_codec": "eac3",
            "subtitles": [
                {
                    "index": 3,
                    "codec": codec,
                    "language": "eng",
                    "title": "English",
                }
            ],
        },
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, b"WEBVTT\n\n", b"")

    monkeypatch.setattr("mythtv_web.subprocess.run", fake_run)
    app = create_app()
    cache_path = tmp_path / "recordings.json"
    cache_path.write_text(json.dumps({"cached_at": time.time(), "programs": programs}))
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(cache_path)
    client = app.test_client()

    response = client.get(f"/api/playback/42/subtitles/3?start={start}")
    invalid_response = client.get("/api/playback/42/subtitles/7")

    assert response.status_code == 200
    assert response.content_type.startswith("text/vtt")
    assert response.data == b"WEBVTT\n\n"
    assert captured["command"][captured["command"].index("-map") + 1] == "0:3"
    input_index = captured["command"].index("-i")
    options_start = captured["command"].index("-threads") + 2
    assert captured["command"][options_start:input_index] == expected_decoder_options
    assert invalid_response.status_code == 404


def test_video_subtitle_endpoint_rebases_teletext_to_transcode_offset(monkeypatch):
    monkeypatch.setattr("mythtv_web.shutil.which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        "mythtv_web._probe_video",
        lambda _app, _video_id: {
            "subtitles": [
                {
                    "index": 2,
                    "codec": "dvb_teletext",
                    "language": "eng",
                    "title": "English",
                }
            ]
        },
    )
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        return subprocess.CompletedProcess(command, 0, b"WEBVTT\n\n", b"")

    monkeypatch.setattr("mythtv_web.subprocess.run", fake_run)

    response = create_app().test_client().get("/api/videos/17/subtitles/2?start=123.4")

    assert response.status_code == 200
    command = captured["command"]
    input_index = command.index("-i")
    assert command[4:input_index] == [
        "-ss",
        "123.4",
        "-fix_sub_duration",
        "-txt_format",
        "text",
    ]
    assert command[input_index + 1] == ("http://127.0.0.1:6544/Content/GetVideo?Id=17")


def test_artwork_proxy_forwards_only_mythtv_image_parameters(monkeypatch):
    captured = {}

    class Response:
        content = b"image bytes"
        headers = {"Content-Type": "image/webp"}

        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("mythtv_web.requests.get", fake_get)
    app = create_app()
    app.config["MYTHTV_BACKEND_URL"] = "http://backend:6544"
    image_url = (
        "/Content/GetImageFile?StorageGroup=Coverart&FileName="
        "%2Fhome%2Fmythtv%2F.mythtv%2FMythVideo%2Fcover.jpg"
    )
    response = app.test_client().get(
        f"/api/artwork?url={image_url.replace('&', '%26')}"
    )

    assert response.status_code == 200
    assert response.data == b"image bytes"
    assert response.content_type == "image/webp"
    assert response.headers["Cache-Control"] == "public, max-age=3600"
    assert captured["url"] == "http://backend:6544/Content/GetImageFile"
    assert captured["params"] == {"StorageGroup": "Coverart", "FileName": "cover.jpg"}


@pytest.mark.parametrize("endpoint", ["position", "bookmark", "transcode/cancel"])
@pytest.mark.parametrize(
    "payload", [[], "text", 1, True, {"token": 1}, {"position": 10**400}]
)
def test_playback_mutations_reject_invalid_json(monkeypatch, endpoint, payload):
    monkeypatch.setattr("mythtv_web._active_recording", lambda *_: {"Recording": {}})
    response = (
        create_app().test_client().post(f"/api/playback/42/{endpoint}", json=payload)
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "result", [{}, [], {"bool": False}, {"bool": "true"}, {"bool": 1}]
)
def test_schedule_update_requires_explicit_backend_success(monkeypatch, result):
    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    monkeypatch.setattr(
        "mythtv_web.requests.get", lambda *_a, **_k: Response({"RecRule": {"Id": 42}})
    )
    monkeypatch.setattr("mythtv_web.requests.post", lambda *_a, **_k: Response(result))
    response = create_app().test_client().post("/api/schedules/42", json={"rule": {}})
    assert response.status_code == 502


@pytest.mark.parametrize(
    "payload", [[], {"ProgramList": []}, {"ProgramList": {"Programs": [None]}}]
)
def test_programs_reject_malformed_backend_lists(monkeypatch, payload):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr("mythtv_web.requests.get", lambda *_a, **_k: Response())
    assert create_app().test_client().get("/api/programs").status_code == 502


def test_artwork_proxy_rejects_arbitrary_upstream_urls(monkeypatch):
    def unexpected_get(*args, **kwargs):
        raise AssertionError("backend should not be called for invalid artwork URLs")

    monkeypatch.setattr("mythtv_web.requests.get", unexpected_get)
    response = (
        create_app()
        .test_client()
        .get("/api/artwork?url=https%3A%2F%2Fevil.invalid%2FContent%2FGetImageFile")
    )

    assert response.status_code == 400
    assert response.json == {"error": "Invalid artwork URL"}
