"""Sélection automatique, mise en évaluation privée et file de publication.

    propose  : analyse les sources et enregistre au plus 3 propositions par vidéo
    review   : crée les clips proposés et les envoie en PRIVÉ pour évaluation
    publish  : traite la file des propositions validées, un clip public par jour au plus

Un clip n'est jamais envoyé en public : il est envoyé en privé, puis sa visibilité
est modifiée. L'extrait garde la même identité que dans la commande batch, donc le
même historique anti-doublons.
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from . import analysis, batch, clipmeta, config, ledger as ledger_module, logs, media, proposals, selection, youtube
from .batch import Outcome
from .ledger import Ledger
from .proposals import Book

log = logging.getLogger("ytclips")

ANALYSIS, PUBLICATIONS = batch.ANALYSIS, "state/publications/"
MAX_PUBLISH_FAILURES = 3

TRANSCRIPTION_HINT = (
    "Sans sous-titres, les fins de phrases ne sont pas vérifiables. Déposez un fichier .srt de même nom "
    "à côté de la vidéo, ou activez une transcription (options et coûts dans le README)."
)


def _title_of(source_key: str) -> str:
    return PurePosixPath(source_key).stem.replace("_", " ").strip()


def _used_ranges(ledger: Ledger, book: Book, source_sha: str) -> list[tuple[int, int]]:
    """Passages déjà envoyés, en cours d'envoi ou déjà proposés (même refusés) pour cette source."""
    used = []
    for record in ledger.records():
        engaged = record["status"] == ledger_module.UPLOADED or (
            record["status"] == ledger_module.PENDING and record.get("phase") == ledger_module.UPLOADING
        )
        if record.get("source_sha256") == source_sha and record.get("start_ms", -1) >= 0 and engaged:
            used.append((record["start_ms"], record["end_ms"]))
    used += [(p["start_ms"], p["end_ms"]) for p in book.all() if p["source_sha256"] == source_sha]
    return used


def propose(
    storage, *, limit: int, dry_run: bool, force: bool = False, only: str | None = None,
    skip_start_ms: int = 0, skip_end_ms: int = 0,
) -> list[dict]:
    """Analyse au plus `limit` sources non encore analysées ; renvoie les propositions créées."""
    book, ledger = Book(storage), Ledger(storage)
    created: list[dict] = []
    handled = 0
    sources = [s for s in batch.list_sources(storage) if only in (None, s.key)]
    if not sources:
        log.info("Aucune vidéo à analyser dans « %s ».", batch.SOURCES)
    for source in sources:
        marker_key = f"{ANALYSIS}{source.fingerprint}.json"
        marker = storage.read(marker_key)
        if marker and not force:
            count = len(json.loads(marker).get("proposals", []))
            log.info("  [déjà analysée] %s — %d proposition(s) ; --force pour chercher d'autres passages.", source.key, count)
            continue
        if handled >= limit:
            log.info("  [en attente] %s — sera analysée par une prochaine exécution.", source.key)
            continue
        handled += 1

        with tempfile.TemporaryDirectory(prefix="ytclips-propose-") as work:
            local = Path(work) / f"source{PurePosixPath(source.key).suffix.lower()}"
            log.info("Source « %s » : téléchargement et analyse…", source.key)
            storage.download(source.key, local)
            source_sha = media.sha256_file(local)
            try:
                info = media.probe(local)
                signals = analysis.analyze(local, info)
            except media.MediaError as error:
                log.error("  [échec] %s — %s", source.key, str(error).splitlines()[0])
                continue
        cues = []
        if source.srt_key:
            cues = selection.make_cues(media.parse_srt(media.decode_text(storage.read(source.srt_key) or b"")))
        used = _used_ranges(ledger, book, source_sha)
        picks = selection.select(
            signals, cues, _title_of(source.key), used, skip_start_ms=skip_start_ms, skip_end_ms=skip_end_ms,
        )

        speech = sum(1 for cue in cues if not cue.music)
        log.info(
            "  Mesures : durée %s, %d changement(s) de plan, %d silence(s), %s, %d passage(s) déjà utilisé(s).",
            media.format_timecode(signals.duration_ms), len(signals.scene_cuts), len(signals.silences),
            f"{speech} réplique(s) de sous-titres" if source.srt_key else "pas de sous-titres", len(used),
        )
        if not speech:
            log.warning("  %s", TRANSCRIPTION_HINT)
        if not picks:
            log.info("  [aucune proposition] %s — aucun passage de 20 à 60 s ne remplit les critères.", source.key)

        keys = []
        for pick in picks:
            key = ledger_module.content_key(source_sha, pick.start_ms, pick.end_ms)
            record = {
                "content_key": key, "source_key": source.key, "source_sha256": source_sha,
                "start_ms": pick.start_ms, "end_ms": pick.end_ms,
                "start": media.format_timecode(pick.start_ms), "end": media.format_timecode(pick.end_ms),
                "title": pick.title, "description": pick.description, "reason": pick.reason,
                "basis": pick.basis, "score": pick.score, "metrics": pick.metrics,
            }
            log.info(
                "  [%s] %s  %s → %s (%.1f s)  score %.2f\n      titre  : %s\n      raison : %s",
                "simulé" if dry_run else "proposé", key[:12], record["start"], record["end"],
                (pick.end_ms - pick.start_ms) / 1000, pick.score, pick.title, pick.reason,
            )
            if not dry_run and book.create(record):
                keys.append(key)
            created.append(record)
        if not dry_run:
            storage.write(marker_key, json.dumps({
                "source_key": source.key, "source_sha256": source_sha, "analyzed_at": ledger_module.now(),
                "duration_ms": signals.duration_ms, "scene_cuts": len(signals.scene_cuts),
                "silences": len(signals.silences), "subtitles": bool(speech), "proposals": keys,
            }, ensure_ascii=False, indent=2).encode())
    if dry_run:
        log.info("SIMULATION : rien n'a été écrit dans le stockage.")
    return created


