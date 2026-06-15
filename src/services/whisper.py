"""OpenAI Whisper transcription service."""

from __future__ import annotations

from pathlib import Path

from openai import AsyncOpenAI

from src.config import settings


class WhisperService:
    """Thin wrapper around OpenAI Whisper API for audio transcription."""

    def __init__(self, api_key: str | None = None, model: str = "whisper-1"):
        self.client = AsyncOpenAI(api_key=api_key or settings.openai_api_key)
        self.model = model

    async def transcribe(self, file_path: str | Path, language: str = "ru") -> str:
        """Transcribe an audio file to text.

        file_path: path to .ogg / .mp3 / .m4a / .wav etc.
        language: ISO-639-1 code (e.g. 'ru', 'en'). Improves accuracy.
        """
        path = Path(file_path)
        with path.open("rb") as audio:
            response = await self.client.audio.transcriptions.create(
                model=self.model,
                file=audio,
                language=language,
            )
        return response.text.strip()