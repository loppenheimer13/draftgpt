"""ElevenLabs synthesis for the upload pipeline.

Called directly rather than through Yoto Labs, which is what buys the upload
pipeline its one real advantage: any voice on the account, and audio longer
than the Labs 3000-character-per-track limit.
"""

from __future__ import annotations

import httpx

from huddle.config import Settings, get_settings
from huddle.tts.base import SynthesisError, SynthesizedAudio

API_BASE = "https://api.elevenlabs.io/v1"


class ElevenLabsSynthesizer:
    key = "elevenlabs"

    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        if not self.settings.elevenlabs_api_key:
            raise SynthesisError(
                "HUDDLE_ELEVENLABS_API_KEY is required for the upload pipeline"
            )
        self._owned = client is None
        # Synthesis is slower than a normal API call, so it gets its own floor.
        self._http = client or httpx.Client(
            timeout=max(self.settings.http_timeout_seconds, 120.0)
        )

    def close(self) -> None:
        if self._owned:
            self._http.close()

    def synthesize(self, text: str, *, voice_id: str | None = None) -> SynthesizedAudio:
        # Callers pass the host's voice; nova's is the fallback.
        voice = voice_id or self.settings.nova_voice_id
        try:
            response = self._http.post(
                f"{API_BASE}/text-to-speech/{voice}",
                json={
                    "text": text,
                    "model_id": self.settings.elevenlabs_model,
                    "output_format": "mp3_44100_128",
                },
                headers={
                    "xi-api-key": self.settings.elevenlabs_api_key or "",
                    "Accept": "audio/mpeg",
                },
            )
        except httpx.HTTPError as exc:
            raise SynthesisError(f"ElevenLabs unreachable: {exc}") from exc

        if response.status_code >= 400:
            raise SynthesisError(
                f"ElevenLabs returned {response.status_code}: {response.text[:200]}"
            )
        if not response.content:
            raise SynthesisError("ElevenLabs returned an empty body")

        return SynthesizedAudio(
            audio=response.content, content_type="audio/mpeg", audio_format="mp3"
        )