def _label(item: dict) -> str:
    return f"{item['content_key'][:12]} {item['source_key']} [{item['start'][3:8]} → {item['end'][3:8]}]"


def _sync(book: Book, item: dict, attempts: list[dict]) -> bool:
    """Reporte sur la proposition un envoi déjà inscrit dans l'historique (reprise après interruption)."""
    done = next((r for r in attempts if r["status"] == ledger_module.UPLOADED and r.get("video_id")), None)
    if done and item.get("video_id") != done["video_id"]:
        item.update(video_id=done["video_id"], privacy=item.get("privacy") or done.get("privacy"))
        book.save(item)
    return bool(done)


def _send(storage, book: Book, ledger: Ledger, creds, items: list[dict], run_id: str, *,
          limit: int, stale_seconds: float, max_attempts: int) -> list[Outcome]:
    """Envoie en privé les propositions qui ne l'ont pas encore été."""
    outcomes: list[Outcome] = []
    sources = {source.key: source for source in batch.list_sources(storage)}
    sent = 0
    stopped = False
    with tempfile.TemporaryDirectory(prefix="ytclips-review-") as work:
        cache: dict[str, tuple[Path, str]] = {}
        for number, item in enumerate(items):
            label = _label(item)
            decision = ledger.decide(item["content_key"], stale_seconds, max_attempts)
            if decision.action == ledger_module.DONE:
                _sync(book, item, decision.attempts)
                outcomes.append(Outcome(label, batch.SKIPPED, f"déjà envoyé (https://youtu.be/{item['video_id']})", video_id=item["video_id"]))
                continue
            if decision.action != ledger_module.TAKE:
                outcomes.append(batch._skip(label, decision))
                continue
            if stopped or sent >= limit:
                outcomes.append(Outcome(label, batch.WAITING, "sera envoyé par une prochaine exécution"))
                continue
            sent += 1
            source = sources.get(item["source_key"])
            if source is None:
                outcomes.append(Outcome(label, batch.FAILED, "source introuvable dans le stockage"))
                continue
            if source.key not in cache:
                local = Path(work) / f"source-{len(cache)}{PurePosixPath(source.key).suffix.lower()}"
                log.info("Source « %s » : téléchargement…", source.key)
                storage.download(source.key, local)
                cache[source.key] = (local, media.sha256_file(local))
            local, source_sha = cache[source.key]
            if source_sha != item["source_sha256"]:
                outcomes.append(Outcome(label, batch.FAILED, "la vidéo source a changé depuis l'analyse : relancer « propose »"))
                continue
            name = PurePosixPath(source.key)
            meta = clipmeta.new_meta(
                clip_file=f"{batch.slug(name.stem)}_{item['start_ms']}-{item['end_ms']}.mp4", clip_sha256="0",
                source_name=name.name, source_sha256=source_sha, start_ms=item["start_ms"], end_ms=item["end_ms"],
                background="blur", subtitles=False, title=item["title"], description=item["description"], tags=[],
            )
            try:
                clipmeta.validate(meta)
            except clipmeta.MetaError as error:
                outcomes.append(Outcome(label, batch.FAILED, f"{error} Corriger avec « approve --title »."))
                continue
            item_work = Path(work) / f"item-{number}"
            item_work.mkdir()
            outcome = batch.send_extract(
                storage, ledger, creds, run_id, label=label, source_key=source.key, srt_key=source.srt_key,
                local=local, meta=meta, background="blur", srt_timing="source",
                attempt=len(decision.attempts) + 1, work=item_work,
            )
            if outcome.video_id:
                item.update(video_id=outcome.video_id, privacy=config.PRIVACY)
                book.save(item)
            outcomes.append(outcome)
            stopped = stopped or outcome.stop
    return outcomes


