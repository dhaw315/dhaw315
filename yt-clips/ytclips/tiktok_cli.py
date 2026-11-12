"""Commandes « ytclips tiktok … » : préparation pour une publication manuelle."""

from __future__ import annotations

import argparse
import json
import logging

from . import storage, tiktok

log = logging.getLogger("ytclips")
OK = 0


def cmd_prepare(args: argparse.Namespace) -> int:
    created = tiktok.prepare(storage.require(), dry_run=args.dry_run)
    if not created:
        log.info("Aucun nouveau clip à préparer pour TikTok.")
    return OK


def cmd_list(args: argparse.Namespace) -> int:
    items = tiktok.records(storage.require())
    if args.status:
        items = [item for item in items if item["status"] == args.status]
    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return OK
    if not items:
        print("Aucune fiche TikTok (lancer « ytclips tiktok prepare »).")
        return OK
    for item in items:
        print(
            f"{item['content_key'][:12]} | {tiktok.LABELS[item['status']]:9} | {item['clip_key']} "
            f"[{item['start']} → {item['end']}]" + (f" | {item['post_url']}" if item.get("post_url") else "")
        )
        print("      légende : " + item["caption"].replace("\n", "\n                "))
    return OK


def cmd_caption(args: argparse.Namespace) -> int:
    item = tiktok.set_caption(storage.require(), args.id, args.text)
    log.info("Légende TikTok enregistrée pour %s.", item["content_key"][:12])
    return OK


def cmd_link(args: argparse.Namespace) -> int:
    item, url = tiktok.link(storage.require(), args.id, args.hours)
    # Affichage direct, hors journal : le lien donne accès au fichier pendant sa durée de validité.
    print(f"Clip    : {item['clip_key']}")
    print(f"Lien    : {url}")
    print(f"          (téléchargement valable {args.hours:g} h ; à ouvrir sur le téléphone, ne pas le partager)")
    print("Légende :")
    print(item["caption"])
    print(f"\nAprès publication dans l'application TikTok : ytclips tiktok done {item['content_key'][:12]}")
    return OK


def cmd_done(args: argparse.Namespace) -> int:
    item = tiktok.mark(storage.require(), args.id, tiktok.POSTED, url=args.url)
    log.info("Noté comme publié sur TikTok : %s.", item["content_key"][:12])
    return OK


def cmd_skip(args: argparse.Namespace) -> int:
    item = tiktok.mark(storage.require(), args.id, tiktok.SKIPPED, note=args.reason)
    log.info("Écarté pour TikTok : %s (YouTube n'est pas concerné).", item["content_key"][:12])
    return OK


def cmd_reopen(args: argparse.Namespace) -> int:
    item = tiktok.mark(storage.require(), args.id, tiktok.READY)
    log.info("De nouveau « à publier » sur TikTok : %s.", item["content_key"][:12])
    return OK


def register(commands) -> None:
    parser = commands.add_parser("tiktok", help="préparer les clips et leur légende pour une publication manuelle sur TikTok")
    actions = parser.add_subparsers(dest="tiktok_command", required=True)

    prepare = actions.add_parser("prepare", help="créer une fiche TikTok pour chaque clip du stockage")
    prepare.add_argument("--dry-run", action="store_true", help="simulation : rien n'est écrit")
    prepare.set_defaults(handler=cmd_prepare)

    listing = actions.add_parser("list", help="lister les fiches TikTok et leur état")
    listing.add_argument("--status", choices=sorted(tiktok.LABELS))
    listing.add_argument("--json", action="store_true")
    listing.set_defaults(handler=cmd_list)

    caption = actions.add_parser("caption", help="modifier la légende d'un clip")
    caption.add_argument("id", help="début de l'identifiant affiché par « tiktok list »")
    caption.add_argument("--text", required=True, help="nouvelle légende (hashtags compris)")
    caption.set_defaults(handler=cmd_caption)

    link = actions.add_parser("link", help="afficher la légende et un lien de téléchargement temporaire du clip")
    link.add_argument("id")
    link.add_argument("--hours", type=float, default=24, help="durée de validité du lien (défaut 24, maximum 168)")
    link.set_defaults(handler=cmd_link)

    done = actions.add_parser("done", help="noter qu'un clip a été publié à la main sur TikTok")
    done.add_argument("id")
    done.add_argument("--url", help="adresse de la publication TikTok")
    done.set_defaults(handler=cmd_done)

    skip = actions.add_parser("skip", help="écarter un clip pour TikTok")
    skip.add_argument("id")
    skip.add_argument("--reason", help="note libre")
    skip.set_defaults(handler=cmd_skip)

    reopen = actions.add_parser("reopen", help="remettre un clip « à publier »")
    reopen.add_argument("id")
    reopen.set_defaults(handler=cmd_reopen)
