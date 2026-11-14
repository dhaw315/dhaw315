"""Interface en ligne de commande : check, auth, clip, upload, batch, history."""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from . import (
    __version__, batch, clipmeta, config, curate_cli, curation, history, ledger, logs, media, montage_cli, storage,
    tiktok_cli, youtube,
)

log = logging.getLogger("ytclips")

EXIT_OK, EXIT_ERROR, EXIT_DUPLICATE, EXIT_AUTH = 0, 1, 3, 4
GOOGLE_LIBS = ("googleapiclient", "google.oauth2", "google_auth_oauthlib", "google_auth_httplib2")


# --- check -----------------------------------------------------------------

def _reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10)
        return True
    except urllib.error.HTTPError:
        return True  # le serveur a répondu : le réseau passe
    except Exception:
        return False


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        return False


def _check_storage(report, offline: bool) -> bool:
    """Vérifie le stockage durable ; True s'il est utilisable par « batch »."""
    kind = storage.configured_kind()
    if kind is None:
        report(False, f"stockage durable : non configuré ({', '.join(config.S3_REQUIRED)})")
        return False
    for name in (*config.S3_REQUIRED, config.ENV_S3_ENDPOINT) if kind == "s3" else ():
        report(bool(os.environ.get(name)), f"variable d'environnement {name} définie")
    if kind == "s3" and offline:
        report(None, "stockage durable non testé (--offline)")
        return False
    try:
        store = storage.from_env()
        result = storage.self_test(store)
        sources = len(batch.list_sources(store))
        records = len(ledger.Ledger(store).records())
    except storage.StorageError as error:
        report(False, f"stockage durable : {error}")
        return False
    except Exception as error:  # noqa: BLE001 - diagnostic, on rapporte tout
        report(False, f"stockage durable : {type(error).__name__}: {logs.redact(str(error))[:300]}")
        return False
    report(result["read_write"], f"stockage durable, lecture et écriture : {store.describe()}")
    report(
        result["exclusive"],
        "stockage durable, création exclusive (anti-doublon entre exécutions simultanées)"
        + ("" if result["exclusive"] else " : NON respectée par ce service"),
    )
    previous = result["previous"]
    report(
        None,
        f"marqueur laissé par l'exécution précédente : {previous['at']} sur {previous['host']}"
        if previous else "aucun marqueur d'une exécution précédente (première vérification de ce stockage)",
    )
    report(None, f"contenu du stockage : {sources} vidéo(s) source, {records} tentative(s) dans l'historique")
    return result["read_write"] and result["exclusive"]


