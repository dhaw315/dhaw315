"""Traitement par lot : source non traitée -> clip vertical -> envoi privé.

Disposition du stockage durable :
    sources/   vidéos déposées (+ fiche .json et sous-titres .srt facultatifs, de même nom)
    clips/     clips produits et leur fiche
    state/     historique des tentatives, index, comptes rendus d'exécution
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import clipmeta, config, ledger as ledger_module, logs, media, youtube
from .ledger import Ledger

log = logging.getLogger("ytclips")

SOURCES, CLIPS, RUNS, ANALYSIS = "sources/", "clips/", "state/runs/", "state/analysis/"
RESERVED = "state/reserved/"  # sources que batch ne doit pas découper tout seul
LONG_SOURCE_MS = 600_000
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
SPEC_KEYS = {"start", "end", "title", "description", "tags", "background", "srt_timing"}

UPLOADED, SIMULATED, FAILED, SKIPPED, WAITING = "envoyé", "simulé", "échec", "ignoré", "en attente"
ATTENTION = "à vérifier"  # bloqué ou abandonné : une action manuelle est attendue


class SpecError(clipmeta.MetaError):
    pass


@dataclass
class Source:
    key: str
    spec_key: str | None
    srt_key: str | None
    fingerprint: str


@dataclass
class Outcome:
    source: str
    result: str
    detail: str
    stop: bool = False  # inutile de poursuivre le lot (problème côté YouTube)
    video_id: str = ""


def list_sources(storage) -> list[Source]:
    objects = {info.key: info for info in storage.list(SOURCES)}
    sources = []
    for key in sorted(objects):
        path = PurePosixPath(key)
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        spec = objects.get(str(path.with_suffix(".json")))
        srt = objects.get(str(path.with_suffix(".srt")))
        # Toute modification de la vidéo, de sa fiche ou de ses sous-titres donne une nouvelle empreinte.
        parts = [key, objects[key].etag, str(objects[key].size), spec.etag if spec else "", srt.etag if srt else ""]
        sources.append(
            Source(
                key, spec.key if spec else None, srt.key if srt else None,
                hashlib.sha256("|".join(parts).encode()).hexdigest()[:32],
            )
        )
    return sources


def _load_spec(storage, source: Source) -> dict:
    if not source.spec_key:
        return {}
    raw = storage.read(source.spec_key) or b""
    try:
        spec = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise SpecError(f"Fiche {source.spec_key} : JSON illisible.") from None
    if not isinstance(spec, dict):
        raise SpecError(f"Fiche {source.spec_key} : un objet JSON est attendu.")
    unknown = sorted(set(spec) - SPEC_KEYS)
    if unknown:
        raise SpecError(
            f"Fiche {source.spec_key} : champ(s) inconnu(s) {', '.join(unknown)} "
            f"(acceptés : {', '.join(sorted(SPEC_KEYS))})."
        )
    return spec


def _resolve_range(spec: dict, duration_ms: int) -> tuple[int, int]:
    """Sans fiche : du début de la source, jusqu'à 60 secondes."""
    start_ms = media.parse_timecode(str(spec["start"])) if "start" in spec else 0
    if "end" in spec:
        end_ms = media.parse_timecode(str(spec["end"]))
    else:
        end_ms = min(start_ms + config.MAX_CLIP_MS, duration_ms)
    return start_ms, end_ms


def _skip(label: str, decision) -> Outcome:
    needs_action = decision.action in (ledger_module.BLOCKED, ledger_module.EXHAUSTED)
    return Outcome(label, ATTENTION if needs_action else SKIPPED, decision.reason.splitlines()[0])


def _tags(value) -> list[str]:
    if isinstance(value, str):
        return [tag.strip() for tag in value.split(",") if tag.strip()]
    return list(value or [])


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:60] or "clip"


