"""Montages conservés dans le stockage durable : analyse réutilisable, plan, envoi privé.

    state/signals/<film>.json       mesures du film (plans, silences, bandes noires), calculées une fois
    state/transcripts/<film>.srt    sous-titres extraits ou transcrits, réutilisés ensuite
    state/montages/<id>.json        plan : passages, timecodes, raison de chaque choix, état

<film> identifie le fichier vidéo du stockage (clé, empreinte et taille) : tant qu'il ne change
pas, ni l'analyse ni la transcription ne sont refaites.
"""

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from . import analysis, batch, clipmeta, ledger as ledger_module, media, montage, selection, tiktok, youtube
from .ledger import Ledger
from .selection import Signals

log = logging.getLogger("ytclips")

PREFIX, SIGNALS, TRANSCRIPTS = "state/montages/", "state/signals/", "state/transcripts/"
PLANNED, BUILT, SENT = "planned", "built", "sent"
LABELS = {PLANNED: "planifié", BUILT: "monté", SENT: "envoyé"}
FRENCH = {"fre", "fra", "fr"}

NO_SUBTITLES = (
    "Aucun sous-titre pour ce film : ni fichier .srt de même nom dans « sources/ », ni transcription "
    "enregistrée, ni piste de sous-titres texte dans le fichier.\n"
    "Sans dialogues, les passages ne peuvent pas être choisis. Solutions :\n"
    "  - déposer le fichier .srt français à côté du film ;\n"
    "  - lancer « ytclips transcribe » : transcription locale et gratuite, mais longue (voir le README)."
)


class Film:
    """Une vidéo du stockage ; le fichier n'est téléchargé que si une étape en a besoin."""

    def __init__(self, storage, source_key: str | None, work: str):
        sources = {source.key: source for source in batch.list_sources(storage)}
        if source_key is None:
            if len(sources) != 1:
                raise media.MediaError(
                    "Préciser la vidéo avec --source. Vidéos présentes : " + (", ".join(sources) or "aucune") + "."
                )
            source_key = next(iter(sources))
        if source_key not in sources:
            raise media.MediaError(f"Vidéo introuvable dans le stockage : {source_key}")
        self.storage, self.source, self.work = storage, sources[source_key], Path(work)
        info = next(item for item in storage.list(source_key) if item.key == source_key)
        self.size = info.size
        self.film_id = hashlib.sha256(f"{source_key}|{info.etag}|{info.size}".encode()).hexdigest()[:32]
        self._local: Path | None = None

    @property
    def key(self) -> str:
        return self.source.key

    @property
    def title(self) -> str:
        return PurePosixPath(self.key).stem.replace("_", " ").strip()

    def local(self) -> Path:
        if self._local is None:
            path = self.work / f"film{PurePosixPath(self.key).suffix.lower()}"
            log.info("  Téléchargement de « %s » (%.0f Mo)…", self.key, self.size / 1e6)
            self.storage.download(self.key, path)
            self._local = path
        return self._local


def measures(film: Film, save: bool = True) -> dict:
    """Mesures du film entier, relues depuis le stockage si elles y sont déjà."""
    key = f"{SIGNALS}{film.film_id}.json"
    raw = film.storage.read(key)
    if raw:
        log.info("  Analyse du film : réutilisée depuis le stockage.")
        return json.loads(raw)
    local = film.local()
    info = media.probe(local)
    log.info("  Analyse du film entier (%s) : changements de plan, silences, bandes noires…", media.format_timecode(info["duration_ms"]))
    signals = analysis.analyze(local, info)
    data = {
        "source_key": film.key, "source_sha256": media.sha256_file(local), "info": info,
        "scene_cuts": signals.scene_cuts, "silences": signals.silences,
        "crop": analysis.detect_crop(local, info), "analyzed_at": ledger_module.now(),
    }
    if save:
        film.storage.write(key, json.dumps(data).encode())
    return data


def to_signals(data: dict) -> Signals:
    return Signals(
        data["info"]["duration_ms"], data["scene_cuts"], [tuple(pair) for pair in data["silences"]],
        data["info"]["has_audio"],
    )


def save_transcript(film: Film, text: str, origin: str) -> None:
    film.storage.write(f"{TRANSCRIPTS}{film.film_id}.srt", text.encode("utf-8"))
    film.storage.write(f"{TRANSCRIPTS}{film.film_id}.json", json.dumps({
        "source_key": film.key, "origin": origin, "created_at": ledger_module.now(),
    }, ensure_ascii=False).encode())


