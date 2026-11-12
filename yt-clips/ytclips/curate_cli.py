"""Commandes de sélection, d'évaluation et de publication, ajoutées à la CLI principale."""

from __future__ import annotations

import argparse
import json
import logging
import signal

from . import config, curation, importer, media, proposals, storage
from .proposals import Book

log = logging.getLogger("ytclips")
OK, ERROR = 0, 1


def _interrupt(*_):
    raise KeyboardInterrupt


def _need_rights(args: argparse.Namespace) -> None:
    if not args.rights_confirmed:
        raise media.MediaError(
            "Ajoutez --rights-confirmed pour confirmer que vous êtes autorisé à republier "
            "les vidéos déposées dans « sources/ »."
        )


def cmd_propose(args: argparse.Namespace) -> int:
    if args.limit < 1:
        raise media.MediaError("--limit doit valoir au moins 1.")
    curation.propose(
        storage.require(), limit=args.limit, dry_run=args.dry_run, force=args.force, only=args.source,
        skip_start_ms=round(args.skip_start * 1000), skip_end_ms=round(args.skip_end * 1000),
    )
    return OK


def cmd_proposals(args: argparse.Namespace) -> int:
    records = Book(storage.require()).all()
    if args.status:
        records = [record for record in records if record["status"] == args.status]
    if args.json:
        print(json.dumps(records, ensure_ascii=False, indent=2))
        return OK
    if not records:
        print("Aucune proposition.")
        return OK
    for record in records:
        video = f"https://youtu.be/{record['video_id']} ({record.get('privacy')})" if record.get("video_id") else "pas encore envoyé"
        print(
            f"{record['content_key'][:12]} | {proposals.LABELS[record['status']]:8} | {record['source_key']} "
            f"[{record['start']} → {record['end']}] | score {record['score']:.2f} | {video}\n"
            f"      titre  : {record['title']}\n      raison : {record['reason']}"
        )
    return OK


def cmd_review(args: argparse.Namespace) -> int:
    _need_rights(args)
    signal.signal(signal.SIGTERM, _interrupt)
    ok = curation.review(
        storage.require(), limit=args.limit, dry_run=args.dry_run,
        stale_seconds=args.stale_hours * 3600, max_attempts=args.max_attempts,
    )
    return OK if ok else ERROR


def cmd_approve(args: argparse.Namespace) -> int:
    item = curation.decide(storage.require(), args.id, True, title=args.title, description=args.description)
    log.info("Validé : %s « %s » — ajouté à la file de publication.", item["content_key"][:12], item["title"])
    return OK


def cmd_reject(args: argparse.Namespace) -> int:
    item = curation.decide(storage.require(), args.id, False, reason=args.reason)
    log.info("Refusé : %s « %s » — ce passage ne sera plus proposé.", item["content_key"][:12], item["title"])
    if item.get("video_id"):
        log.info("Sa copie privée reste sur la chaîne : https://studio.youtube.com/video/%s/edit", item["video_id"])
    return OK


def cmd_publish(args: argparse.Namespace) -> int:
    _need_rights(args)
    signal.signal(signal.SIGTERM, _interrupt)
    ok = curation.publish(
        storage.require(), public=args.public, dry_run=args.dry_run,
        stale_seconds=args.stale_hours * 3600, max_attempts=args.max_attempts,
    )
    return OK if ok else ERROR


def cmd_import(args: argparse.Namespace) -> int:
    importer.import_url(storage.require(), args.url, rights_confirmed=args.rights_confirmed, name=args.name)
    return OK


def _retry_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rights-confirmed", action="store_true",
                        help="je confirme être autorisé à republier les vidéos de « sources/ »")
    parser.add_argument("--dry-run", action="store_true", help="simulation : rien n'est écrit ni envoyé")
    parser.add_argument("--max-attempts", type=int, default=3, help="essais par extrait avant abandon (défaut 3)")
    parser.add_argument("--stale-hours", type=float, default=6,
                        help="délai après lequel une réservation interrompue avant l'envoi est reprise (défaut 6)")


def register(commands) -> None:
    propose = commands.add_parser("propose", help="analyser les sources et proposer jusqu'à 3 extraits par vidéo")
    propose.add_argument("--dry-run", action="store_true", help="simulation : rien n'est écrit")
    propose.add_argument("--limit", type=int, default=1, help="nombre maximal de sources analysées (défaut 1)")
    propose.add_argument("--source", help="n'analyser que cette clé, par exemple sources/ma_video.mp4")
    propose.add_argument("--force", action="store_true", help="chercher d'autres passages dans une source déjà analysée")
    propose.add_argument("--skip-start", type=float, default=0, metavar="S", help="secondes à ignorer au début (générique)")
    propose.add_argument("--skip-end", type=float, default=0, metavar="S", help="secondes à ignorer à la fin (générique)")
    propose.set_defaults(handler=cmd_propose)

    listing = commands.add_parser("proposals", help="lister les propositions et leur état")
    listing.add_argument("--status", choices=sorted(proposals.LABELS))
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=cmd_proposals)

    review = commands.add_parser("review", help="créer les clips proposés et les envoyer en PRIVÉ pour évaluation")
    _retry_options(review)
    review.add_argument("--limit", type=int, default=3, help="nombre maximal d'envois par exécution (défaut 3)")
    review.set_defaults(handler=cmd_review)

    approve = commands.add_parser("approve", help="valider une proposition (file de publication)")
    approve.add_argument("id", help="début de l'identifiant affiché par « proposals »")
    approve.add_argument("--title", help="remplacer le titre proposé")
    approve.add_argument("--description", help="remplacer la description proposée")
    approve.set_defaults(handler=cmd_approve)

    reject = commands.add_parser("reject", help="refuser une proposition")
    reject.add_argument("id", help="début de l'identifiant affiché par « proposals »")
    reject.add_argument("--reason", help="note libre")
    reject.set_defaults(handler=cmd_reject)

    publish = commands.add_parser("publish", help="traiter la file : un clip validé, un clip public par jour au plus")
    _retry_options(publish)
    publish.add_argument(
        "--public", action="store_true",
        help=f"rendre le clip PUBLIC ; exige aussi la variable {config.ENV_PUBLIC_MODE}=enabled",
    )
    publish.set_defaults(handler=cmd_publish)

    bring = commands.add_parser("import", help="importer une vidéo depuis une source autorisée vers « sources/ »")
    bring.add_argument("url", help="adresse https directe d'un fichier vidéo, sur un hôte de la liste autorisée")
    bring.add_argument("--name", help="nom du fichier dans « sources/ »")
    bring.add_argument("--rights-confirmed", action="store_true", help="je confirme être autorisé à republier cette vidéo")
    bring.set_defaults(handler=cmd_import)