def cmd_check(args: argparse.Namespace) -> int:
    lines: list[tuple[str, str]] = []

    def report(ok: bool | None, text: str) -> bool | None:
        lines.append(({True: "OK    ", False: "MANQUE", None: "INFO  "}[ok], text))
        return ok

    report(sys.version_info >= (3, 10), f"Python {sys.version.split()[0]} (3.10 minimum)")

    ffmpeg_ok = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
    report(ffmpeg_ok, "ffmpeg et ffprobe dans le PATH")
    if ffmpeg_ok:
        filters = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
        for name in ("scale", "overlay", "gblur", "pad"):
            ffmpeg_ok &= bool(report(re.search(rf"\s{name}\s", filters) is not None, f"filtre FFmpeg « {name} »"))
        subtitles_ok = report(re.search(r"\ssubtitles\s", filters) is not None, "filtre « subtitles » (libass, pour les SRT)")
        ffmpeg_ok &= bool(report("libx264" in encoders, "encodeur libx264"))
        ffmpeg_ok &= bool(report(re.search(r"\saac\s", encoders) is not None, "encodeur aac"))
    else:
        subtitles_ok = False

    storage_ok = True
    for label, folder in (("données (historique, journaux)", config.data_dir()), ("identifiants", config.config_dir())):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            report(True, f"dossier {label} accessible en écriture : {folder}")
        except OSError as error:
            storage_ok = False
            report(False, f"dossier {label} inutilisable : {folder} ({error.strerror})")
    in_repo = config.inside_git_repo(config.config_dir())
    report(not in_repo, "dossier des identifiants situé hors de tout dépôt git")

    libs_ok = True
    for name in GOOGLE_LIBS:
        libs_ok &= bool(report(_has_module(name), f"bibliothèque Python « {name} »"))
    report(_has_module("boto3"), "bibliothèque Python « boto3 » (stockage durable)")

    network_ok = True
    if args.offline:
        report(None, "réseau non testé (--offline)")
    else:
        for url in ("https://www.googleapis.com/discovery/v1/apis/youtube/v3/rest", "https://oauth2.googleapis.com/token"):
            network_ok &= bool(report(_reachable(url), f"accès réseau à {url.split('/')[2]}"))

    for name in (config.ENV_CLIENT_ID, config.ENV_CLIENT_SECRET, config.ENV_REFRESH_TOKEN):
        report(None, f"variable d'environnement {name} ({'présente' if os.environ.get(name) else 'absente'})")
    source = youtube.credential_source()
    report(source is not None, f"identifiants YouTube : {source or 'aucun (lancer « ytclips auth »)'}")
    connected = False
    if source and libs_ok and network_ok and not args.offline:
        try:
            creds = youtube.load_credentials()
            channel = youtube.channel_info(creds)
            connected = channel is not None
            report(connected, f"connexion à la chaîne : {channel['title']} ({channel['id']})" if channel else "aucune chaîne YouTube sur ce compte")
        except Exception as error:  # noqa: BLE001 - diagnostic, on rapporte tout
            report(False, f"connexion à la chaîne : {logs.redact(str(error))[:600]}")
        if connected:
            try:
                manage = youtube.can_manage(youtube.granted_scopes(creds))
                report(None, "permission de rendre un clip public : " + (
                    "accordée" if manage else "absente (« ytclips auth --public » requis avant toute publication publique)"
                ))
            except youtube.YouTubeError as error:
                report(None, f"permission de rendre un clip public : non vérifiée ({error})")
    report(None, "mode public : " + (
        f"variable {config.ENV_PUBLIC_MODE}=enabled (l'option --public reste nécessaire)"
        if curation.public_mode(True) else "désactivé"
    ))

    durable_ok = _check_storage(report, args.offline)

    for status, text in lines:
        print(f"[{status}] {text}")
    clip_ready = ffmpeg_ok and storage_ok
    upload_ready = clip_ready and connected and not in_repo
    print()
    print(f"Création de clips       : {'prête' if clip_ready else 'NON prête'}")
    print(f"Sous-titres SRT         : {'prêts' if clip_ready and subtitles_ok else 'NON prêts'}")
    print(f"Simulation (--dry-run)  : {'prête' if clip_ready else 'NON prête'}")
    print(f"Envoi privé sur YouTube : {'prêt' if upload_ready else 'NON prêt'}")
    print(f"Traitement par lot      : {'prêt' if upload_ready and durable_ok else 'NON prêt'}")
    log.debug("check : clips=%s connexion=%s stockage=%s", clip_ready, connected, durable_ok)
    return EXIT_OK if clip_ready else EXIT_ERROR


# --- auth ------------------------------------------------------------------

def cmd_auth(args: argparse.Namespace) -> int:
    target = youtube.run_auth(no_browser=args.no_browser, port=args.port, public=args.public)
    log.info("Connexion enregistrée dans %s (lecture réservée à votre utilisateur).", target)
    try:
        channel = youtube.channel_info(youtube.load_credentials())
    except Exception as error:  # noqa: BLE001
        log.warning("Jeton enregistré, mais la chaîne n'a pas pu être lue : %s", error)
        return EXIT_OK
    if channel:
        log.info("Chaîne connectée : %s (%s)", channel["title"], channel["id"])
    else:
        log.warning("Ce compte Google n'a pas de chaîne YouTube.")
    return EXIT_OK