def subtitles(film: Film, save: bool = True) -> tuple[str, str] | None:
    """Sous-titres du film et leur provenance : fichier fourni, copie enregistrée, puis piste intégrée."""
    storage = film.storage
    if film.source.srt_key:
        return media.decode_text(storage.read(film.source.srt_key) or b""), "fichier .srt fourni"
    raw = storage.read(f"{TRANSCRIPTS}{film.film_id}.srt")
    if raw:
        meta = json.loads(storage.read(f"{TRANSCRIPTS}{film.film_id}.json") or b"{}")
        return raw.decode("utf-8"), f"{meta.get('origin', 'transcription')} (réutilisée depuis le stockage)"
    tracks = [track for track in media.subtitle_streams(film.local()) if track["codec"] in media.TEXT_SUBTITLES]
    if not tracks:
        return None
    track = next((t for t in tracks if t["language"] in FRENCH), None)
    if track is None:
        track = tracks[0]
        log.warning(
            "  Aucune piste de sous-titres française dans le fichier : piste « %s » utilisée. "
            "Pour des sous-titres français, déposer un fichier .srt.", track["language"] or "langue non indiquée",
        )
    text = media.extract_subtitles(film.local(), track["position"], film.work / "integres.srt")
    origin = f"piste de sous-titres intégrée ({track['language'] or 'langue non indiquée'})"
    if save:
        save_transcript(film, text, origin)
    return text, origin


def transcribe(storage, source_key: str | None, *, model: str, language: str | None, force: bool) -> str:
    """Enregistre des sous-titres pour le film : piste intégrée si possible, sinon transcription locale."""
    from . import transcribe as engine

    with tempfile.TemporaryDirectory(prefix="ytclips-transcribe-") as work:
        film = Film(storage, source_key, work)
        found = None if force else subtitles(film)
        if found:
            log.info("Sous-titres disponibles : %s. Aucune transcription nécessaire.", found[1])
            return found[1]
        text, detected = engine.transcribe(film.local(), Path(work), model, language)
        origin = f"transcription locale (faster-whisper « {model} », langue {detected})"
        save_transcript(film, text, origin)
    log.info("Transcription enregistrée dans le stockage : %d réplique(s), %s.", text.count("-->"), origin)
    return origin


# --- plans -------------------------------------------------------------------

def records(storage) -> list[dict]:
    items = []
    for info in storage.list(PREFIX):
        if not info.key.endswith(".json"):
            continue  # sous-titres propres à un montage
        raw = storage.read(info.key)
        if raw:
            items.append(json.loads(raw))
    return sorted(items, key=lambda item: item["created_at"])


def find(storage, prefix: str | None) -> dict:
    items = records(storage)
    if prefix is None:
        items = [item for item in items if item["status"] != SENT]
        if len(items) != 1:
            raise clipmeta.MetaError(f"{len(items)} montage(s) en attente : préciser l'identifiant (voir « ytclips montage list »).")
        return items[0]
    matches = [item for item in items if item["content_key"].startswith(prefix)]
    if len(matches) != 1:
        raise clipmeta.MetaError(f"Aucun montage unique ne commence par « {prefix} ».")
    return matches[0]


def _save(storage, item: dict) -> None:
    item["updated_at"] = ledger_module.now()
    storage.write(f"{PREFIX}{item['content_key']}.json", json.dumps(item, ensure_ascii=False, indent=2).encode())


