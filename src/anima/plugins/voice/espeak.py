"""Local eSpeak NG speech synthesizer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from anima.plugins.voice.config import VoiceSettings


class EspeakSynthesizer:
    def __init__(
        self, settings: VoiceSettings, *,
        process_factory: Callable[..., object] = asyncio.create_subprocess_exec,
    ) -> None:
        self.settings = settings
        self.process_factory = process_factory

    async def synthesize(self, text: str, output: Path) -> None:
        normalized = text.strip()
        if not normalized or len(normalized) > self.settings.maximum_characters:
            raise ValueError("speech text is invalid")
        output.parent.mkdir(parents=True, exist_ok=True)
        process = await self.process_factory(
            "espeak-ng", "-v", self.settings.voice,
            "-s", str(self.settings.speed), "-a", str(self.settings.volume),
            "-w", str(output), "--", normalized,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                process.communicate(), self.settings.timeout_seconds
            )
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            output.unlink(missing_ok=True)
            raise RuntimeError("local speech synthesis timed out") from error
        if process.returncode != 0:
            output.unlink(missing_ok=True)
            detail = stderr.decode(errors="replace").strip()[:300]
            raise RuntimeError(f"local speech synthesis failed: {detail or 'unknown error'}")
        if not output.is_file() or output.stat().st_size == 0:
            output.unlink(missing_ok=True)
            raise RuntimeError("local speech synthesis produced no audio")