# --- clip ------------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:60] or "clip"


def cmd_clip(args: argparse.Namespace) -> int:
    if "://" in args.source:
        raise media.MediaError("Seuls les fichiers locaux que vous fournissez sont acceptés (pas d'URL).")
    source = Path(args.source).expanduser()
    if not source.is_file():
        raise media.MediaError(f"Fichier source introuvable : {source}")
    if not args.rights_confirmed:
        raise media.MediaError(
            "Ajoutez --rights-confirmed pour confirmer que vous êtes autorisé à republier cette vidéo."
        )
    srt = Path(args.srt).expanduser() if args.srt else None
    if srt and not srt.is_file():
        raise media.MediaError(f"Fichier SRT introuvable : {srt}")

    start_ms, end_ms = media.parse_timecode(args.start), media.parse_timecode(args.end)
    media.validate_range(start_ms, end_ms)
    out_dir = Path(args.output_dir).expanduser()
    output = out_dir / f"{_slug(source.stem)}_{start_ms}-{end_ms}.mp4"
    meta_file = clipmeta.meta_path_for(output)
    if (output.exists() or meta_file.exists()) and not args.overwrite:
        raise media.MediaError(f"{output} existe déjà (ajouter --overwrite pour le remplacer).")

    title = args.title or source.stem.replace("_", " ").strip()[:100]
    tags = [tag.strip() for tag in (args.tags or "").split(",") if tag.strip()]
    draft = clipmeta.new_meta(
        clip_file=output.name, clip_sha256="0", source_name=source.name, source_sha256="0",
        start_ms=start_ms, end_ms=end_ms, background=args.background, subtitles=False,
        title=title, description=args.description or "", tags=tags,
    )
    clipmeta.validate(draft)  # échoue avant l'encodage si le titre est invalide

    log.info(
        "Création du clip : %s, de %s à %s (%.1f s), fond « %s ».",
        source.name, media.format_timecode(start_ms), media.format_timecode(end_ms),
        (end_ms - start_ms) / 1000, args.background,
    )
    produced = media.render_clip(source, output, start_ms, end_ms, args.background, srt, args.srt_timing)
    draft["clip"].update(
        sha256=media.sha256_file(output), source_sha256=media.sha256_file(source),
        subtitles=produced["subtitles"],
    )
    clipmeta.save(meta_file, draft)
    log.info(
        "Clip prêt : %s (%dx%d, %.2f s, %.1f Mo).", output, produced["width"], produced["height"],
        produced["duration_ms"] / 1000, output.stat().st_size / 1e6,
    )
    log.info("Titre et description modifiables dans : %s", meta_file)
    log.info("Étape suivante : ytclips upload %s --dry-run", meta_file)
    return EXIT_OK


# --- upload ----------------------------------------------------------------

