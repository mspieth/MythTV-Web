import fcntl
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    stream_with_context,
)
from werkzeug.middleware.proxy_fix import ProxyFix

from .auth import deletion_allowed, init_auth

DEFAULT_BACKEND_URL = "http://127.0.0.1:6544"
PRIMARY_RECORDING_GROUP = "Seasons"
ACTIVE_RECORDING_GROUPS = {"Default", PRIMARY_RECORDING_GROUP}
RECORDINGS_PAGE_SIZE = 5000
RECORDINGS_CACHE_SECONDS = 120
RECORDING_RULE_FIELDS = (
    "Title",
    "SubTitle",
    "Description",
    "Category",
    "StartTime",
    "EndTime",
    "SeriesId",
    "ProgramId",
    "ChanId",
    "CallSign",
    "FindDay",
    "FindTime",
    "Inactive",
    "Season",
    "Episode",
    "Inetref",
    "Type",
    "SearchType",
    "RecPriority",
    "PreferredInput",
    "StartOffset",
    "EndOffset",
    "DupMethod",
    "DupIn",
    "NewEpisOnly",
    "Filter",
    "RecProfile",
    "RecGroup",
    "StorageGroup",
    "PlayGroup",
    "AutoExpire",
    "MaxEpisodes",
    "MaxNewest",
    "AutoCommflag",
    "AutoTranscode",
    "AutoMetaLookup",
    "AutoUserJob1",
    "AutoUserJob2",
    "AutoUserJob3",
    "AutoUserJob4",
    "Transcoder",
    "AutoExtend",
)


