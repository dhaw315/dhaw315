"""Découpe et mise au format vertical avec FFmpeg."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import HEIGHT, MAX_CLIP_MS, MIN_CLIP_MS, WIDTH

log = logging.getLogger("ytclips")


class MediaError(Exception):
    pass


def parse_timecode(value: str) -> int:
    """« 90 », « 90.5 », « 1:30 » ou « 00:01:30.250 » -> millisecondes."""
    text = value.strip().replace(",", ".")
    if not re.fullmatch(r"(\d+:){0,2}\d+(\.\d+)?", text):
        raise MediaError(f"Temps invalide : « {value} » (attendu : secondes, MM:SS ou HH:MM:SS.mmm)")
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return round(seconds * 1000)


def format_timecode(ms: int) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, milli = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{milli:03d}"


def validate_range(start_ms: int, end_ms: int, source_ms: int | None = None) -> int:
    duration = end_ms - start_ms
    if duration <= 0:
        raise MediaError("La fin doit être après le début.")
    if not MIN_CLIP_MS <= duration <= MAX_CLIP_MS:
        raise MediaError(
            f"Durée de {duration / 1000:.1f} s refusée : un clip doit durer entre "
            f"{MIN_CLIP_MS // 1000} et {MAX_CLIP_MS // 1000} secondes."
        )
    if source_ms is not None and end_ms > source_ms + 50:
        raise MediaError(
            f"La fin ({format_timecode(end_ms)}) dépasse la durée de la source ({format_timecode(source_ms)})."
        )
    return duration


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise MediaError(f"{tool} est introuvable dans le PATH.")


def probe(path: Path) -> dict:
    """Renvoie durée (ms), dimensions et présence d'audio."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MediaError(f"Fichier illisible par ffprobe : {path.name} ({result.stderr.strip()[-300:]})")
    info = json.loads(result.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError(f"Aucune piste vidéo dans {path.name}.")
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    return {
        "duration_ms": round(duration * 1000),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": any(s.get("codec_type") == "audio" for s in info["streams"]),
    }


# --- Sous-titres -----------------------------------------------------------

_SRT_TIME = re.compile(
    r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def _srt_ms(h: str, m: str, s: str, frac: str) -> int:
    return int(h) * 3_600_000 + int(m) * 60_000 + int(s) * 1000 + int(frac.ljust(3, "0"))


def _srt_stamp(ms: int) -> str:
    return format_timecode(ms).replace(".", ",")


def decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def read_srt(path: Path) -> str:
    return decode_text(path.read_bytes())


def parse_srt(text: str) -> list[tuple[int, int, str]]:
    """Répliques (début ms, fin ms, texte) dans l'ordre chronologique."""
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = block.splitlines()
        index = next((i for i, line in enumerate(lines) if _SRT_TIME.search(line)), None)
        if index is None:
            continue
        match = _SRT_TIME.search(lines[index])
        start, end = _srt_ms(*match.group(1, 2, 3, 4)), _srt_ms(*match.group(5, 6, 7, 8))
        body = "\n".join(lines[index + 1 :]).strip()
        if end > start and body:
            cues.append((start, end, body))
    return sorted(cues)


def shift_srt(text: str, offset_ms: int, duration_ms: int) -> str:
    """Recale un SRT calé sur la source vers la ligne de temps du clip.

    Seules les répliques qui recouvrent [offset, offset + durée] sont gardées.
    """
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = block.splitlines()
        index = next((i for i, line in enumerate(lines) if _SRT_TIME.search(line)), None)
        if index is None:
            continue
        match = _SRT_TIME.search(lines[index])
        start = _srt_ms(*match.group(1, 2, 3, 4)) - offset_ms
        end = _srt_ms(*match.group(5, 6, 7, 8)) - offset_ms
        start, end = max(start, 0), min(end, duration_ms)
        body = "\n".join(lines[index + 1 :]).strip()
        if end > start and body:
            cues.append((start, end, body))
    return "".join(
        f"{number}\n{_srt_stamp(start)} --> {_srt_stamp(end)}\n{body}\n\n"
        for number, (start, end, body) in enumerate(cues, 1)
    )


# --- Rendu -----------------------------------------------------------------

SUBTITLE_STYLE = (
    "FontName=DejaVu Sans,FontSize=9,Bold=1,PrimaryColour=&H00FFFFFF,"
    "OutlineColour=&H00000000,BorderStyle=1,Outline=1.2,Shadow=0,Alignment=2,MarginV=50,"
    "MarginL=20,MarginR=20"
)


def build_filter(background: str, subtitles_file: str | None) -> str:
    """Image entière centrée dans 1080x1920, sur fond flou ou uni."""
    fit = f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    if background == "blur":
        # Le flou est calculé en basse définition puis agrandi : même rendu, bien plus rapide.
        graph = (
            f"[0:v]split=2[a][b];"
            f"[a]scale={WIDTH // 4}:{HEIGHT // 4}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH // 4}:{HEIGHT // 4},gblur=sigma=8,scale={WIDTH}:{HEIGHT},"
            f"eq=brightness=-0.10:saturation=0.9[bg];"
            f"[b]{fit}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
        )
    else:
        color = "black" if background == "black" else "0x" + background.lstrip("#")
        graph = f"[0:v]{fit},pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={color}"
    graph += ",setsar=1"
    if subtitles_file:
        graph += f",subtitles=filename={subtitles_file}:force_style='{SUBTITLE_STYLE}'"
    return graph + ",format=yuv420p[v]"


def validate_background(value: str) -> str:
    if value in ("blur", "black") or re.fullmatch(r"#?[0-9a-fA-F]{6}", value):
        return value
    raise MediaError("Fond invalide : utiliser « blur », « black » ou une couleur #RRGGBB.")


def build_command(
    source: Path, output: Path, start_ms: int, duration_ms: int, background: str,
    subtitles_file: str | None, has_audio: bool,
) -> list[str]:
    command = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start_ms / 1000:.3f}", "-t", f"{duration_ms / 1000:.3f}", "-i", str(source),
        "-filter_complex", build_filter(background, subtitles_file), "-map", "[v]",
    ]
    if has_audio:
        command += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    command += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-f", "mp4", str(output),
    ]
    return command


