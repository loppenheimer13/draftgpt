"""Speech synthesis contract and provider lookup."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from huddle.config import Settings, get_settings


class SynthesisError(RuntimeError):
    """Synthesis failed. Never carries the API key."""


@dataclass(frozen=True)
class SynthesizedAudio:
    audio: bytes
    content_type: str
    audio_format: str
    #: None when the provider does not report it; the Yoto transcoder measures
    #: the real duration anyway, so this is only ever a hint.
    duration_seconds: float | None = None

    def __len__(self) -> int:
        return len(self.audio)


@runtime_checkable
class SpeechSynthesizer(Protocol):
    key: str

    def synthesize(self, text: str, *, voice_id: str | None = None) -> SynthesizedAudio: ...


def get_synthesizer(settings: Settings | None = None) -> SpeechSynthesizer:
    settings = settings or get_settings()
    provider = (settings.tts_provider or "elevenlabs").lower()
    if provider == "elevenlabs":
        from huddle.tts.elevenlabs import ElevenLabsSynthesizer

        return ElevenLabsSynthesizer(settings)
    raise SynthesisError(
        f"unknown TTS provider '{provider}'; set HUDDLE_TTS_PROVIDER=elevenlabs "
        "or use the default HUDDLE_AUDIO_PIPELINE=labs"
    )