def make_plan(
    storage, source_key: str | None, *, dry_run: bool = False, cutoff: float = montage.SPOILER_CUTOFF,
    skip_start_ms: int = 0, skip_end_ms: int = 0, ranges: list[tuple[int, int]] | None = None,
    zoom: float = 1.0, background: str = "blur", title: str | None = None, cut_bottom: float = 0.0,
) -> dict | None:
    """Analyse le film (ou relit son analyse) et enregistre un plan de montage."""
    background = media.validate_background(background)
    if not 1.0 <= zoom <= 2.0:
        raise media.MediaError("Le zoom doit être compris entre 1.0 (image entière) et 2.0.")
    if not 0.0 <= cut_bottom <= 0.4:
        raise media.MediaError("--cut-bottom doit être compris entre 0 et 0.4.")
    if not 0.2 <= cutoff <= 1.0:
        raise media.MediaError("--cutoff doit être compris entre 0.2 et 1.0.")
    with tempfile.TemporaryDirectory(prefix="ytclips-montage-") as work:
        film = Film(storage, source_key, work)
        log.info("Film « %s »", film.key)
        data = measures(film, save=not dry_run)
        found = subtitles(film, save=not dry_run)
    if found is None:
        raise media.MediaError(NO_SUBTITLES)
    text, origin = found
    cues = selection.make_cues(media.parse_srt(text))
    signals = to_signals(data)
    speech = sum(1 for cue in cues if not cue.music)
    log.info(
        "  Mesures : durée %s, %d changement(s) de plan, %d silence(s), %d réplique(s) — sous-titres : %s.",
        media.format_timecode(signals.duration_ms), len(signals.scene_cuts), len(signals.silences), speech, origin,
    )
    name = title or film.title
    if ranges:
        chosen = montage.manual(ranges, signals, cues, name)
    else:
        chosen = montage.plan(signals, cues, name, cutoff=cutoff, skip_start_ms=skip_start_ms, skip_end_ms=skip_end_ms)
    if chosen is None:
        log.error(
            "Aucun montage possible : il faut 4 à 6 passages de dialogue répartis sur le film et totalisant "
            "exactement 60 s (%d réplique(s) exploitables avant la limite fixée).", speech,
        )
        return None

    spans = [(segment.start_ms, segment.end_ms) for segment in chosen.segments]
    crop = data.get("crop")
    if cut_bottom:
        # Retire le bas de l'image, par exemple des sous-titres incrustés dans une autre langue.
        width, height, x, y = (int(v) for v in (crop or f"{data['info']['width']}:{data['info']['height']}:0:0").split(":"))
        crop = f"{width}:{int(height * (1 - cut_bottom)) // 2 * 2}:{x}:{y}"
    identity = f"{data['source_sha256']}:montage:{zoom}:{background}:" + ",".join(f"{s}-{e}" for s, e in spans)
    if cut_bottom:
        identity += f":bas{cut_bottom}"
    stamp = ledger_module.now()
    item = {
        "content_key": hashlib.sha256(identity.encode()).hexdigest()[:40], "status": PLANNED,
        "source_key": film.key, "source_sha256": data["source_sha256"], "film_id": film.film_id,
        "title": chosen.title, "description": chosen.description, "total_ms": chosen.total_ms,
        "nothing_after_ms": chosen.limit_ms, "subtitles_origin": origin, "whole_sentences": chosen.whole_sentences,
        "zoom": zoom, "background": background, "crop": crop, "cut_bottom": cut_bottom,
        "segments": [
            {
                "role": segment.role, "start_ms": segment.start_ms, "end_ms": segment.end_ms,
                "start": media.format_timecode(segment.start_ms), "end": media.format_timecode(segment.end_ms),
                "duration_ms": segment.end_ms - segment.start_ms, "text": segment.text,
                "reason": segment.reason, "score": segment.score,
            }
            for segment in chosen.segments
        ],
        "video_id": None, "privacy": None, "clip_key": None, "created_at": stamp, "updated_at": stamp,
    }
    describe(item)
    if dry_run:
        log.info("SIMULATION : rien n'a été écrit dans le stockage.")
        return item
    created = storage.write(f"{PREFIX}{item['content_key']}.json", json.dumps(item, ensure_ascii=False, indent=2).encode(), if_absent=True)
    # Le traitement par lot ne doit pas découper ce film de son côté.
    storage.write(f"{batch.RESERVED}{film.source.fingerprint}.json", json.dumps({
        "source_key": film.key, "reason": "montage", "at": stamp,
    }).encode())
    log.info("Plan %s : %s.", item["content_key"][:12], "enregistré" if created else "identique à un plan déjà enregistré")
    return item


def describe(item: dict) -> None:
    video = f"https://youtu.be/{item['video_id']} ({item.get('privacy')})" if item.get("video_id") else "pas encore envoyé"
    lines = [
        f"{item['content_key'][:12]} | {LABELS[item['status']]} | {item['source_key']} | "
        f"{len(item['segments'])} passages, {item['total_ms'] / 1000:.3f} s | {video}",
        f"      titre : {item['title']}",
        f"      rien n'est pris après {media.format_timecode(item['nothing_after_ms'])[:8]} du film ; "
        f"sous-titres : {item['subtitles_origin']} ; zoom {item['zoom']:g}"
        + (f" ; image recadrée ({item['crop']})" if item.get("crop") else "")
        + (f" ; bas de l'image retiré sur {item['cut_bottom']:.0%}" if item.get("cut_bottom") else ""),
    ]
    for number, segment in enumerate(item["segments"], 1):
        lines.append(
            f"      {number}. {segment['start']} → {segment['end']} ({segment['duration_ms'] / 1000:.2f} s)\n"
            f"         « {selection._safe(segment['text'], 110)} »\n         {segment['reason']}"
        )
    log.info("%s", "\n".join(lines))