def review(storage, *, limit: int, dry_run: bool, stale_seconds: float, max_attempts: int) -> bool:
    """Met en ligne, en privé, les propositions en attente d'évaluation."""
    book, ledger = Book(storage), Ledger(storage)
    items = [
        p for p in book.all()
        if p["status"] in (proposals.PROPOSED, proposals.APPROVED) and not p.get("video_id")
    ]
    if not items:
        log.info("Aucune proposition à mettre en évaluation.")
        return True
    if dry_run:
        for item in items[:limit]:
            log.info("  [simulé] %s — serait créé puis envoyé en privé : « %s »", _label(item), item["title"])
        log.info("SIMULATION : aucun clip créé, aucun appel à YouTube.")
        return True
    run_id = uuid.uuid4().hex[:12]
    creds = youtube.load_credentials()
    outcomes = _send(storage, book, ledger, creds, items, run_id, limit=limit, stale_seconds=stale_seconds, max_attempts=max_attempts)
    batch.log_outcomes(outcomes)
    batch.save_report(storage, run_id, "review", outcomes)
    return not any(outcome.result == batch.FAILED for outcome in outcomes)


def decide(storage, identifier: str, approve: bool, *, title: str | None = None,
           description: str | None = None, reason: str | None = None) -> dict:
    book = Book(storage)
    item = book.find(identifier)
    if item is None:
        raise clipmeta.MetaError(f"Aucune proposition unique ne commence par « {identifier} » (voir « ytclips proposals »).")
    if item["status"] == proposals.PUBLISHED:
        raise clipmeta.MetaError("Cette proposition est déjà publiée : sa visibilité se gère dans YouTube Studio.")
    if title is not None:
        item["title"] = title
    if description is not None:
        item["description"] = description
    if approve:
        check = clipmeta.new_meta(
            clip_file="x.mp4", clip_sha256="0", source_name="x", source_sha256="0", start_ms=0, end_ms=0,
            background="blur", subtitles=False, title=item["title"], description=item["description"], tags=[],
        )
        clipmeta.validate(check)
    item.update(
        status=proposals.APPROVED if approve else proposals.REJECTED,
        decided_at=ledger_module.now(), decision_note=reason,
    )
    book.save(item)
    return item


def _today() -> str:
    name = config.env(config.ENV_TIMEZONE) or "UTC"
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(name)).date().isoformat()
    except Exception:  # noqa: BLE001 - fuseau inconnu ou base absente
        return datetime.now(timezone.utc).date().isoformat()


def public_mode(flag: bool) -> bool:
    """Deux verrous : l'option --public ET la variable d'environnement."""
    return flag and config.env(config.ENV_PUBLIC_MODE).lower() == "enabled"


