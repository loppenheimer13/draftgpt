"""Local speech synthesis for the upload pipeline.

Only used when ``HUDDLE_AUDIO_PIPELINE=upload``. The default Labs pipeline
needs none of this -- Yoto synthesises server-side.
"""

from huddle.tts.base import SpeechSynthesizer, SynthesisError, SynthesizedAudio, get_synthesizer

__all__ = ["SpeechSynthesizer", "SynthesisError", "SynthesizedAudio", "get_synthesizer"]