def _clip_key(item: dict) -> str:
    stem = f"{batch.slug(PurePosixPath(item['source_key']).stem)}_montage"
    return f"{batch.CLIPS}{stem}_{item['content_key'][:8]}.mp4"


def _stored(storage, item: dict) -> bool:
    key = item.get("clip_key")
    return bool(key) and any(info.key == key for info in storage.list(key))


def _own_subtitles(storage, item: dict) -> str | None:
    raw = storage.read(f"{PREFIX}{item['content_key']}.srt")
    return raw.decode("utf-8") if raw else None


def set_subtitles(storage, prefix: str | None, text: str) -> dict:
    """Sous-titres propres à un montage (une traduction, par exemple), calés sur les temps du film."""
    item = find(storage, prefix)
    if item["status"] == SENT:
        raise clipmeta.MetaError("Ce montage a déjà été envoyé : ses sous-titres ne changent plus.")
    spans = [(segment["start_ms"], segment["end_ms"]) for segment in item["segments"]]
    shown = montage.compose_srt(selection.make_cues(media.parse_srt(text)), spans)
    if not shown:
        raise clipmeta.MetaError(
            "Aucune réplique de ce fichier ne tombe dans les passages du montage (les temps attendus sont ceux du film)."
        )
    storage.write(f"{PREFIX}{item['content_key']}.srt", text.encode("utf-8"))
    # Un montage déjà créé avec d'autres sous-titres est à refaire.
    item.update(subtitles_origin="fichier propre à ce montage", status=PLANNED, clip_key=None)
    _save(storage, item)
    log.info("Sous-titres enregistrés pour le montage %s : %d réplique(s) affichée(s).", item["content_key"][:12], shown.count("-->"))
    return item


class _Job:
    """Tout ce qu'il faut pour créer le montage d'un plan : film, sous-titres recalés, fiche du clip."""

    def __init__(self, storage, item: dict, work: str):
        self.storage, self.item = storage, item
        self.spans = [(segment["start_ms"], segment["end_ms"]) for segment in item["segments"]]
        self.film = Film(storage, item["source_key"], work)
        if self.film.film_id != item["film_id"]:
            raise media.MediaError("Le film a changé dans le stockage depuis ce plan : relancer « ytclips montage plan ».")
        text = _own_subtitles(storage, item)
        if text is None:
            found = subtitles(self.film, save=False)
            if found is None:
                raise media.MediaError(NO_SUBTITLES)
            text = found[0]
        self.srt_text = montage.compose_srt(selection.make_cues(media.parse_srt(text)), self.spans)
        name = PurePosixPath(self.film.key)
        self.meta = clipmeta.new_meta(
            clip_file=f"{batch.slug(name.stem)}_montage.mp4", clip_sha256="0", source_name=name.name,
            source_sha256=item["source_sha256"], start_ms=0, end_ms=item["total_ms"],
            background=item["background"], subtitles=False, title=item["title"], description=item["description"], tags=[],
        )
        self.meta["clip"].update(content_key=item["content_key"], segments=[list(span) for span in self.spans])
        clipmeta.validate(self.meta)

    def render(self, output: Path) -> dict:
        item = self.item
        if _stored(self.storage, item):
            # Le fichier déjà monté et visionné est celui qui part : pas de second rendu.
            log.info("Montage déjà créé : réutilisation de %s.", item["clip_key"])
            self.storage.download(item["clip_key"], output)
            produced = media.probe(output)
            saved = json.loads(self.storage.read(item["clip_key"][:-4] + ".json") or b"{}")
            produced["subtitles"] = bool(saved.get("clip", {}).get("subtitles"))
            return produced
        log.info("Montage de %d passages, %.0f s, zoom %g…", len(self.spans), item["total_ms"] / 1000, item["zoom"])
        return media.render_montage(
            self.film.local(), output, self.spans, background=item["background"], srt_text=self.srt_text or None,
            zoom=item["zoom"], crop=item.get("crop"),
        )


