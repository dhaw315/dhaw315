"""Transcription locale facultative, avec faster-whisper (modèles Whisper libres, sans service payant).

La bibliothèque n'est pas installée par défaut : `uv sync --frozen --extra dev --extra transcribe`.
Le modèle est téléchargé depuis Hugging Face à la première utilisation dans chaque environnement.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from .media import MediaError, _srt_stamp

log = logging.getLogger("ytclips")

MAX_CHARS, MAX_GAP_MS = 70, 600


def group_words(words: list[tuple[int, int, str]]) -> list[tuple[int, int, str]]:
    """Regroupe des mots horodatés en répliques courtes qui s'arrêtent aux fins de phrase."""
    cues, current = [], []
    for index, (start, end, word) in enumerate(words):
        current.append((start, end, word.strip()))
        text = " ".join(item[2] for item in current)
        following = words[index + 1][0] if index + 1 < len(words) else None
        if (
            following is None or text.endswith((".", "?", "!", "…")) or len(text) >= MAX_CHARS
            or following - end > MAX_GAP_MS
        ):
            if text:
                cues.append((current[0][0], current[-1][1], text))
            current = []
    return cues


def to_srt(cues: list[tuple[int, int, str]]) -> str:
    return "".join(
        f"{number}\n{_srt_stamp(start)} --> {_srt_stamp(max(end, start + 200))}\n{text}\n\n"
        for number, (start, end, text) in enumerate(cues, 1)
    )


def transcribe(path: Path, work: Path, model_name: str = "small", language: str | None = None) -> tuple[str, str]:
    """Renvoie le SRT et la langue détectée. Long : compter une fraction notable de la durée du film."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise MediaError(
            "Transcription locale non installée. Installer avec : "
            "uv sync --frozen --extra dev --extra transcribe (ou pip install 'ytclips[transcribe]')."
        ) from None
    audio = work / "audio.wav"
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(audio)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise MediaError("Extraction de la bande-son impossible :\n" + result.stderr.strip()[-500:])
    log.info("Transcription locale avec le modèle « %s » (téléchargé au premier usage)…", model_name)
    started = time.monotonic()
    # Le son est décodé ici (FFmpeg puis lecture du WAV) plutôt que par la bibliothèque,
    # dont le décodeur intégré dépend d'une version précise de PyAV.
    import wave

    import numpy

    with wave.open(str(audio), "rb") as handle:
        samples = numpy.frombuffer(handle.readframes(handle.getnframes()), dtype=numpy.int16).astype(numpy.float32) / 32768
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(samples, language=language, vad_filter=True, word_timestamps=True)
    words = []
    for segment in segments:  # générateur : le calcul se fait ici
        for word in segment.words or []:
            words.append((round(word.start * 1000), round(word.end * 1000), word.word))
    log.info(
        "Transcription terminée en %.0f s pour %.0f s d'audio (langue : %s).",
        time.monotonic() - started, info.duration, info.language,
    )
    return to_srt(group_words(words)), info.language