def cmd_upload(args: argparse.Namespace) -> int:
    meta_file = clipmeta.meta_path_for(Path(args.clip).expanduser())
    meta = clipmeta.load(meta_file)
    clip_info = meta["clip"]
    clip_file = meta_file.parent / clip_info["file"]
    if not clip_file.is_file():
        raise clipmeta.MetaError(f"Fichier vidéo introuvable : {clip_file}")
    if media.sha256_file(clip_file) != clip_info["sha256"]:
        raise clipmeta.MetaError(
            "Le fichier vidéo ne correspond plus à sa fiche (modifié après création) : recréer le clip."
        )
    produced = media.probe(clip_file)
    if (produced["width"], produced["height"]) != (config.WIDTH, config.HEIGHT):
        raise clipmeta.MetaError("Le clip n'est pas en 1080x1920.")
    media.validate_range(0, produced["duration_ms"])

    body = clipmeta.request_body(meta)
    store = history.History(config.history_path())
    key = history.dedupe_key(clip_info["source_sha256"], clip_info["start_ms"], clip_info["end_ms"])
    existing = store.find_blocking(key, clip_info["sha256"])

    if args.dry_run:
        log.info("SIMULATION : aucun appel à YouTube, rien n'est inscrit dans l'historique.")
        log.info("Fichier : %s (%.2f s, %.1f Mo)", clip_file, produced["duration_ms"] / 1000, clip_file.stat().st_size / 1e6)
        log.info("Requête videos.insert qui serait envoyée :\n%s", json.dumps(body, ensure_ascii=False, indent=2))
        log.info("Identifiants YouTube : %s", youtube.credential_source() or "absents (requis pour un envoi réel)")
        if existing is not None:
            log.warning(
                "Un envoi réel serait REFUSÉ : extrait déjà dans l'historique (n° %s, état %s, vidéo %s).",
                existing["id"], existing["status"], existing["video_id"] or "inconnue",
            )
            return EXIT_DUPLICATE
        log.info("Historique : aucun doublon, un envoi réel serait accepté.")
        return EXIT_OK

    if existing is not None and not args.force:
        log.error(
            "Envoi refusé, doublon : extrait déjà dans l'historique (n° %s, état %s, vidéo %s). "
            "Voir « ytclips history ».", existing["id"], existing["status"], existing["video_id"] or "inconnue",
        )
        return EXIT_DUPLICATE

    creds = youtube.load_credentials()
    try:
        row_id = store.claim(
            key, clip_info["sha256"], clip_info["source_name"], clip_info["start_ms"],
            clip_info["end_ms"], body["snippet"]["title"], force=args.force,
        )
    except history.DuplicateError as error:
        log.error("Envoi refusé, doublon : %s.", error)
        return EXIT_DUPLICATE

    log.info("Envoi en mode PRIVÉ de « %s » (historique n° %d)…", body["snippet"]["title"], row_id)
    try:
        response = youtube.upload_private(creds, clip_file, body)
    except Exception as error:  # noqa: BLE001 - on classe puis on relance
        status = getattr(getattr(error, "resp", None), "status", None)
        message = logs.redact(f"{type(error).__name__}: {error}")
        if youtube.is_auth_error(error):
            store.mark_failed(row_id, message)
            raise youtube.auth_error(error) from None
        if status is not None and 400 <= int(status) < 500:
            store.mark_failed(row_id, message)
            log.error("YouTube a refusé l'envoi (HTTP %s) : aucune vidéo créée, un nouvel essai est possible.", status)
        else:
            store.note_error(row_id, message)
            log.error(
                "Envoi interrompu, issue inconnue. L'entrée n° %d reste « pending » et bloque tout nouvel envoi : "
                "vérifiez YouTube Studio, puis « ytclips history --release %d » si aucune vidéo n'a été créée.",
                row_id, row_id,
            )
        raise

    video_id = response.get("id", "")
    privacy = response.get("status", {}).get("privacyStatus", "inconnue")
    store.mark_uploaded(row_id, video_id, privacy)
    log.info("Vidéo envoyée : https://youtu.be/%s", video_id)
    log.info("Gestion : https://studio.youtube.com/video/%s/edit", video_id)
    if privacy != config.PRIVACY:
        log.error("ALERTE : YouTube indique la visibilité « %s » au lieu de « private ». Vérifiez dans YouTube Studio.", privacy)
        return EXIT_ERROR
    log.info("Visibilité confirmée par YouTube : private.")
    return EXIT_OK


# --- batch -----------------------------------------------------------------

def _interrupt(*_):
    raise KeyboardInterrupt


