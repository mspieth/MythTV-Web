import subprocess
from pathlib import Path

import pytest

from mythtv_web.captions import extract_captions


@pytest.mark.parametrize(
    "standard,selection", [("cea608", "608-cc1"), ("cea708", "708-service-01")]
)
def test_caption_extraction_selects_service_and_cleans_temporary_file(
    monkeypatch, standard, selection
):
    class Download:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, _):
            yield b"recording"

    monkeypatch.setattr("mythtv_web.captions.requests.get", lambda *a, **kw: Download())
    paths = []
    captions = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n"

    def run(command, **kwargs):
        if command[0] == "mythccextractor":
            paths.append(Path(command[2]))
            assert paths[-1].read_bytes() == b"recording"
            assert command[3] == "--destdir"
            (paths[-1].parent / f"recording.{selection}.eng.srt").write_text("captions")
            return subprocess.CompletedProcess(command, 0, b"")
        assert command[0] == "ffmpeg"
        assert command[-3:] == ["-f", "webvtt", "pipe:1"]
        assert kwargs["timeout"] <= 120
        return subprocess.CompletedProcess(command, 0, captions)

    monkeypatch.setattr("mythtv_web.captions.subprocess.run", run)
    assert (
        extract_captions(
            "mythccextractor", "http://backend/recording", standard, "ffmpeg"
        )
        == captions
    )
    assert not paths[0].exists()


def test_caption_extraction_rejects_unknown_service():
    with pytest.raises(ValueError, match="Unknown"):
        extract_captions(
            "mythccextractor", "http://backend/recording", "unknown", "ffmpeg"
        )


@pytest.mark.parametrize("output", [b"", b"WEBVTT\n\n"])
def test_caption_extraction_rejects_empty_output_and_cleans_up(monkeypatch, output):
    class Download:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, _):
            yield b"recording"

    paths = []
    monkeypatch.setattr("mythtv_web.captions.requests.get", lambda *a, **kw: Download())

    def run(command, **kwargs):
        if command[0] == "mythccextractor":
            paths.append(Path(command[2]))
            (paths[-1].parent / "recording.708-service-01.eng.srt").write_text(
                "captions"
            )
        return subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr("mythtv_web.captions.subprocess.run", run)
    with pytest.raises(ValueError):
        extract_captions(
            "mythccextractor", "http://backend/recording", "cea708", "ffmpeg"
        )
    assert not paths[0].exists()
