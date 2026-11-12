"""Stockage durable partagé entre exécutions : bucket compatible S3 ou dossier local.

Les deux offrent une écriture « seulement si absent », atomique, sur laquelle
repose l'anti-doublon entre exécutions simultanées.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .logs import register_secret


class StorageError(Exception):
    pass


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str


class LocalStorage:
    """Dossier local (tests, ou disque réellement partagé)."""

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        return f"dossier {self.root}"

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise StorageError(f"Clé invalide : {key}")
        return path

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        items = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file() or path.name.startswith(".tmp-"):
                continue
            key = path.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                stat = path.stat()
                items.append(ObjectInfo(key, stat.st_size, f"{stat.st_size}-{stat.st_mtime_ns}"))
        return items

    def read(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def write(self, key: str, data: bytes, if_absent: bool = False) -> bool:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".tmp-{uuid.uuid4().hex}")
        temporary.write_bytes(data)
        try:
            if if_absent:
                try:
                    # Un lien physique échoue si la cible existe : création atomique, contenu complet.
                    os.link(temporary, target)
                except FileExistsError:
                    return False
                return True
            os.replace(temporary, target)
            return True
        finally:
            temporary.unlink(missing_ok=True)

    def download(self, key: str, destination: Path) -> None:
        try:
            shutil.copyfile(self._path(key), destination)
        except FileNotFoundError:
            raise StorageError(f"Objet introuvable : {key}") from None

    def upload(self, source: Path, key: str) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".tmp-{uuid.uuid4().hex}")
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def presign(self, key: str, seconds: int) -> str:
        """Dossier local : le chemin du fichier tient lieu de lien."""
        return str(self._path(key))


class S3Storage:
    """Bucket compatible S3 (Cloudflare R2, AWS S3, MinIO…)."""

    def __init__(
        self, bucket: str, access_key: str, secret_key: str,
        endpoint_url: str | None = None, region: str = "auto", prefix: str = "",
    ):
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise StorageError("Bibliothèque boto3 absente. Installer avec : pip install -e .") from None
        register_secret(secret_key)
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.client = boto3.client(
            "s3", endpoint_url=endpoint_url or None, region_name=region,
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            config=Config(
                signature_version="s3v4", connect_timeout=15, read_timeout=120,
                retries={"max_attempts": 4, "mode": "standard"},
                # Les sommes de contrôle ajoutées par défaut depuis boto3 1.36 sont refusées
                # par plusieurs services compatibles S3.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def describe(self) -> str:
        host = (self.endpoint_url or "AWS S3").split("//")[-1].split("/")[0]
        return f"bucket « {self.bucket} » ({host})" + (f", préfixe {self.prefix}" if self.prefix else "")

    def _call(self, action: str, function, **kwargs):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return function(**kwargs)
        except ClientError:
            raise
        except BotoCoreError as error:
            raise StorageError(f"Stockage injoignable ({action}) : {type(error).__name__}") from None

    @staticmethod
    def _code(error) -> tuple[str, int]:
        response = getattr(error, "response", {}) or {}
        return (
            str(response.get("Error", {}).get("Code", "")),
            int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0),
        )

    def _fail(self, action: str, error) -> StorageError:
        code, status = self._code(error)
        return StorageError(f"Le stockage a refusé l'opération « {action} » : {code or 'erreur'} (HTTP {status}).")

    def list(self, prefix: str = "") -> list[ObjectInfo]:
        from botocore.exceptions import BotoCoreError, ClientError

        items = []
        try:
            pages = self.client.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=self.prefix + prefix
            )
            for page in pages:
                for entry in page.get("Contents", []):
                    items.append(
                        ObjectInfo(entry["Key"][len(self.prefix):], entry["Size"], entry["ETag"].strip('"'))
                    )
        except ClientError as error:
            raise self._fail("lister", error) from None
        except BotoCoreError as error:
            raise StorageError(f"Stockage injoignable (lister) : {type(error).__name__}") from None
        return sorted(items, key=lambda item: item.key)

    def read(self, key: str) -> bytes | None:
        from botocore.exceptions import ClientError

        try:
            response = self._call("lire", self.client.get_object, Bucket=self.bucket, Key=self.prefix + key)
        except ClientError as error:
            code, status = self._code(error)
            if code in ("NoSuchKey", "404") or status == 404:
                return None
            raise self._fail("lire", error) from None
        return response["Body"].read()

    def write(self, key: str, data: bytes, if_absent: bool = False) -> bool:
        from botocore.exceptions import ClientError

        arguments = {"Bucket": self.bucket, "Key": self.prefix + key, "Body": data}
        if if_absent:
            arguments["IfNoneMatch"] = "*"
        try:
            self._call("écrire", self.client.put_object, **arguments)
        except ClientError as error:
            code, status = self._code(error)
            if if_absent and (status == 412 or code == "PreconditionFailed"):
                return False
            if if_absent and status == 409:
                # Écritures concurrentes sur la même clé : perdu si l'objet existe désormais.
                if self.read(key) is not None:
                    return False
            raise self._fail("écrire", error) from None
        return True

    def download(self, key: str, destination: Path) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.client.download_file(self.bucket, self.prefix + key, str(destination))
        except ClientError as error:
            raise self._fail("télécharger", error) from None
        except BotoCoreError as error:
            raise StorageError(f"Stockage injoignable (télécharger) : {type(error).__name__}") from None

    def upload(self, source: Path, key: str) -> None:
        from boto3.exceptions import S3UploadFailedError
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self.client.upload_file(str(source), self.bucket, self.prefix + key)
        except ClientError as error:
            raise self._fail("déposer", error) from None
        except (BotoCoreError, S3UploadFailedError) as error:
            raise StorageError(f"Dépôt impossible dans le stockage : {type(error).__name__}") from None

    def delete(self, key: str) -> None:
        from botocore.exceptions import ClientError

        try:
            self._call("supprimer", self.client.delete_object, Bucket=self.bucket, Key=self.prefix + key)
        except ClientError as error:
            raise self._fail("supprimer", error) from None

    def presign(self, key: str, seconds: int) -> str:
        """Lien de téléchargement temporaire vers un objet du bucket privé (signé localement)."""
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": self.prefix + key}, ExpiresIn=seconds
        )


def configured_kind() -> str | None:
    """« local », « s3 » ou None, sans lire aucune valeur secrète."""
    if config.env(config.ENV_STORAGE_DIR):
        return "local"
    if any(config.env(name) for name in config.S3_REQUIRED):
        return "s3"
    return None


def from_env():
    """Construit le stockage décrit par l'environnement, ou None s'il n'est pas configuré."""
    local = config.env(config.ENV_STORAGE_DIR)
    if local:
        return LocalStorage(Path(local))
    missing = [name for name in config.S3_REQUIRED if not config.env(name)]
    if len(missing) == len(config.S3_REQUIRED):
        return None
    if missing:
        raise StorageError("Stockage S3 incomplet, variable(s) manquante(s) : " + ", ".join(missing))
    return S3Storage(
        bucket=config.env(config.ENV_S3_BUCKET),
        access_key=config.env(config.ENV_S3_ACCESS_KEY),
        secret_key=config.env(config.ENV_S3_SECRET_KEY),
        endpoint_url=config.env(config.ENV_S3_ENDPOINT),
        region=config.env(config.ENV_S3_REGION) or "auto",
        prefix=config.env(config.ENV_S3_PREFIX) or "",
    )