def cmd_batch(args: argparse.Namespace) -> int:
    if not args.rights_confirmed:
        raise media.MediaError(
            "Ajoutez --rights-confirmed pour confirmer que vous êtes autorisé à republier "
            "toutes les vidéos déposées dans « sources/ »."
        )
    if args.limit < 1 or args.max_attempts < 1:
        raise media.MediaError("--limit et --max-attempts doivent valoir au moins 1.")
    store = storage.require()
    # Un arrêt demandé par la plateforme est consigné comme une interruption ordinaire.
    signal.signal(signal.SIGTERM, _interrupt)
    _, ok = batch.run(
        store, limit=args.limit, dry_run=args.dry_run,
        stale_seconds=args.stale_hours * 3600, max_attempts=args.max_attempts,
    )
    return EXIT_OK if ok else EXIT_ERROR


# --- history ---------------------------------------------------------------

def _remote_history(args: argparse.Namespace) -> int:
    book = ledger.Ledger(storage.require())
    if args.release is not None:
        released = book.release(args.release)
        if released:
            log.info("Extrait %s libéré : il pourra être renvoyé.", released["content_key"][:12])
            return EXIT_OK
        log.error("Aucun extrait « pending » unique ne commence par « %s ».", args.release)
        return EXIT_ERROR
    records = book.records()
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return EXIT_OK
    if not records:
        print(f"Historique partagé vide ({book.storage.describe()}).")
        return EXIT_OK
    for record in records:
        state = record["status"] if record["status"] != ledger.PENDING else f"pending/{record.get('phase')}"
        if record.get("start_ms", -1) >= 0:
            span = f"[{media.format_timecode(record['start_ms'])} → {media.format_timecode(record['end_ms'])}]"
        else:
            span = f"[montage de {len(record['segments'])} passages]" if record.get("segments") else "[bornes non déterminées]"
        print(
            f"{record['content_key'][:12]} n° {record['attempt']} | {record['updated_at']} | {state:17} | "
            f"{record.get('privacy') or '-':7} | {record.get('video_id') or '-':11} | "
            f"{record.get('source_key')} {span} | {record.get('title') or '-'}"
        )
        if record.get("error"):
            print(f"      erreur : {record['error']}")
    return EXIT_OK


def cmd_history(args: argparse.Namespace) -> int:
    if args.remote:
        return _remote_history(args)
    store = history.History(config.history_path())
    if args.release is not None:
        if args.release.isdigit() and store.release(int(args.release)):
            log.info("Entrée n° %s libérée : cet extrait peut être renvoyé.", args.release)
            return EXIT_OK
        log.error("Entrée n° %s introuvable ou pas à l'état « pending ».", args.release)
        return EXIT_ERROR
    rows = store.rows()
    if args.json:
        print(json.dumps([dict(row) for row in rows], ensure_ascii=False, indent=2))
        return EXIT_OK
    if not rows:
        print(f"Historique vide ({config.history_path()}).")
        return EXIT_OK
    for row in rows:
        print(
            f"n° {row['id']} | {row['updated_at']} | {row['status']:8} | {row['privacy'] or '-':7} | "
            f"{row['video_id'] or '-':11} | {row['source_name']} "
            f"[{media.format_timecode(row['start_ms'])} → {media.format_timecode(row['end_ms'])}] | {row['title']}"
        )
        if row["error"]:
            print(f"      erreur : {row['error']}")
    return EXIT_OK