def render_clip(
    source: Path, output: Path, start_ms: int, end_ms: int,
    background: str = "blur", srt: Path | None = None, srt_timing: str = "source",
) -> dict:
    """Crée le clip vertical et renvoie les caractéristiques mesurées du résultat."""
    require_tools()
    background = validate_background(background)
    info = probe(source)
    duration_ms = validate_range(start_ms, end_ms, info["duration_ms"])
    source, output = source.resolve(), output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="ytclips-") as work:
        subtitles_file = None
        if srt is not None:
            text = read_srt(srt)
            shifted = shift_srt(text, start_ms if srt_timing == "source" else 0, duration_ms)
            if shifted:
                # Nom simple + cwd dédié : aucun échappement de chemin dans le filtre.
                subtitles_file = "subs.srt"
                (Path(work) / subtitles_file).write_text(shifted, encoding="utf-8")
                log.info("Sous-titres : %d réplique(s) dans l'extrait.", shifted.count("-->"))
            else:
                log.warning("Aucune réplique du fichier SRT ne tombe dans l'extrait : clip sans sous-titres.")

        partial = output.with_name(output.name + ".partial")
        command = build_command(
            source, partial, start_ms, duration_ms, background, subtitles_file, info["has_audio"]
        )
        log.debug("Commande FFmpeg : %s", " ".join(command))
        result = subprocess.run(command, cwd=work, capture_output=True, text=True)
        if result.returncode != 0:
            partial.unlink(missing_ok=True)
            raise MediaError("FFmpeg a échoué :\n" + result.stderr.strip()[-1500:])
        partial.replace(output)

    produced = probe(output)
    if (produced["width"], produced["height"]) != (WIDTH, HEIGHT):
        raise MediaError(f"Dimensions inattendues : {produced['width']}x{produced['height']}.")
    if abs(produced["duration_ms"] - duration_ms) > 500:
        raise MediaError(
            f"Durée inattendue : {produced['duration_ms'] / 1000:.2f} s au lieu de {duration_ms / 1000:.2f} s."
        )
    produced["subtitles"] = subtitles_file is not None
    return produced