def publish(storage, *, public: bool, dry_run: bool, stale_seconds: float, max_attempts: int) -> bool:
    """Traite le premier clip validé de la file. Sans mode public, il reste privé."""
    book, ledger = Book(storage), Ledger(storage)
    queue = sorted(
        (p for p in book.all() if p["status"] == proposals.APPROVED),
        key=lambda p: (p.get("decided_at") or "", p["content_key"]),
    )
    blocked = [p for p in queue if p.get("publish_failures", 0) >= MAX_PUBLISH_FAILURES]
    for item in blocked:
        log.warning("  [à vérifier] %s — %d échecs de publication : %s", _label(item), item["publish_failures"], item.get("publish_error"))
    queue = [p for p in queue if p not in blocked]
    enabled = public_mode(public)
    log.info(
        "File de publication : %d clip(s) validé(s). Mode public : %s.", len(queue),
        "ACTIVÉ" if enabled else f"désactivé (il faut l'option --public et la variable {config.ENV_PUBLIC_MODE}=enabled)",
    )
    if not queue:
        return not blocked
    item = queue[0]
    day = _today()
    slot_key = f"{PUBLICATIONS}{day}.json"
    if dry_run:
        taken = storage.read(slot_key)
        log.info(
            "  [simulé] prochain clip : %s « %s » — %s ; créneau du %s : %s.", _label(item), item["title"],
            "déjà en ligne en privé" if item.get("video_id") else "pas encore envoyé",
            day, "déjà utilisé" if taken else "libre",
        )
        log.info("SIMULATION : aucun appel à YouTube, rien n'est modifié.")
        return True

    run_id = uuid.uuid4().hex[:12]
    creds = youtube.load_credentials()
    if not item.get("video_id") and not _sync(book, item, ledger.attempts(item["content_key"])):
        outcomes = _send(storage, book, ledger, creds, [item], run_id, limit=1, stale_seconds=stale_seconds, max_attempts=max_attempts)
        batch.log_outcomes(outcomes)
        batch.save_report(storage, run_id, "publish", outcomes)
    if not item.get("video_id"):
        log.error("Le clip n'a pas pu être mis en ligne en privé : publication reportée.")
        return False
    url = f"https://youtu.be/{item['video_id']}"
    if not enabled:
        log.info("Mode public désactivé : « %s » reste en PRIVÉ (%s).", item["title"], url)
        return True

    # Vérifié avant de réserver le créneau du jour : un refus certain ne consomme rien.
    if not youtube.can_manage(youtube.granted_scopes(creds)):
        raise youtube.AuthError(youtube.MANAGE_SCOPE_HELP)

    slot = {"date": day, "content_key": item["content_key"], "video_id": item["video_id"], "phase": "claimed", "run_id": run_id, "at": ledger_module.now()}
    fresh = storage.write(slot_key, json.dumps(slot).encode(), if_absent=True)
    if not fresh:
        existing = json.loads(storage.read(slot_key) or b"{}")
        if existing.get("content_key") != item["content_key"] or existing.get("phase") == "done":
            log.info("Limite d'un clip public par jour atteinte pour le %s : « %s » attendra.", day, item["title"])
            return True
        log.info("Reprise d'une publication interrompue aujourd'hui.")

    def give_up(message: str, keep_slot: bool = False) -> bool:
        if not keep_slot:
            storage.delete(slot_key)
        item.update(publish_failures=item.get("publish_failures", 0) + 1, publish_error=logs.redact(message)[:500])
        book.save(item)
        log.error("Publication impossible : %s", message)
        return False

    try:
        status = youtube.video_status(creds, item["video_id"])
        if status is None:
            return give_up(f"la vidéo {item['video_id']} est introuvable sur la chaîne (supprimée ?)")
        if status.get("privacyStatus") == "public":
            privacy = "public"
            if fresh:
                storage.delete(slot_key)  # déjà publique : rien n'a été publié aujourd'hui
                fresh = False
        else:
            log.info("Passage en PUBLIC de « %s » (%s)…", item["title"], url)
            privacy = youtube.make_public(creds, item["video_id"], status)
    except Exception as error:  # noqa: BLE001 - on classe l'erreur pour décider de la suite
        if isinstance(error, youtube.AuthError) or youtube.is_auth_error(error):
            storage.delete(slot_key)
            raise youtube.auth_error(error) from None
        code = getattr(getattr(error, "resp", None), "status", None)
        if code is not None and 400 <= int(code) < 500:
            return give_up(f"refus de YouTube (HTTP {code}) : {type(error).__name__}: {error}")
        log.error("Publication interrompue, issue inconnue : la prochaine exécution vérifiera l'état réel de la vidéo.")
        return False
    if privacy != "public":
        return give_up(f"YouTube indique la visibilité « {privacy} » après la demande")

    item.update(status=proposals.PUBLISHED, privacy="public", public_at=ledger_module.now())
    book.save(item)
    if storage.read(slot_key):
        slot.update(phase="done", at=ledger_module.now())
        storage.write(slot_key, json.dumps(slot).encode())
    log.info("Clip PUBLIC : %s — visibilité public confirmée par YouTube.", url)
    return True
