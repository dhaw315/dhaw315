"""TikTok : préparation des clips et de leur légende pour une publication manuelle.

Aucun appel à TikTok n'est fait ici. La publication directe par l'API n'est pas
accessible à cet outil (voir le README) ; ce module tient donc un historique
propre à TikTok, indépendant de celui de YouTube :

    state/tiktok/<identifiant>.json   une fiche par extrait

L'identifiant est celui de l'extrait (contenu de la source et bornes), le même
que pour YouTube, mais les deux historiques ne se lisent ni ne s'écrivent l'un l'autre.
"""

from __future__ import annotations

import json
import logging

from . import batch, clipmeta, ledger, proposals

log = logging.getLogger("ytclips")

PREFIX = "state/tiktok/"
READY, POSTED, SKIPPED = "ready", "posted", "skipped"
LABELS = {READY: "à publier", POSTED: "publié", SKIPPED: "écarté"}
CAPTION_MAX = 2200  # limite documentée par TikTok, comptée en unités UTF-16
MAX_LINK_HOURS = 168


def check_caption(text: str) -> str:
    text = text.strip()
    if not text:
        raise clipmeta.MetaError("La légende TikTok est vide.")
    length = len(text.encode("utf-16-le")) // 2
    if length > CAPTION_MAX:
        raise clipmeta.MetaError(f"Légende TikTok trop longue ({length} caractères, maximum {CAPTION_MAX}).")
    return text


def _key(content_key: str) -> str:
    return f"{PREFIX}{content_key}.json"


def records(storage) -> list[dict]:
    items = []
    for info in storage.list(PREFIX):
        raw = storage.read(info.key)
        if raw:
            items.append(json.loads(raw))
    return sorted(items, key=lambda r: (r["created_at"], r["clip_key"]))


def find(storage, prefix: str) -> dict:
    matches = [record for record in records(storage) if record["content_key"].startswith(prefix)]
    if len(matches) != 1:
        raise clipmeta.MetaError(f"Aucune fiche TikTok unique ne commence par « {prefix} » (voir « ytclips tiktok list »).")
    return matches[0]


def _save(storage, record: dict) -> None:
    record["updated_at"] = ledger.now()
    storage.write(_key(record["content_key"]), json.dumps(record, ensure_ascii=False, indent=2).encode())


def prepare(storage, dry_run: bool = False) -> list[dict]:
    """Crée une fiche TikTok pour chaque clip du stockage qui n'en a pas encore."""
    objects = {info.key for info in storage.list(batch.CLIPS)}
    book = {p["content_key"]: p for p in proposals.Book(storage).all()}
    created = []
    for meta_key in sorted(key for key in objects if key.endswith(".json")):
        clip_key = meta_key[:-5] + ".mp4"
        try:
            meta = json.loads(storage.read(meta_key) or b"")
            clip = meta["clip"]
            content_key = clip.get("content_key") or ledger.content_key(clip["source_sha256"], clip["start_ms"], clip["end_ms"])
            title = str(meta["youtube"]["title"])
        except (ValueError, KeyError, TypeError):
            log.warning("  [ignoré] %s — fiche de clip illisible.", meta_key)
            continue
        if clip_key not in objects:
            log.warning("  [ignoré] %s — fichier vidéo absent du stockage.", clip_key)
            continue
        proposal = book.get(content_key)
        if proposal and proposal["status"] == proposals.REJECTED:
            log.info("  [ignoré] %s — proposition refusée.", clip_key)
            continue
        if storage.read(_key(content_key)):
            log.info("  [déjà préparé] %s", clip_key)
            continue
        stamp = ledger.now()
        record = {
            "content_key": content_key, "platform": "tiktok", "status": READY, "clip_key": clip_key,
            "source_name": clip["source_name"], "start": clip["start"], "end": clip["end"],
            # Le titre validé par l'utilisateur prime sur celui du premier envoi.
            "caption": (proposal or {}).get("title") or title,
            "post_url": None, "note": None, "created_at": stamp, "updated_at": stamp, "posted_at": None,
        }
        log.info("  [%s] %s  %s — légende : %s", "simulé" if dry_run else "préparé", content_key[:12], clip_key, record["caption"])
        # Création exclusive : deux exécutions simultanées n'écrasent pas une légende déjà retouchée.
        if dry_run or storage.write(_key(content_key), json.dumps(record, ensure_ascii=False, indent=2).encode(), if_absent=True):
            created.append(record)
    if not objects:
        log.info("Aucun clip dans « %s » du stockage.", batch.CLIPS)
    if dry_run:
        log.info("SIMULATION : rien n'a été écrit dans le stockage.")
    return created


def set_caption(storage, prefix: str, text: str) -> dict:
    record = find(storage, prefix)
    record["caption"] = check_caption(text)
    _save(storage, record)
    return record


def link(storage, prefix: str, hours: float) -> tuple[dict, str]:
    """Fiche et lien de téléchargement temporaire du clip."""
    if not 0 < hours <= MAX_LINK_HOURS:
        raise clipmeta.MetaError(f"--hours doit être compris entre 0 et {MAX_LINK_HOURS} (7 jours).")
    record = find(storage, prefix)
    check_caption(record["caption"])
    return record, storage.presign(record["clip_key"], int(hours * 3600))


def mark(storage, prefix: str, status: str, *, url: str | None = None, note: str | None = None) -> dict:
    record = find(storage, prefix)
    if status == POSTED and url is not None and not url.startswith("https://"):
        raise clipmeta.MetaError("L'adresse de la publication doit commencer par https://.")
    record.update(status=status, note=note if note is not None else record.get("note"))
    if status == POSTED:
        record.update(post_url=url or record.get("post_url"), posted_at=record.get("posted_at") or ledger.now())
    else:
        record.update(posted_at=None)
    _save(storage, record)
    return record