def _parse_api_time(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _title_sort_key(title: str) -> tuple:
    normalized = re.sub(r"^(?:a|an|the)\s+", "", title.casefold().strip())
    parts = tuple(
        (1, int(part)) if part.isdigit() else (0, part)
        for part in re.split(r"(\d+)", normalized)
    )
    return parts, title.casefold()


def _normalize_group_title(title: str) -> str:
    return " ".join(title.casefold().split())


def _commercial_breaks_from_cuttings(cuttings: list) -> list[dict[str, int]]:
    if not isinstance(cuttings, list):
        return []
    breaks = []
    start = None
    for cutting in cuttings:
        if not isinstance(cutting, dict):
            continue
        try:
            mark = int(cutting.get("Mark"))
            offset = int(cutting.get("Offset"))
        except (TypeError, ValueError):
            continue

        if mark == 4:
            start = offset
        elif mark == 5 and start is not None:
            if offset > start:
                breaks.append({"Start": start, "End": offset})
            start = None
    return breaks


def _recording_preview_url(program: dict) -> str | None:
    recorded_id = (program.get("Recording") or {}).get("RecordedId")
    if not isinstance(recorded_id, int) or recorded_id <= 0:
        return None
    return f"/api/preview?recorded_id={recorded_id}"


def _active_recording(app: Flask, recorded_id: int) -> dict | None:
    return next(
        (
            program
            for program in _load_all_recordings(app)
            if (program.get("Recording") or {}).get("RecordedId") == recorded_id
            and (program.get("Recording") or {}).get("RecGroup")
            in ACTIVE_RECORDING_GROUPS
        ),
        None,
    )


def _invalidate_recordings_cache(app: Flask) -> None:
    cache_path = Path(app.config["MYTHTV_RECORDINGS_CACHE_PATH"])
    lock_path = cache_path.with_suffix(f"{cache_path.suffix}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            cache_path.unlink(missing_ok=True)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _recording_url(app: Flask, recorded_id: int) -> str:
    return f"{app.config['MYTHTV_BACKEND_URL']}/Content/GetRecording?RecordedId={recorded_id}"


@lru_cache(maxsize=128)
def _probe_recording(app: Flask, recorded_id: int) -> dict:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {
            "video_codec": None,
            "video_profile": None,
            "video_level": None,
            "video_pixel_format": None,
            "video_frame_rate": None,
            "audio_codec": None,
            "subtitles": [],
        }
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,profile,level,pix_fmt,avg_frame_rate,r_frame_rate:stream_tags=language,title",
                "-of",
                "json",
                _recording_url(app, recorded_id),
            ],
            capture_output=True,
            timeout=30,
            check=True,
            text=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        app.logger.info("Recording stream probe failed: %s", error)
        return {
            "video_codec": None,
            "video_profile": None,
            "video_level": None,
            "video_pixel_format": None,
            "video_frame_rate": None,
            "audio_codec": None,
            "subtitles": [],
        }

    subtitles = []
    codecs = {
        "video_codec": None,
        "video_profile": None,
        "video_level": None,
        "video_pixel_format": None,
        "video_frame_rate": None,
        "audio_codec": None,
    }
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and codecs["video_codec"] is None:
            codecs["video_codec"] = stream.get("codec_name")
            codecs["video_profile"] = stream.get("profile")
            codecs["video_level"] = stream.get("level")
            codecs["video_pixel_format"] = stream.get("pix_fmt")
            for value in (stream.get("avg_frame_rate"), stream.get("r_frame_rate")):
                try:
                    frame_rate = float(Fraction(value))
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                if math.isfinite(frame_rate) and frame_rate > 0:
                    codecs["video_frame_rate"] = frame_rate
                    break
        elif codec_type == "audio" and codecs["audio_codec"] is None:
            codecs["audio_codec"] = stream.get("codec_name")
        elif codec_type == "subtitle":
            tags = stream.get("tags") or {}
            subtitles.append(
                {
                    "index": stream.get("index"),
                    "codec": stream.get("codec_name"),
                    "language": tags.get("language", "und"),
                    "title": tags.get("title", ""),
                }
            )
    return {**codecs, "subtitles": subtitles}


def _post_playback_offset(
    app: Flask, endpoint: str, params: dict, recorded_id: int, position: float
) -> bool:
    request_options = {
        "headers": {"Accept": "application/json"},
        "timeout": app.config["MYTHTV_TIMEOUT"],
    }
    url = f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/{endpoint}"
    response = requests.post(
        url,
        params={
            **params,
            "OffsetType": "Duration",
            "Offset": round(position * 1000),
        },
        **request_options,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("bool") is not False:
        return isinstance(payload, dict) and payload.get("bool") is True

    frame_rate = _probe_recording(app, recorded_id).get("video_frame_rate")
    if not frame_rate:
        return False
    response = requests.post(
        url,
        params={**params, "Offset": round(position * frame_rate)},
        **request_options,
    )
    response.raise_for_status()
    payload = response.json()
    return isinstance(payload, dict) and payload.get("bool") is True


def _read_frame_offset(
    app: Flask, endpoint: str, recorded_id: int, params: dict
) -> int:
    response = requests.get(
        f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/{endpoint}",
        params={**params, "OffsetType": "Position"},
        headers={"Accept": "application/json"},
        timeout=app.config["MYTHTV_TIMEOUT"],
    )
    response.raise_for_status()
    frames = int(response.json().get("long", 0))
    frame_rate = _probe_recording(app, recorded_id).get("video_frame_rate")
    return round(frames * 1000 / frame_rate) if frame_rate else 0


def _load_all_recordings(app: Flask) -> list[dict]:
    cache_path = Path(app.config["MYTHTV_RECORDINGS_CACHE_PATH"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache_path.with_suffix(f"{cache_path.suffix}.lock")

    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            try:
                cached = json.loads(cache_path.read_text())
                cache_age = time.time() - cached["cached_at"]
                if cache_age <= RECORDINGS_CACHE_SECONDS:
                    return cached["programs"]
            except (OSError, ValueError, KeyError, TypeError):
                pass

            programs = []
            start_index = 0
            total = None
            while total is None or start_index < total:
                response = requests.get(
                    f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedList",
                    params={
                        "StartIndex": start_index,
                        "Count": RECORDINGS_PAGE_SIZE,
                        "Descending": "true",
                    },
                    headers={"Accept": "application/json"},
                    timeout=app.config["MYTHTV_GROUPS_TIMEOUT"],
                )
                response.raise_for_status()
                payload = response.json().get("ProgramList", {})
                page = payload.get("Programs", [])
                total = payload.get("TotalAvailable", 0)
                programs.extend(page)
                count = payload.get("Count", len(page))
                if count <= 0:
                    break
                start_index += count

            temporary_path = cache_path.with_name(
                f".{cache_path.name}.{os.getpid()}.tmp"
            )
            temporary_path.write_text(
                json.dumps({"cached_at": time.time(), "programs": programs})
            )
            os.replace(temporary_path, cache_path)
            return programs
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_prefix=1)

    @app.after_request
    def set_browser_wasm_isolation_headers(response):
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
        return response

    app.config["MYTHTV_BACKEND_URL"] = os.environ.get(
        "MYTHTV_BACKEND_URL", DEFAULT_BACKEND_URL
    ).rstrip("/")
    app.config["MYTHTV_TIMEOUT"] = 8
    app.config["MYTHTV_GROUPS_TIMEOUT"] = 30
    app.config["MYTHTV_RECORDINGS_CACHE_PATH"] = str(
        Path(__file__).resolve().parents[1] / ".run" / "recordings-cache.json"
    )
    app.config["MYTHTV_TRANSCODE_DIR"] = str(
        Path(__file__).resolve().parents[1] / ".run" / "transcodes"
    )
    app.config.update(
        AUTH_DATABASE_PATH=os.environ.get(
            "MYTHTV_AUTH_DATABASE",
            str(Path(__file__).resolve().parents[1] / ".run" / "users.sqlite"),
        ),
        INITIAL_ADMIN_USERNAME=os.environ.get("MYTHTV_INITIAL_ADMIN_USERNAME", "admin"),
        INITIAL_ADMIN_PASSWORD=os.environ.get(
            "MYTHTV_INITIAL_ADMIN_PASSWORD", "mythtv-admin"
        ),
        SECRET_KEY=os.environ.get("MYTHTV_SECRET_KEY"),
        SESSION_COOKIE_SECURE=os.environ.get("MYTHTV_SESSION_COOKIE_SECURE", "0")
        in {"1", "true", "yes"},
    )
    session_lifetime_days = int(os.environ.get("MYTHTV_SESSION_LIFETIME_DAYS", "365"))
    if session_lifetime_days < 1:
        raise ValueError("MYTHTV_SESSION_LIFETIME_DAYS must be at least 1")
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=session_lifetime_days)
    if config:
        app.config.update(config)
    init_auth(app)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify(status="ok")

    @app.get("/api/programs")
    def programs():
        start_index = max(0, request.args.get("start", default=0, type=int))
        page_count = min(100, max(1, request.args.get("count", default=40, type=int)))
        try:
            response = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedList",
                params={
                    "StartIndex": start_index,
                    "Count": page_count,
                    "Descending": "true",
                },
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            response.raise_for_status()
            payload = response.json().get("ProgramList", {})
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV recorded-list request failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502

        return jsonify(
            programs=payload.get("Programs", []),
            start=payload.get("StartIndex", 0),
            count=payload.get("Count", 0),
            total=payload.get("TotalAvailable", 0),
            as_of=payload.get("AsOf"),
        )

    @app.get("/api/backend-status")
    def backend_status():
        try:
            response = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Status/GetBackendStatus",
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            response.raise_for_status()
            payload = response.json().get("BackendStatus")
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV backend-status request failed: %s", error)
            return jsonify(error="Could not load MythTV backend status"), 502

        if not isinstance(payload, dict):
            return jsonify(error="MythTV returned an invalid backend status"), 502
        return jsonify(status=payload)

    @app.get("/api/upcoming")
    def upcoming():
        start_index = max(0, request.args.get("start", default=0, type=int))
        page_count = min(100, max(1, request.args.get("count", default=40, type=int)))
        try:
            response = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetUpcomingList",
                params={"StartIndex": start_index, "Count": page_count},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            response.raise_for_status()
            payload = response.json().get("ProgramList", {})
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV upcoming-list request failed: %s", error)
            return jsonify(error="Could not load upcoming recordings"), 502

        return jsonify(
            programs=payload.get("Programs", []),
            start=payload.get("StartIndex", 0),
            count=payload.get("Count", 0),
            total=payload.get("TotalAvailable", 0),
        )

    @app.get("/api/guide")
    def guide():
        start_time = request.args.get("start", "")
        end_time = request.args.get("end", "")
        parsed_start = _parse_api_time(start_time)
        parsed_end = _parse_api_time(end_time)
        if (
            parsed_start is None
            or parsed_end is None
            or parsed_end <= parsed_start
            or parsed_end - parsed_start > timedelta(hours=6)
        ):
            return (
                jsonify(error="A valid guide window of up to six hours is required"),
                400,
            )

        try:
            response = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Guide/GetProgramGuide",
                params={"StartTime": start_time, "EndTime": end_time},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            response.raise_for_status()
            payload = response.json().get("ProgramGuide")
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV program-guide request failed: %s", error)
            return jsonify(error="Could not load the MythTV program guide"), 502

        if not isinstance(payload, dict) or not isinstance(
            payload.get("Channels"), list
        ):
            return jsonify(error="MythTV returned an invalid program guide"), 502
        return jsonify(
            channels=payload["Channels"],
            start=payload.get("StartTime"),
            end=payload.get("EndTime"),
            total=payload.get("TotalAvailable", 0),
            as_of=payload.get("AsOf"),
        )

    @app.get("/api/schedules/options")
    def schedule_options():
        record_id = request.args.get("record_id", default=0, type=int)
        channel_id = request.args.get("channel_id", default=0, type=int)
        start_time = request.args.get("start_time", "")
        if record_id > 0:
            params = {"RecordId": record_id}
        elif channel_id > 0 and _parse_api_time(start_time) is not None:
            params = {
                "RecordId": 0,
                "Template": "",
                "RecordedId": 0,
                "ChanId": channel_id,
                "StartTime": start_time,
                "MakeOverride": False,
            }
        else:
            return jsonify(error="A rule ID or program is required"), 400

        backend_url = app.config["MYTHTV_BACKEND_URL"]
        headers = {"Accept": "application/json"}
        timeout = app.config["MYTHTV_TIMEOUT"]
        try:
            rule_response = requests.get(
                f"{backend_url}/Dvr/GetRecordSchedule",
                params=params,
                headers=headers,
                timeout=timeout,
            )
            rule_response.raise_for_status()
            rule = rule_response.json().get("RecRule")
            if not isinstance(rule, dict):
                return jsonify(error="MythTV returned an invalid recording rule"), 502

            choice_paths = {
                "filters": "/Dvr/GetRecRuleFilterList",
                "inputs": "/Dvr/GetInputList",
                "recording_groups": "/Dvr/GetRecGroupList",
                "storage_groups": "/Dvr/GetRecStorageGroupList",
                "play_groups": "/Dvr/GetPlayGroupList",
            }
            choices = {}
            for key, path in choice_paths.items():
                params = {"UsedBy": "schedule"} if key == "recording_groups" else None
                response = requests.get(
                    f"{backend_url}{path}",
                    params=params,
                    headers=headers,
                    timeout=timeout,
                )
                response.raise_for_status()
                choices[key] = response.json()
        except (requests.RequestException, ValueError, TypeError) as error:
            app.logger.warning("MythTV schedule-options request failed: %s", error)
            return jsonify(error="Could not load recording options"), 502

        return jsonify(rule=rule, choices=choices)

    @app.post("/api/schedules")
    def schedule_recording():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="A channel and program start time are required"), 400
        channel_id = payload.get("channel_id")
        start_time = payload.get("start_time")
        if (
            type(channel_id) is not int
            or channel_id <= 0
            or _parse_api_time(start_time) is None
        ):
            return (
                jsonify(error="A valid channel and program start time are required"),
                400,
            )
        rule_options = payload.get("rule", {})
        if not isinstance(rule_options, dict):
            return jsonify(error="Recording options must be an object"), 400

        backend_url = app.config["MYTHTV_BACKEND_URL"]
        request_options = {
            "headers": {"Accept": "application/json"},
            "timeout": app.config["MYTHTV_TIMEOUT"],
        }
        try:
            lookup = requests.get(
                f"{backend_url}/Dvr/GetRecordSchedule",
                params={
                    "RecordId": 0,
                    "Template": "",
                    "RecordedId": 0,
                    "ChanId": channel_id,
                    "StartTime": start_time,
                    "MakeOverride": False,
                },
                **request_options,
            )
            lookup.raise_for_status()
            rule = lookup.json().get("RecRule")
            if not isinstance(rule, dict):
                return jsonify(error="MythTV returned an invalid recording rule"), 502
            if int(rule.get("Id") or 0) > 0:
                return jsonify(scheduled=True, existing=True, rule_id=rule["Id"])

            rule.update(
                {
                    key: value
                    for key, value in rule_options.items()
                    if key in RECORDING_RULE_FIELDS
                }
            )
            selected_channel = rule_options.get("ChanId", channel_id)
            rule["ChanId"] = (
                selected_channel
                if type(selected_channel) is int and selected_channel > 0
                else channel_id
            )
            rule["Station"] = rule.pop("CallSign", rule.get("Station", ""))
            rule["StartTime"] = rule_options.get("StartTime", start_time)
            if "Type" not in rule_options:
                rule["Type"] = "Record One"
            created = requests.post(
                f"{backend_url}/Dvr/AddRecordSchedule",
                json=rule,
                **request_options,
            )
            created.raise_for_status()
            result = created.json()
            rule_id = result.get("uint") if isinstance(result, dict) else None
        except (requests.RequestException, ValueError, TypeError) as error:
            app.logger.warning("MythTV schedule request failed: %s", error)
            return jsonify(error="Could not schedule this MythTV program"), 502

        if not isinstance(rule_id, int) or rule_id <= 0:
            return jsonify(error="MythTV did not create the recording schedule"), 502
        return jsonify(scheduled=True, existing=False, rule_id=rule_id)

    @app.post("/api/schedules/<int:record_id>")
    def update_schedule(record_id):
        payload = request.get_json(silent=True)
        rule_options = payload.get("rule") if isinstance(payload, dict) else None
        if not isinstance(rule_options, dict):
            return jsonify(error="Recording options must be an object"), 400

        backend_url = app.config["MYTHTV_BACKEND_URL"]
        headers = {"Accept": "application/json"}
        timeout = app.config["MYTHTV_TIMEOUT"]
        try:
            lookup = requests.get(
                f"{backend_url}/Dvr/GetRecordSchedule",
                params={"RecordId": record_id},
                headers=headers,
                timeout=timeout,
            )
            lookup.raise_for_status()
            rule = lookup.json().get("RecRule")
            if not isinstance(rule, dict) or int(rule.get("Id") or 0) != record_id:
                return jsonify(error="Recording rule not found"), 404
            rule.update(
                {
                    key: value
                    for key, value in rule_options.items()
                    if key in RECORDING_RULE_FIELDS
                }
            )
            rule["Station"] = rule.pop("CallSign", rule.get("Station", ""))
            updated = requests.post(
                f"{backend_url}/Dvr/UpdateRecordSchedule",
                json={"RecordId": record_id, **rule},
                headers=headers,
                timeout=timeout,
            )
            updated.raise_for_status()
        except (requests.RequestException, ValueError, TypeError) as error:
            app.logger.warning("MythTV schedule update failed: %s", error)
            return jsonify(error="Could not update the MythTV recording rule"), 502

        return jsonify(
            updated=bool(updated.json().get("bool", True)), rule_id=record_id
        )

    @app.post("/api/schedules/<int:record_id>/delete")
    @deletion_allowed
    def delete_schedule(record_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            return jsonify(error="Confirm removal of this recording rule"), 400

        try:
            response = requests.post(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/RemoveRecordSchedule",
                json={"RecordId": record_id},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError, TypeError) as error:
            app.logger.warning("MythTV schedule removal failed: %s", error)
            return jsonify(error="Could not remove the MythTV recording rule"), 502

        if not isinstance(result, dict) or not isinstance(result.get("bool"), bool):
            return jsonify(error="MythTV returned an invalid removal response"), 502
        if not result["bool"]:
            return jsonify(error="Recording rule not found"), 404
        return jsonify(deleted=True, rule_id=record_id)

    @app.get("/api/program-groups")
    def program_groups():
        try:
            programs = _load_all_recordings(app)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV group-list request failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502

        default_programs = [
            program
            for program in programs
            if (program.get("Recording") or {}).get("RecGroup")
            in ACTIVE_RECORDING_GROUPS
        ]

        def group_artwork(program):
            artwork = (program.get("Artwork") or {}).get("ArtworkInfos") or []
            for image_type in ("coverart", "fanart"):
                image_url = next(
                    (
                        item.get("URL")
                        for item in artwork
                        if item.get("Type") == image_type and item.get("URL")
                    ),
                    None,
                )
                if image_url:
                    return image_url, image_type
            return None, None

        groups = {}
        for program in default_programs:
            title = program.get("Title") or "Untitled recording"
            group_key = _normalize_group_title(title)
            latest_start = program.get("StartTime") or ""
            group = groups.get(group_key)
            if group is None:
                artwork_url, artwork_type = group_artwork(program)
                group = {
                    "title": title,
                    "recording_count": 0,
                    "watched_count": 0,
                    "latest_start": latest_start,
                    "channel_name": (program.get("Channel") or {}).get("ChannelName"),
                    "category": program.get("Category"),
                    "artwork_url": artwork_url,
                    "artwork_type": artwork_type,
                    "preview_url": _recording_preview_url(program),
                }
                groups[group_key] = group
            elif not group["artwork_url"]:
                group["artwork_url"], group["artwork_type"] = group_artwork(program)

            group["recording_count"] += 1
            if "WATCHED" in (program.get("ProgramFlagNames") or "").split("|"):
                group["watched_count"] += 1
            if latest_start > group["latest_start"]:
                group["latest_start"] = latest_start

        sorted_groups = sorted(
            groups.values(), key=lambda group: _title_sort_key(group["title"])
        )
        return jsonify(groups=sorted_groups, total=len(default_programs))

    @app.get("/api/program-group")
    def program_group():
        title = request.args.get("title", "")
        if not title:
            return jsonify(error="A program title is required"), 400

        try:
            programs = _load_all_recordings(app)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV program-group request failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502

        matches = [
            program
            for program in programs
            if (program.get("Recording") or {}).get("RecGroup")
            in ACTIVE_RECORDING_GROUPS
            and _normalize_group_title(program.get("Title") or "Untitled recording")
            == _normalize_group_title(title)
        ]
        if not matches:
            return jsonify(error="Program group not found"), 404
        matches.sort(key=lambda program: program.get("StartTime") or "")
        return jsonify(title=title, programs=matches, total=len(matches))

    @app.delete("/api/recordings/<int:recorded_id>")
    @deletion_allowed
    def delete_recording(recorded_id):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            return jsonify(error="Explicit confirmation is required"), 400

        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV delete lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        params = {
            "RecordedId": recorded_id,
            "ChanId": (program.get("Channel") or {}).get("ChanId", 0),
            "StartTime": program.get("StartTime", ""),
            "ForceDelete": False,
            "AllowRerecord": False,
        }
        try:
            upstream = requests.post(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/DeleteRecording",
                json=params,
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
            payload = upstream.json()
            deleted = isinstance(payload, dict) and payload.get("bool") is True
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV recording deletion failed: %s", error)
            return jsonify(error="Could not delete recording from MythTV"), 502

        if not deleted:
            return jsonify(error="MythTV did not delete the recording"), 502

        try:
            _invalidate_recordings_cache(app)
        except OSError as error:
            app.logger.warning("Could not invalidate recordings cache: %s", error)
        return jsonify(deleted=True)

    @app.get("/api/artwork")
    def artwork():
        source_url = request.args.get("url", "")
        parsed_url = urlsplit(source_url)
        pairs = parse_qsl(parsed_url.query, keep_blank_values=True)
        parameters = dict(pairs)
        valid_image_file = parsed_url.path == "/Content/GetImageFile" and set(
            parameters
        ) == {"StorageGroup", "FileName"}
        valid_program_artwork = (
            parsed_url.path == "/Content/GetRecordingArtwork"
            and len(parameters) == len(pairs)
            and set(parameters).issubset(
                {"Inetref", "InetRef", "Type", "Season", "Width", "Height"}
            )
            and ("Inetref" in parameters) != ("InetRef" in parameters)
            and re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}",
                parameters.get("Inetref", parameters.get("InetRef", "")),
            )
            and parameters.get("Type") in {"coverart", "fanart", "banner"}
        )
        if (
            parsed_url.scheme
            or parsed_url.netloc
            or len(parameters) != len(pairs)
            or not (valid_image_file or valid_program_artwork)
        ):
            return jsonify(error="Invalid artwork URL"), 400

        if parsed_url.path == "/Content/GetRecordingArtwork":
            parameters.pop("Width", None)
            parameters.pop("Height", None)

        try:
            upstream = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}{parsed_url.path}",
                params=parameters,
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
        except requests.RequestException as error:
            app.logger.info("MythTV artwork request failed: %s", error)
            return Response(status=404)

        return Response(
            upstream.content,
            content_type=upstream.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/channel-icon")
    def channel_icon():
        filename = request.args.get("filename", "")
        if not re.fullmatch(r"[A-Za-z0-9_. -]{1,255}", filename) or filename in {
            ".",
            "..",
        }:
            return jsonify(error="Invalid channel icon filename"), 400

        try:
            upstream = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Guide/GetChannelIcon",
                params={"FileName": filename},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
        except requests.RequestException as error:
            app.logger.info("MythTV channel icon request failed: %s", error)
            return Response(status=404)

        return Response(
            upstream.content,
            content_type=upstream.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/preview")
    def preview():
        recorded_id = request.args.get("recorded_id", type=int)
        if recorded_id is None or recorded_id <= 0:
            return jsonify(error="A valid recording ID is required"), 400

        try:
            upstream = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Content/GetPreviewImage",
                params={"RecordedId": recorded_id, "Width": 640, "Format": "JPG"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
        except requests.RequestException as error:
            app.logger.info("MythTV preview request failed: %s", error)
            return Response(status=404)

        return Response(
            upstream.content,
            content_type=upstream.headers.get("Content-Type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/playback/<int:recorded_id>")
    def playback_info(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        channel_id = (program.get("Channel") or {}).get("ChanId", 0)
        start_time = program.get("StartTime", "")
        params = {
            "RecordedId": recorded_id,
            "ChanId": channel_id,
            "StartTime": start_time,
            "OffsetType": "Duration",
        }
        try:
            comm_breaks = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedCommBreak",
                params={**params, "IncludeFps": "false"},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            comm_breaks.raise_for_status()
            cut_list = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedCutList",
                params={**params, "IncludeFps": "false"},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            cut_list.raise_for_status()
            last_position = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetLastPlayPos",
                params=params,
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            last_position.raise_for_status()
            bookmark = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetSavedBookmark",
                params=params,
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            bookmark.raise_for_status()
            last_position_ms = int(last_position.json().get("long", 0))
            bookmark_ms = int(bookmark.json().get("long", 0))
            if last_position_ms < 0:
                last_position_ms = _read_frame_offset(
                    app, "GetLastPlayPos", recorded_id, params
                )
            if bookmark_ms < 0:
                bookmark_ms = _read_frame_offset(
                    app, "GetSavedBookmark", recorded_id, params
                )
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV playback metadata request failed: %s", error)
            return jsonify(error="Could not load MythTV playback metadata"), 502

        return jsonify(
            program=program,
            media=_probe_recording(app, recorded_id),
            comm_breaks=_commercial_breaks_from_cuttings(
                (comm_breaks.json().get("CutList") or {}).get("Cuttings", [])
            ),
            cut_list=(cut_list.json().get("CutList") or {}).get("Cuttings", []),
            last_position=last_position_ms,
            bookmark=bookmark_ms,
        )

    @app.get("/api/playback/<int:recorded_id>/live-status")
    def playback_live_status(recorded_id):
        try:
            cached_program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV live playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if cached_program is None:
            return jsonify(error="Recording not found"), 404

        try:
            current = requests.get(
                f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecorded",
                params={"RecordedId": recorded_id},
                headers={"Accept": "application/json"},
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            current.raise_for_status()
            payload = current.json()
            program = payload.get("Program") if isinstance(payload, dict) else None
            if not isinstance(program, dict):
                return jsonify(error="Invalid MythTV recording status response"), 502

            recording = program.get("Recording") or {}
            status_name = str(recording.get("StatusName", ""))
            end_time = program.get("EndTime") or recording.get("EndTs")
            in_progress = status_name.casefold() == "recording"
            comm_breaks = []
            if in_progress:
                markers = requests.get(
                    f"{app.config['MYTHTV_BACKEND_URL']}/Dvr/GetRecordedCommBreak",
                    params={
                        "RecordedId": recorded_id,
                        "OffsetType": "Duration",
                        "IncludeFps": "false",
                    },
                    headers={"Accept": "application/json"},
                    timeout=app.config["MYTHTV_TIMEOUT"],
                )
                markers.raise_for_status()
                comm_breaks = _commercial_breaks_from_cuttings(
                    (markers.json().get("CutList") or {}).get("Cuttings", [])
                )
        except (requests.RequestException, ValueError, AttributeError) as error:
            app.logger.info("MythTV live playback status refresh failed: %s", error)
            return jsonify(error="Could not refresh MythTV recording status"), 502

        return jsonify(
            in_progress=in_progress,
            end_time=end_time,
            comm_breaks=comm_breaks,
        )

    @app.get("/api/playback/<int:recorded_id>/stream")
    def playback_stream(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        headers = {}
        if request.headers.get("Range"):
            headers["Range"] = request.headers["Range"]
        try:
            upstream = requests.get(
                _recording_url(app, recorded_id),
                headers=headers,
                stream=True,
                timeout=app.config["MYTHTV_TIMEOUT"],
            )
            upstream.raise_for_status()
        except requests.RequestException as error:
            app.logger.info("MythTV recording stream failed: %s", error)
            return Response(status=404)

        def generate():
            try:
                yield from upstream.iter_content(chunk_size=64 * 1024)
            finally:
                upstream.close()

        response = Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            content_type=upstream.headers.get(
                "Content-Type", "application/octet-stream"
            ),
        )
        for header in (
            "Accept-Ranges",
            "Content-Length",
            "Content-Range",
            "Content-Disposition",
            "Last-Modified",
        ):
            if header in upstream.headers:
                response.headers[header] = upstream.headers[header]
        return response

    @app.post("/api/playback/<int:recorded_id>/position")
    def save_playback_position(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        position = request.json.get("position") if request.is_json else None
        if (
            isinstance(position, bool)
            or not isinstance(position, (int, float))
            or not math.isfinite(position)
            or position < 0
        ):
            return jsonify(error="A non-negative playback position is required"), 400
        try:
            saved = _post_playback_offset(
                app,
                "SetLastPlayPos",
                {
                    "RecordedId": recorded_id,
                    "ChanId": (program.get("Channel") or {}).get("ChanId", 0),
                    "StartTime": program.get("StartTime", ""),
                },
                recorded_id,
                position,
            )
            if not saved:
                app.logger.warning("MythTV rejected playback position save")
                return jsonify(error="MythTV did not save playback position"), 502
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV playback position save failed: %s", error)
            return jsonify(error="Could not save playback position"), 502
        return jsonify(saved=True)

    @app.post("/api/playback/<int:recorded_id>/bookmark")
    def save_playback_bookmark(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        position = request.json.get("position") if request.is_json else None
        if (
            isinstance(position, bool)
            or not isinstance(position, (int, float))
            or not math.isfinite(position)
            or position < 0
        ):
            return jsonify(error="A non-negative playback position is required"), 400
        try:
            saved = _post_playback_offset(
                app,
                "SetSavedBookmark",
                {
                    "RecordedId": recorded_id,
                    "ChanId": (program.get("Channel") or {}).get("ChanId", 0),
                    "StartTime": program.get("StartTime", ""),
                },
                recorded_id,
                position,
            )
            if not saved:
                app.logger.warning("MythTV rejected playback bookmark save")
                return jsonify(error="MythTV did not save playback bookmark"), 502
        except (requests.RequestException, ValueError) as error:
            app.logger.warning("MythTV bookmark save failed: %s", error)
            return jsonify(error="Could not save playback bookmark"), 502
        return jsonify(saved=True)

    @app.get("/api/playback/<int:recorded_id>/transcode")
    def playback_transcode(recorded_id):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return jsonify(error="FFmpeg is required to transcode this recording"), 501
        start = request.args.get("start", default=0.0, type=float)
        token = request.args.get("token", "")
        if (
            start is None
            or not math.isfinite(start)
            or start < 0
            or start > 7 * 24 * 60 * 60
        ):
            return jsonify(error="Invalid playback start position"), 400
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            return jsonify(error="A valid transcode token is required"), 400
        transcode_dir = Path(app.config["MYTHTV_TRANSCODE_DIR"])
        transcode_dir.mkdir(parents=True, exist_ok=True)
        process_path = transcode_dir / f"{token}.pid"
        if process_path.exists():
            return jsonify(error="Transcode token is already active"), 409

        subtitle = request.args.get("subtitle")
        subtitle_index = None
        if subtitle is not None:
            if len(subtitle) > 10 or not subtitle.isascii() or not subtitle.isdecimal():
                return jsonify(error="Invalid subtitle track"), 400
            subtitle_index = int(subtitle)
            tracks = _probe_recording(app, recorded_id)["subtitles"]
            if not any(
                track["index"] == subtitle_index
                and track.get("codec") == "dvb_subtitle"
                for track in tracks
            ):
                return jsonify(error="DVB subtitle track not found"), 404
        threads = "2"
        video_options = ["-map", "0:v:0?"]
        if subtitle_index is not None:
            video_options = [
                "-filter_complex_threads",
                threads,
                "-filter_complex",
                f"[0:v:0][0:{subtitle_index}]overlay=eof_action=pass[v]",
                "-map",
                "[v]",
            ]
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(start),
            "-i",
            _recording_url(app, recorded_id),
            *video_options,
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+frag_keyframe+empty_moov+default_base_moof",
            "-f",
            "mp4",
            "pipe:1",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as error:
            app.logger.warning("Could not start FFmpeg: %s", error)
            return jsonify(error="Could not start FFmpeg"), 502
        process_path.write_text(str(process.pid))

        def generate():
            try:
                while chunk := process.stdout.read1(64 * 1024):
                    yield chunk
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process_path.unlink(missing_ok=True)

        return Response(
            stream_with_context(generate()),
            content_type="video/mp4",
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/playback/<int:recorded_id>/transcode/cancel")
    def cancel_playback_transcode(recorded_id):
        payload = request.get_json(silent=True) or {}
        token = payload.get("token", "")
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            return jsonify(error="A valid transcode token is required"), 400
        process_path = Path(app.config["MYTHTV_TRANSCODE_DIR"]) / f"{token}.pid"
        try:
            process_id = int(process_path.read_text())
            os.kill(process_id, signal.SIGTERM)
        except FileNotFoundError:
            return jsonify(cancelled=False), 404
        except (OSError, ValueError):
            return jsonify(cancelled=False), 404
        return jsonify(cancelled=True)

    @app.get("/api/playback/<int:recorded_id>/subtitles/<int:stream_index>")
    def playback_subtitles(recorded_id, stream_index):
        try:
            program = _active_recording(app, recorded_id)
        except (requests.RequestException, ValueError, OSError) as error:
            app.logger.warning("MythTV playback lookup failed: %s", error)
            return jsonify(error="MythTV backend is unavailable"), 502
        if program is None:
            return jsonify(error="Recording not found"), 404

        subtitle_indexes = {
            track["index"] for track in _probe_recording(app, recorded_id)["subtitles"]
        }
        if stream_index not in subtitle_indexes:
            return jsonify(error="Subtitle track not found"), 404

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return jsonify(error="FFmpeg is required to extract subtitles"), 501
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-i",
                    _recording_url(app, recorded_id),
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
        except (OSError, subprocess.TimeoutExpired) as error:
            app.logger.warning("Subtitle extraction failed: %s", error)
            return jsonify(error="Could not extract recording subtitles"), 502
        if result.returncode:
            app.logger.info("Subtitle extraction returned %s", result.returncode)
            return Response(status=404)
        return Response(result.stdout, content_type="text/vtt; charset=utf-8")

    return app
