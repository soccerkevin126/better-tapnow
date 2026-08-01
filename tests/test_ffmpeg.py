"""ffmpeg adapter tests — real local ffmpeg, still zero network and zero spend."""
import shutil
import subprocess

import pytest

from tapnow.providers import ProviderError, ProviderRequest
from tapnow.providers.ffmpeg import FfmpegProvider

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                  reason="ffmpeg not installed")


def make_clip(path, spec):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", spec, "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@needs_ffmpeg
async def test_assembles_mismatched_clips(tmp_path):
    clips = [make_clip(tmp_path / "a.mp4", "testsrc=duration=1:size=640x360:rate=24"),
             make_clip(tmp_path / "b.mp4", "testsrc2=duration=1:size=1920x1080:rate=30")]
    result = await FfmpegProvider().execute(ProviderRequest(
        kind="assemble", model="ffmpeg", input_files=clips,
        output_dir=tmp_path / "out"))

    out = result.files[0]
    assert out.exists() and result.cost_usd == 0.0
    duration = float(subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(out)]))
    assert duration == pytest.approx(2.0, abs=0.2)


@needs_ffmpeg
async def test_missing_clip_reported(tmp_path):
    with pytest.raises(ProviderError, match="missing input clip"):
        await FfmpegProvider().execute(ProviderRequest(
            kind="assemble", model="ffmpeg",
            input_files=[tmp_path / "nope.mp4"], output_dir=tmp_path))


@needs_ffmpeg
async def test_no_clips_rejected(tmp_path):
    with pytest.raises(ProviderError, match="no clips"):
        await FfmpegProvider().execute(ProviderRequest(
            kind="assemble", model="ffmpeg", input_files=[], output_dir=tmp_path))