def send_extract(
    storage, ledger: Ledger, creds, run_id: str, *, label: str, source_key: str, srt_key: str | None,
    local: Path, meta: dict, background: str, srt_timing: str, attempt: int, work: Path,
    key: str | None = None, render=None, extra: dict | None = None,
) -> Outcome:
    """Réserve l'extrait, crée le clip, le conserve puis l'envoie en privé. `meta` est déjà validée."""
    clip = meta["clip"]
    start_ms, end_ms = clip["start_ms"], clip["end_ms"]
    key = key or ledger_module.content_key(clip["source_sha256"], start_ms, end_ms)
    record = ledger.claim(key, attempt, {
        "run_id": run_id, "source_key": source_key, "source_sha256": clip["source_sha256"],
        "start_ms": start_ms, "end_ms": end_ms, "title": meta["youtube"]["title"], "clip_key": None,
        **(extra or {}),
    })
    if record is None:
        return Outcome(label, SKIPPED, "réservé au même instant par une autre exécution")

    # Avant l'envoi : aucune vidéo ne peut exister, tout arrêt est consigné comme un échec sûr.
    try:
        output = work / clip["file"]
        if render is not None:
            produced = render(output)
        else:
            srt_local = None
            if srt_key:
                srt_local = work / "source.srt"
                storage.download(srt_key, srt_local)
            log.info(
                "Création du clip : de %s à %s, fond « %s ».",
                media.format_timecode(start_ms), media.format_timecode(end_ms), background,
            )
            produced = media.render_clip(local, output, start_ms, end_ms, background, srt_local, srt_timing)
        clip.update(sha256=media.sha256_file(output), subtitles=produced["subtitles"])
        meta_file = output.with_suffix(".json")
        clipmeta.save(meta_file, meta)
        clip_key = f"{CLIPS}{output.stem}_{key[:8]}.mp4"
        storage.upload(output, clip_key)
        storage.upload(meta_file, clip_key[:-4] + ".json")
        ledger.update(record, phase=ledger_module.RENDERED, clip_key=clip_key)
        body = clipmeta.request_body(meta)
    except media.MediaError as error:
        ledger.fail(record, "error", str(error))
        return Outcome(label, FAILED, str(error).splitlines()[0])
    except BaseException as error:
        ledger.fail(record, "interrupted", logs.redact(f"{type(error).__name__}: {error}"))
        raise

    ledger.update(record, phase=ledger_module.UPLOADING)
    conflict = ledger.conflict(record)
    if conflict:
        ledger.fail(record, "superseded", conflict)
        return Outcome(label, SKIPPED, conflict)

    log.info("Envoi en mode PRIVÉ de « %s »…", body["snippet"]["title"])
    try:
        response = youtube.upload_private(creds, output, body)
    except Exception as error:  # noqa: BLE001 - on classe l'erreur pour décider de la suite
        message = logs.redact(f"{type(error).__name__}: {error}")
        if youtube.is_auth_error(error):
            # Requête rejetée avant d'aboutir : aucune vidéo créée.
            ledger.fail(record, "auth", message)
            raise youtube.auth_error(error) from None
        status = getattr(getattr(error, "resp", None), "status", None)
        if status is not None and 400 <= int(status) < 500:
            ledger.fail(record, "error", message)
            return Outcome(label, FAILED, f"refusé par YouTube (HTTP {status}), aucune vidéo créée", stop=True)
        ledger.update(record, error=message[:1000])
        return Outcome(
            label, FAILED,
            "envoi interrompu, issue inconnue : vérifier YouTube Studio, puis "
            f"« ytclips history --remote --release {key[:12]} » si aucune vidéo n'a été créée",
            stop=True,
        )

    video_id = response.get("id", "")
    privacy = response.get("status", {}).get("privacyStatus", "inconnue")
    ledger.update(record, status=ledger_module.UPLOADED, phase=ledger_module.FINISHED, video_id=video_id, privacy=privacy)
    if privacy != config.PRIVACY:
        return Outcome(
            label, FAILED,
            f"ALERTE : vidéo {video_id} envoyée, mais YouTube indique la visibilité « {privacy} »",
            stop=True, video_id=video_id,
        )
    return Outcome(label, UPLOADED, f"https://youtu.be/{video_id} — visibilité private confirmée", video_id=video_id)