# --- point d'entrée ---------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ytclips", description="Clips verticaux 1080x1920 avec FFmpeg, envoi PRIVÉ sur YouTube."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-v", "--verbose", action="store_true", help="afficher les détails (commande FFmpeg…)")
    commands = parser.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="vérifier l'environnement")
    check.add_argument("--offline", action="store_true", help="ne tester ni le réseau, ni YouTube, ni le stockage S3")
    check.set_defaults(handler=cmd_check)

    auth = commands.add_parser("auth", help="connecter la chaîne YouTube (OAuth)")
    auth.add_argument("--no-browser", action="store_true", help="machine sans navigateur : copier-coller manuel")
    auth.add_argument("--port", type=int, default=8765, help="port local de redirection (défaut 8765)")
    auth.add_argument("--public", action="store_true",
                      help="demander aussi la permission de modifier la visibilité des vidéos (publication publique)")
    auth.set_defaults(handler=cmd_auth)

    clip = commands.add_parser("clip", help="créer un clip vertical")
    clip.add_argument("source", help="fichier vidéo local")
    clip.add_argument("--start", required=True, help="début : secondes, MM:SS ou HH:MM:SS.mmm")
    clip.add_argument("--end", required=True, help="fin (durée de 20 à 60 s)")
    clip.add_argument("--rights-confirmed", action="store_true", help="je confirme être autorisé à republier cette vidéo")
    clip.add_argument("--srt", help="fichier de sous-titres SRT à incruster")
    clip.add_argument("--srt-timing", choices=("source", "clip"), default="source",
                      help="les temps du SRT se rapportent à la vidéo source (défaut) ou déjà au clip")
    clip.add_argument("--background", default="blur", help="blur (défaut), black ou couleur #RRGGBB")
    clip.add_argument("--title", help="titre YouTube (modifiable ensuite dans la fiche JSON)")
    clip.add_argument("--description", help="description YouTube")
    clip.add_argument("--tags", help="tags séparés par des virgules")
    clip.add_argument("--output-dir", default=str(config.PROJECT_ROOT / "output"))
    clip.add_argument("--overwrite", action="store_true")
    clip.set_defaults(handler=cmd_clip)

    upload = commands.add_parser("upload", help="envoyer un clip en mode privé")
    upload.add_argument("clip", help="fiche .json du clip (ou son .mp4)")
    upload.add_argument("--dry-run", action="store_true", help="simulation : aucun envoi sur YouTube")
    upload.add_argument("--force", action="store_true", help="envoyer malgré un doublon dans l'historique")
    upload.set_defaults(handler=cmd_upload)

    lot = commands.add_parser("batch", help="traiter les sources non traitées du stockage durable")
    lot.add_argument("--rights-confirmed", action="store_true",
                     help="je confirme être autorisé à republier toutes les vidéos de « sources/ »")
    lot.add_argument("--dry-run", action="store_true", help="simulation : rien n'est écrit ni envoyé")
    lot.add_argument("--limit", type=int, default=1, help="nombre maximal de sources par exécution (défaut 1)")
    lot.add_argument("--max-attempts", type=int, default=3, help="essais par source avant abandon (défaut 3)")
    lot.add_argument("--stale-hours", type=float, default=6,
                     help="délai après lequel une réservation interrompue avant l'envoi est reprise (défaut 6)")
    lot.set_defaults(handler=cmd_batch)

    hist = commands.add_parser("history", help="consulter l'historique des envois")
    hist.add_argument("--remote", action="store_true", help="historique partagé du stockage durable (commande batch)")
    hist.add_argument("--json", action="store_true")
    hist.add_argument("--release", metavar="ID",
                      help="libérer une entrée « pending » après vérification (numéro local, ou début de clé avec --remote)")
    hist.set_defaults(handler=cmd_history)
    curate_cli.register(commands)
    tiktok_cli.register(commands)
    montage_cli.register(commands)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logs.setup(config.log_path(), args.verbose)
    for name in (config.ENV_CLIENT_SECRET, config.ENV_REFRESH_TOKEN, config.ENV_S3_SECRET_KEY):
        logs.register_secret(os.environ.get(name))
    log.debug("Commande : %s", args.command)
    try:
        return args.handler(args)
    except youtube.AuthError as error:
        log.error("%s", error)
        return EXIT_AUTH
    except (media.MediaError, clipmeta.MetaError, youtube.YouTubeError, storage.StorageError) as error:
        log.error("%s", error)
        return EXIT_ERROR
    except KeyboardInterrupt:
        log.error("Interrompu.")
        return 130
    except Exception as error:  # noqa: BLE001
        log.error("Erreur inattendue : %s: %s (détails dans %s)", type(error).__name__, error, config.log_path())
        log.debug("Trace", exc_info=True)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
