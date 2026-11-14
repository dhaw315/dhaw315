"""Commandes « ytclips montage … » et « ytclips transcribe »."""

from __future__ import annotations

import argparse
import json
import logging
import signal

from . import media, montage, montages, storage

log = logging.getLogger("ytclips")
OK, ERROR = 0, 1


def _interrupt(*_):
    raise KeyboardInterrupt


def _ranges(text: str | None) -> list[tuple[int, int]] | None:
    """« 00:04:10-00:04:22.500, 00:17:03-00:17:15 » -> liste de (début, fin) en millisecondes."""
    if not text:
        return None
    ranges = []
    for part in text.split(","):
        start, separator, end = part.strip().partition("-")
        if not separator:
            raise media.MediaError(f"Passage invalide : « {part.strip()} » (attendu : début-fin).")
        ranges.append((media.parse_timecode(start), media.parse_timecode(end)))
    return ranges


def cmd_plan(args: argparse.Namespace) -> int:
    item = montages.make_plan(
        storage.require(), args.source, dry_run=args.dry_run, cutoff=args.cutoff,
        skip_start_ms=round(args.skip_start * 1000), skip_end_ms=round(args.skip_end * 1000),
        ranges=_ranges(args.segments), zoom=args.zoom, background=args.background, title=args.title,
    )
    return OK if item else ERROR


def cmd_list(args: argparse.Namespace) -> int:
    items = montages.records(storage.require())
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return OK
    if not items:
        print("Aucun montage (lancer « ytclips montage plan »).")
    for item in items:
        montages.describe(item)
    return OK


def cmd_send(args: argparse.Namespace) -> int:
    if not args.rights_confirmed:
        raise media.MediaError("Ajoutez --rights-confirmed pour confirmer que vous êtes autorisé à republier ce film.")
    signal.signal(signal.SIGTERM, _interrupt)
    ok = montages.send(
        storage.require(), args.id, dry_run=args.dry_run,
        stale_seconds=args.stale_hours * 3600, max_attempts=args.max_attempts,
    )
    return OK if ok else ERROR


def cmd_transcribe(args: argparse.Namespace) -> int:
    montages.transcribe(storage.require(), args.source, model=args.model, language=args.language, force=args.force)
    return OK


def register(commands) -> None:
    parser = commands.add_parser("montage", help="un montage de 60 s composé de 4 à 6 passages d'un film")
    actions = parser.add_subparsers(dest="montage_command", required=True)

    plan = actions.add_parser("plan", help="analyser le film et enregistrer un plan de montage")
    plan.add_argument("--source", help="clé de la vidéo, par exemple sources/mon_film.mkv")
    plan.add_argument("--dry-run", action="store_true", help="simulation : rien n'est écrit dans le stockage")
    plan.add_argument("--cutoff", type=float, default=montage.SPOILER_CUTOFF,
                      help="fraction du film après laquelle rien n'est pris, pour ne pas révéler la fin (défaut 0.75)")
    plan.add_argument("--skip-start", type=float, default=0, metavar="S", help="secondes à ignorer au début (générique)")
    plan.add_argument("--skip-end", type=float, default=0, metavar="S", help="secondes à ignorer à la fin (générique)")
    plan.add_argument("--segments", help="imposer les passages : « début-fin, début-fin, … » (4 à 6, total 60 s)")
    plan.add_argument("--zoom", type=float, default=1.0,
                      help="1.0 : image entière (défaut) ; jusqu'à 2.0 : recadrage centré, les bords latéraux sont perdus")
    plan.add_argument("--background", default="blur", help="blur (défaut), black ou couleur #RRGGBB")
    plan.add_argument("--title", help="titre du film, pour le titre et la description du montage")
    plan.set_defaults(handler=cmd_plan)

    listing = actions.add_parser("list", help="lister les plans de montage, leurs passages et leur état")
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=cmd_list)

    send = actions.add_parser("send", help="créer le montage, l'envoyer en PRIVÉ sur YouTube et préparer TikTok")
    send.add_argument("id", nargs="?", help="début de l'identifiant du plan (facultatif s'il n'y en a qu'un)")
    send.add_argument("--rights-confirmed", action="store_true", help="je confirme être autorisé à republier ce film")
    send.add_argument("--dry-run", action="store_true", help="créer le montage puis le jeter, sans rien envoyer")
    send.add_argument("--max-attempts", type=int, default=3)
    send.add_argument("--stale-hours", type=float, default=6)
    send.set_defaults(handler=cmd_send)

    transcribe = commands.add_parser("transcribe", help="enregistrer des sous-titres pour un film qui n'en a pas")
    transcribe.add_argument("--source", help="clé de la vidéo")
    transcribe.add_argument("--model", default="small", help="modèle Whisper : tiny, base, small (défaut), medium…")
    transcribe.add_argument("--language", help="langue parlée, par exemple fr (défaut : détection automatique)")
    transcribe.add_argument("--force", action="store_true", help="transcrire même si des sous-titres existent déjà")
    transcribe.set_defaults(handler=cmd_transcribe)