def require():
    store = from_env()
    if store is None:
        raise StorageError(
            "Stockage durable non configuré : définir " + ", ".join(config.S3_REQUIRED)
            + f" (et {config.ENV_S3_ENDPOINT} hors AWS), ou {config.ENV_STORAGE_DIR} pour un dossier local."
        )
    return store


def self_test(store) -> dict:
    """Vérifie lecture, écriture et création exclusive ; relit le marqueur de l'exécution précédente."""
    probe = f"state/checks/probe-{uuid.uuid4().hex}.json"
    result = {"read_write": False, "exclusive": False, "previous": None}
    try:
        first = store.write(probe, b"1", if_absent=True)
        second = store.write(probe, b"2", if_absent=True)
        content = store.read(probe)
        result["read_write"] = first and content in (b"1", b"2")
        # Un service qui ignore la condition accepte la seconde écriture : anti-doublon non garanti.
        result["exclusive"] = first and not second and content == b"1"
    finally:
        store.delete(probe)
    marker = "state/checks/last.json"
    previous = store.read(marker)
    if previous:
        try:
            result["previous"] = json.loads(previous)
        except ValueError:
            pass
    current = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "pid": os.getpid(),
    }
    store.write(marker, json.dumps(current).encode())
    result["current"] = current
    return result
