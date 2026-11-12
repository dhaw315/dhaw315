"""Fiche JSON posée à côté de chaque clip : titre et description modifiables."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .config import PRIVACY
from .media import format_timecode


class MetaError(Exception):
    pass


def meta_path_for(path: Path) -> Path:
    return path if path.suffix.lower() == ".json" else path.with_suffix(".json")


def new_meta(
    *, clip_file: str, clip_sha256: str, source_name: str, source_sha256: str,
    start_ms: int, end_ms: int, background: str, subtitles: bool,
    title: str, description: str, tags: list[str],
) -> dict:
    return {
        "youtube": {
            "title": title,
            "description": description,
            "tags": tags,
            "category_id": "22",
            "made_for_kids": False,
            "privacy": PRIVACY,
        },
        "clip": {
            "file": clip_file,
            "sha256": clip_sha256,
            "source_name": source_name,
            "source_sha256": source_sha256,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start": format_timecode(start_ms),
            "end": format_timecode(end_ms),
            "background": background,
            "subtitles": subtitles,
            "rights_confirmed": True,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }


def save(path: Path, meta: dict) -> None:
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load(path: Path) -> dict:
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise MetaError(f"Fiche introuvable : {path}") from None
    except json.JSONDecodeError as error:
        raise MetaError(f"Fiche JSON invalide ({path.name}) : {error}") from None
    validate(meta)
    return meta


def validate(meta: dict) -> None:
    try:
        youtube, clip = meta["youtube"], meta["clip"]
        title, description = youtube["title"], youtube.get("description", "")
        for field in ("file", "sha256", "source_name", "source_sha256", "start_ms", "end_ms"):
            clip[field]
    except (KeyError, TypeError) as error:
        raise MetaError(f"Fiche incomplète : champ {error} manquant.") from None

    if youtube.get("privacy", PRIVACY) != PRIVACY:
        raise MetaError(
            f"Visibilité « {youtube.get('privacy')} » refusée : ce programme n'envoie qu'en mode privé."
        )
    if clip.get("rights_confirmed") is not True:
        raise MetaError("Les droits de republication ne sont pas confirmés dans la fiche.")
    if not isinstance(title, str) or not title.strip():
        raise MetaError("Le titre est vide.")
    if len(title) > 100:
        raise MetaError(f"Titre trop long ({len(title)} caractères, maximum 100).")
    if not isinstance(description, str) or len(description.encode("utf-8")) > 5000:
        raise MetaError("Description invalide ou trop longue (maximum 5000 octets).")
    if any(char in title + description for char in "<>"):
        raise MetaError("YouTube refuse les caractères < et > dans le titre et la description.")
    tags = youtube.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise MetaError("« tags » doit être une liste de textes.")
    if sum(len(tag) + 3 for tag in tags) > 500:
        raise MetaError("Les tags dépassent la limite de 500 caractères.")


def request_body(meta: dict) -> dict:
    """Corps de videos.insert. La visibilité est toujours « private »."""
    youtube = meta["youtube"]
    return {
        "snippet": {
            "title": youtube["title"].strip(),
            "description": youtube.get("description", ""),
            "tags": youtube.get("tags", []),
            "categoryId": str(youtube.get("category_id", "22")),
        },
        "status": {
            "privacyStatus": PRIVACY,
            "selfDeclaredMadeForKids": bool(youtube.get("made_for_kids", False)),
        },
    }
