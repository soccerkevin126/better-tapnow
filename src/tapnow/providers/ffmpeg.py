"""ffmpeg assembly adapter — local, free, no API.

Clips from different generators can disagree on resolution/fps/codec, so we
normalize every input (scale + pad to a common frame, constant fps) and
concat through a filter graph rather than the concat demuxer.
"""
from __future__ import annotations

import asyncio
import shutil

from .base import Provider, ProviderError, ProviderRequest, ProviderResult


class FfmpegProvider(Provider):
    name = "ffmpeg"

    def __init__(self, pricing=None, binary: str | None = None):
        self.binary = binary or shutil.which("ffmpeg")
        if not self.binary:
            raise ProviderError("ffmpeg not found on PATH — install it (brew install ffmpeg)")

    async def execute(self, request: ProviderRequest) -> ProviderResult:
        if request.kind != "assemble":
            raise ProviderError(f"ffmpeg adapter handles kind 'assemble', not '{request.kind}'")
        clips = request.input_files
        if not clips:
            raise ProviderError("no clips to assemble")
        missing = [str(c) for c in clips if not c.exists()]
        if missing:
            raise ProviderError(f"missing input clip(s): {', '.join(missing)}")

        w = int(request.params.get("width", 1280))
        h = int(request.params.get("height", 720))
        fps = int(request.params.get("fps", 24))
        request.output_dir.mkdir(parents=True, exist_ok=True)
        out = request.output_dir / str(request.params.get("output_name", "assembled.mp4"))

        n = len(clips)
        normalize = "".join(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}];"
            for i in range(n))
        concat = "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[outv]"

        args = [self.binary, "-y", "-hide_banner", "-loglevel", "error"]
        for clip in clips:
            args += ["-i", str(clip)]
        args += ["-filter_complex", normalize + concat, "-map", "[outv]", "-an",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 str(out)]

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise ProviderError(
                f"ffmpeg failed (exit {proc.returncode}): {stderr.decode()[-800:]}")
        return ProviderResult(files=[out], cost_usd=0.0,
                              meta={"clips": n, "resolution": f"{w}x{h}", "fps": fps})
