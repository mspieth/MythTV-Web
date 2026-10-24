"""Extract embedded broadcast captions using MythTV's caption extractor."""

import subprocess
import tempfile
import time
from pathlib import Path

import requests

MAX_RECORDING_BYTES = 8 * 1024**3


def extract_captions(
    executable: str, source_url: str, standard: str, ffmpeg: str
) -> bytes:
    if standard not in {"cea608", "cea708"}:
        raise ValueError("Unknown caption standard")
    deadline = time.monotonic() + 120
    with tempfile.TemporaryDirectory(prefix="mythtv-captions-") as directory:
        source = Path(directory) / "recording.ts"
        size = 0
        with requests.get(source_url, stream=True, timeout=(5, 15)) as response:
            response.raise_for_status()
            with source.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_RECORDING_BYTES:
                        raise ValueError(
                            "Recording exceeds the 8 GiB caption extraction limit"
                        )
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Caption download timed out")
                    output.write(chunk)
        subprocess.run(
            [executable, "--infile", str(source), "--destdir", directory, "--quiet"],
            capture_output=True,
            timeout=max(1, deadline - time.monotonic()),
            check=True,
        )
        pattern = (
            "recording.*608-cc1.*.srt"
            if standard == "cea608"
            else "recording.*708-service-01.*.srt"
        )
        tracks = sorted(Path(directory).glob(pattern))
        if not tracks:
            raise ValueError("No captions found for the selected service")
        if len(tracks) != 1:
            raise ValueError(
                "Multiple video streams contain the selected caption service"
            )
        result = subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(tracks[0]),
                "-f",
                "webvtt",
                "pipe:1",
            ],
            capture_output=True,
            timeout=max(1, deadline - time.monotonic()),
            check=True,
        )
        if not result.stdout.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
            raise ValueError("No WebVTT captions returned")
        if b"-->" not in result.stdout:
            raise ValueError("No captions found for the selected service")
        return result.stdout