def _process(storage, ledger: Ledger, source: Source, creds, run_id: str, dry_run: bool,
             stale_seconds: float, max_attempts: int, work: Path) -> Outcome:
    name = PurePosixPath(source.key)
    local = work / f"source{name.suffix.lower()}"
    log.info("Source « %s » : téléchargement…", source.key)
    storage.download(source.key, local)
    source_sha = media.sha256_file(local)

    # Préparation : toute erreur ici est propre à la source et sera consignée.
    problem = None
    start_ms = end_ms = -1
    try:
        spec = _load_spec(storage, source)
        info = media.probe(local)
        start_ms, end_ms = _resolve_range(spec, info["duration_ms"])
        media.validate_range(start_ms, end_ms, info["duration_ms"])
        background = media.validate_background(str(spec.get("background", "blur")))
        srt_timing = spec.get("srt_timing", "source")
        if srt_timing not in ("source", "clip"):
            raise SpecError("« srt_timing » doit valoir « source » ou « clip ».")
        title = str(spec.get("title") or name.stem.replace("_", " ").strip())[:100]
        meta = clipmeta.new_meta(
            clip_file=f"{slug(name.stem)}_{start_ms}-{end_ms}.mp4", clip_sha256="0",
            source_name=name.name, source_sha256=source_sha,
            start_ms=start_ms, end_ms=end_ms, background=background, subtitles=False,
            title=title, description=str(spec.get("description", "")), tags=_tags(spec.get("tags")),
        )
        clipmeta.validate(meta)
    except (media.MediaError, clipmeta.MetaError) as error:
        problem = str(error)
        start_ms = end_ms = -1

    # Un film entier sans fiche manuelle n'est pas découpé d'office : ses 60 premières secondes n'ont pas de sens.
    if problem is None and not source.spec_key and info["duration_ms"] > LONG_SOURCE_MS:
        if not dry_run:
            storage.write(f"{RESERVED}{source.fingerprint}.json", json.dumps({
                "source_key": source.key, "reason": "long", "duration_ms": info["duration_ms"], "at": ledger_module.now(),
            }).encode())
        return Outcome(
            source.key, SKIPPED,
            f"source de plus de {LONG_SOURCE_MS // 60000} minutes sans fiche : utiliser « ytclips montage » ou « ytclips propose »",
        )

    key = ledger_module.content_key(source_sha, start_ms, end_ms)
    decision = ledger.decide(key, stale_seconds, max_attempts)

    if dry_run:
        if problem:
            return Outcome(source.key, FAILED, f"SIMULATION — {problem.splitlines()[0]}")
        if decision.action != ledger_module.TAKE:
            return _skip(source.key, decision)
        srt_local = None
        if source.srt_key:
            srt_local = work / "source.srt"
            storage.download(source.srt_key, srt_local)
        produced = media.render_clip(local, work / meta["clip"]["file"], start_ms, end_ms, background, srt_local, srt_timing)
        log.info(
            "SIMULATION : requête videos.insert qui serait envoyée :\n%s",
            json.dumps(clipmeta.request_body(meta), ensure_ascii=False, indent=2),
        )
        return Outcome(
            source.key, SIMULATED,
            f"clip {produced['width']}x{produced['height']} de {produced['duration_ms'] / 1000:.1f} s créé puis jeté ; "
            "rien écrit dans le stockage, aucun appel à YouTube",
        )

    ledger.remember(source.fingerprint, {
        "content_key": key, "source_key": source.key, "start_ms": start_ms, "end_ms": end_ms,
    })
    if decision.action != ledger_module.TAKE:
        return _skip(source.key, decision)

    if problem:
        record = ledger.claim(key, len(decision.attempts) + 1, {
            "run_id": run_id, "source_key": source.key, "source_sha256": source_sha,
            "start_ms": start_ms, "end_ms": end_ms, "title": None, "clip_key": None,
        })
        if record is None:
            return Outcome(source.key, SKIPPED, "réservé au même instant par une autre exécution")
        ledger.fail(record, "error", problem)
        return Outcome(source.key, FAILED, problem.splitlines()[0])

    return send_extract(
        storage, ledger, creds, run_id, label=source.key, source_key=source.key, srt_key=source.srt_key,
        local=local, meta=meta, background=background, srt_timing=srt_timing,
        attempt=len(decision.attempts) + 1, work=work,
    )


