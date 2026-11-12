"""Import d'une vidéo depuis une source autorisée vers « sources/ ».

Volontairement limité : téléchargement direct d'un fichier vidéo en HTTPS, depuis un
hôte que l'utilisateur a inscrit lui-même dans la liste autorisée. Aucune extraction
depuis une page web ou un lecteur, aucun contournement de protection ou de restriction
d'accès : un refus du serveur ou un fichier illisible arrête l'import.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

from . import batch, config, ledger, media

log = logging.getLogger("ytclips")

IMPORTS = "state/imports/"
MAX_BYTES = 8 * 1024**3
DIRECT_TYPES = {"application/octet-stream", "binary/octet-stream", "application/mp4"}


class ImportRefused(media.MediaError):
    pass


def allowed_hosts() -> set[str]:
    return {host.strip().lower() for host in config.env(config.ENV_IMPORT_HOSTS).split(",") if host.strip()}


def check_url(url: str, hosts: set[str], need_video_name: bool = True):
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ImportRefused("Import refusé : seules les adresses https:// sont acceptées.")
    if parsed.username or parsed.password:
        raise ImportRefused("Import refusé : pas d'identifiants dans l'adresse.")
    host = (parsed.hostname or "").lower()
    if host not in hosts:
        raise ImportRefused(
            f"Import refusé : « {host} » n'est pas dans la liste des sources autorisées "
            f"({config.ENV_IMPORT_HOSTS})."
        )
    if need_video_name and PurePosixPath(parsed.path).suffix.lower() not in batch.VIDEO_EXTENSIONS:
        raise ImportRefused(
            "Import refusé : l'adresse doit désigner directement un fichier vidéo "
            f"({', '.join(sorted(batch.VIDEO_EXTENSIONS))}), pas une page ni un lecteur."
        )
    return parsed


class _SameListRedirects(urllib.request.HTTPRedirectHandler):
    """Une redirection ne peut pas faire sortir de la liste autorisée."""

    def __init__(self, hosts: set[str]):
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl, self.hosts, need_video_name=False)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def import_url(storage, url: str, *, rights_confirmed: bool, name: str | None = None, opener=None) -> str:
    """Télécharge le fichier et le dépose dans « sources/ » ; renvoie la clé créée."""
    if not rights_confirmed:
        raise ImportRefused("Ajoutez --rights-confirmed pour confirmer que vous êtes autorisé à republier cette vidéo.")
    hosts = allowed_hosts()
    if not hosts:
        raise ImportRefused(
            f"Import désactivé : aucune source autorisée. Définir {config.ENV_IMPORT_HOSTS} "
            "avec les hôtes dont vous avez le droit de reprendre les vidéos (séparés par des virgules)."
        )
    parsed = check_url(url, hosts)
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", name or unquote(PurePosixPath(parsed.path).name)).strip("._")
    if PurePosixPath(filename).suffix.lower() not in batch.VIDEO_EXTENSIONS:
        raise ImportRefused("Import refusé : le nom de destination doit garder une extension vidéo.")
    key = f"{batch.SOURCES}{filename}"
    if any(item.key == key for item in storage.list(key)):
        raise ImportRefused(f"« {key} » existe déjà : une source originale n'est jamais remplacée (choisir --name).")

    opener = opener or urllib.request.build_opener(_SameListRedirects(hosts))
    request = urllib.request.Request(url, headers={"User-Agent": "ytclips-import"})
    digest = hashlib.sha256()
    with tempfile.TemporaryDirectory(prefix="ytclips-import-") as work:
        local = Path(work) / filename
        try:
            with opener.open(request, timeout=60) as response:
                content_type = response.headers.get_content_type()
                if not (content_type.startswith("video/") or content_type in DIRECT_TYPES):
                    raise ImportRefused(
                        f"Import refusé : la source renvoie « {content_type} » et non un fichier vidéo "
                        "(page web, lecteur protégé ou accès restreint). Aucun contournement n'est tenté."
                    )
                size = 0
                with local.open("wb") as handle:
                    while chunk := response.read(1 << 20):
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ImportRefused("Import refusé : fichier de plus de 8 Go.")
                        digest.update(chunk)
                        handle.write(chunk)
        except urllib.error.HTTPError as error:
            raise ImportRefused(
                f"Import refusé par la source (HTTP {error.code}) : accès non autorisé ou fichier absent. "
                "Aucun contournement n'est tenté."
            ) from None
        except urllib.error.URLError as error:
            raise ImportRefused(f"Source injoignable : {error.reason}") from None
        try:
            info = media.probe(local)
        except media.MediaError:
            raise ImportRefused(
                "Import refusé : le fichier reçu n'est pas une vidéo lisible (corrompu ou protégé). "
                "Aucun déchiffrement n'est tenté."
            ) from None
        storage.upload(local, key)
    storage.write(f"{IMPORTS}{filename}.json", json.dumps({
        "key": key, "host": parsed.hostname, "path": parsed.path, "sha256": digest.hexdigest(), "size": size,
        "duration_ms": info["duration_ms"], "rights_confirmed": True, "imported_at": ledger.now(),
    }, ensure_ascii=False, indent=2).encode())
    log.info("Importé : %s (%.1f Mo, %s).", key, size / 1e6, media.format_timecode(info["duration_ms"]))
    return key