def build(storage, prefix: str | None, *, force: bool = False) -> dict:
    """Crée le montage et le conserve dans le stockage, sans rien envoyer à YouTube ni à TikTok."""
    item = find(storage, prefix)
    if force and item["status"] == SENT:
        raise clipmeta.MetaError("Ce montage a déjà été envoyé : il n'est plus recréé (faire un nouveau plan).")
    if _stored(storage, item) and not force:
        log.info("Montage déjà créé : %s.", item["clip_key"])
    else:
        if force:
            item["clip_key"] = None
        with tempfile.TemporaryDirectory(prefix="ytclips-montage-") as work:
            job = _Job(storage, item, work)
            output = Path(work) / job.meta["clip"]["file"]
            produced = job.render(output)
            job.meta["clip"].update(sha256=media.sha256_file(output), subtitles=produced["subtitles"])
            meta_file = output.with_suffix(".json")
            clipmeta.save(meta_file, job.meta)
            clip_key = _clip_key(item)
            storage.upload(output, clip_key)
            storage.upload(meta_file, clip_key[:-4] + ".json")
        item.update(clip_key=clip_key, status=BUILT if item["status"] == PLANNED else item["status"])
        _save(storage, item)
        log.info(
            "Montage créé : %s (%dx%d, %.2f s, sous-titres : %s). Rien n'a été envoyé.", clip_key,
            produced["width"], produced["height"], produced["duration_ms"] / 1000, "oui" if produced["subtitles"] else "non",
        )
    tiktok.prepare(storage)
    return item


def link(storage, prefix: str | None, hours: float) -> tuple[dict, str]:
    """Lien de téléchargement temporaire du montage déjà créé."""
    if not 0 < hours <= tiktok.MAX_LINK_HOURS:
        raise clipmeta.MetaError(f"--hours doit être compris entre 0 et {tiktok.MAX_LINK_HOURS} (7 jours).")
    item = find(storage, prefix)
    if not _stored(storage, item):
        raise clipmeta.MetaError("Ce montage n'a pas encore été créé : lancer « ytclips montage build ».")
    return item, storage.presign(item["clip_key"], int(hours * 3600))


def send(storage, prefix: str | None, *, dry_run: bool, stale_seconds: float, max_attempts: int) -> bool:
    """Crée le montage s'il ne l'est pas déjà, l'envoie en PRIVÉ sur YouTube et prépare sa fiche TikTok."""
    item = find(storage, prefix)
    key = item["content_key"]
    ledger = Ledger(storage)
    decision = ledger.decide(key, stale_seconds, max_attempts)
    if decision.action == ledger_module.DONE:
        done = next(r for r in decision.attempts if r["status"] == ledger_module.UPLOADED)
        if item.get("video_id") != done.get("video_id"):
            item.update(status=SENT, video_id=done.get("video_id"), privacy=done.get("privacy"), clip_key=done.get("clip_key"))
            _save(storage, item)
        log.info("Montage déjà envoyé : https://youtu.be/%s — aucun nouvel envoi.", item["video_id"])
        tiktok.prepare(storage)
        return True
    label = f"montage {key[:12]} {item['source_key']}"
    if decision.action != ledger_module.TAKE:
        batch.log_outcomes([batch._skip(label, decision)])
        return decision.action == ledger_module.BUSY

    creds = None if dry_run else youtube.load_credentials()
    run_id = uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix="ytclips-montage-") as work:
        job = _Job(storage, item, work)
        if dry_run:
            produced = job.render(Path(work) / "essai.mp4")
            log.info(
                "SIMULATION : montage %dx%d de %.2f s prêt puis jeté ; rien écrit dans le stockage, aucun appel à YouTube.",
                produced["width"], produced["height"], produced["duration_ms"] / 1000,
            )
            return True
        outcome = batch.send_extract(
            storage, ledger, creds, run_id, label=label, source_key=job.film.key, srt_key=None, local=None,
            meta=job.meta, background=item["background"], srt_timing="source", attempt=len(decision.attempts) + 1,
            work=Path(work), key=key, render=job.render,
            extra={"start_ms": -1, "end_ms": -1, "segments": [list(span) for span in job.spans]},
        )
    if outcome.video_id:
        item.update(status=SENT, video_id=outcome.video_id, privacy="private", clip_key=_clip_key(item))
        _save(storage, item)
    batch.log_outcomes([outcome])
    batch.save_report(storage, run_id, "montage", [outcome])
    # Le MP4 est dans le stockage dès qu'il est créé : TikTok ne dépend pas du résultat côté YouTube.
    tiktok.prepare(storage)
    return outcome.result == batch.UPLOADED