def save_report(storage, run_id: str, kind: str, outcomes: list[Outcome]) -> None:
    report = {"run_id": run_id, "kind": kind, "at": ledger_module.now(), "outcomes": [vars(o) for o in outcomes]}
    storage.write(
        f"{RUNS}{report['at'].replace(':', '')}-{run_id}.json",
        logs.redact(json.dumps(report, ensure_ascii=False, indent=2)).encode(),
    )


def log_outcomes(outcomes: list[Outcome]) -> None:
    for outcome in outcomes:
        log.info("  [%s] %s — %s", outcome.result, outcome.source, outcome.detail)
    waiting = sum(1 for outcome in outcomes if outcome.result == ATTENTION)
    if waiting:
        log.warning("%d élément(s) « à vérifier » : une action manuelle est attendue, ils ne seront pas retentés.", waiting)


def run(storage, *, limit: int, dry_run: bool, stale_seconds: float, max_attempts: int) -> tuple[list[Outcome], bool]:
    """Traite au plus `limit` sources. Renvoie les résultats et True si tout s'est bien passé."""
    run_id = uuid.uuid4().hex[:12]
    ledger = Ledger(storage)
    log.info("Lot %s — stockage : %s%s", run_id, storage.describe(), " — SIMULATION" if dry_run else "")

    # Vérifié avant toute réservation : une autorisation expirée ne laisse aucune trace à nettoyer.
    creds = None if dry_run else youtube.load_credentials()

    outcomes: list[Outcome] = []
    handled = 0
    stopped = False
    auth_failure = None
    for source in list_sources(storage):
        # Une source analysée par « propose » et sans fiche manuelle relève de la sélection automatique.
        if not source.spec_key and storage.read(f"{ANALYSIS}{source.fingerprint}.json"):
            outcomes.append(Outcome(source.key, SKIPPED, "gérée par la sélection automatique (voir « ytclips proposals »)"))
            continue
        if not source.spec_key and storage.read(f"{RESERVED}{source.fingerprint}.json"):
            outcomes.append(Outcome(source.key, SKIPPED, "film réservé au montage ou trop long pour un clip automatique"))
            continue
        known = ledger.lookup(source.fingerprint)
        if known:
            decision = ledger.decide(known["content_key"], stale_seconds, max_attempts)
            if decision.action != ledger_module.TAKE:
                outcomes.append(_skip(source.key, decision))
                continue
        if stopped or handled >= limit:
            outcomes.append(Outcome(source.key, WAITING, "sera traitée par une prochaine exécution"))
            continue
        with tempfile.TemporaryDirectory(prefix="ytclips-batch-") as work:
            try:
                outcome = _process(
                    storage, ledger, source, creds, run_id, dry_run, stale_seconds, max_attempts, Path(work)
                )
            except youtube.AuthError as error:
                auth_failure = error
                outcome = Outcome(source.key, FAILED, "autorisation Google refusée pendant l'envoi", stop=True)
        outcomes.append(outcome)
        if outcome.result not in (SKIPPED, ATTENTION):
            handled += 1
        stopped = stopped or outcome.stop

    log_outcomes(outcomes)
    if not outcomes:
        log.info("Aucune vidéo dans « %s » du stockage.", SOURCES)
    elif handled == 0:
        log.info("Rien à traiter : aucune source nouvelle.")

    if not dry_run:
        save_report(storage, run_id, "batch", outcomes)
    if auth_failure:
        raise auth_failure
    return outcomes, not any(outcome.result == FAILED for outcome in outcomes)
