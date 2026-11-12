"""Emplacements et constantes. Aucun secret n'est défini ici."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

WIDTH, HEIGHT = 1080, 1920
MIN_CLIP_MS, MAX_CLIP_MS = 20_000, 60_000

# Seule visibilité autorisée par ce programme.
PRIVACY = "private"

ENV_CLIENT_ID = "YOUTUBE_CLIENT_ID"
ENV_CLIENT_SECRET = "YOUTUBE_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "YOUTUBE_REFRESH_TOKEN"

# Stockage durable partagé entre exécutions (bucket compatible S3).
ENV_S3_BUCKET = "YTCLIPS_S3_BUCKET"
ENV_S3_ACCESS_KEY = "YTCLIPS_S3_ACCESS_KEY_ID"
ENV_S3_SECRET_KEY = "YTCLIPS_S3_SECRET_ACCESS_KEY"
ENV_S3_ENDPOINT = "YTCLIPS_S3_ENDPOINT_URL"
ENV_S3_REGION = "YTCLIPS_S3_REGION"
ENV_S3_PREFIX = "YTCLIPS_S3_PREFIX"
S3_REQUIRED = (ENV_S3_BUCKET, ENV_S3_ACCESS_KEY, ENV_S3_SECRET_KEY)
# Variante sans service externe : un dossier (tests, disque partagé).
ENV_STORAGE_DIR = "YTCLIPS_STORAGE_DIR"

# Publication publique : désactivée tant que cette variable ne vaut pas « enabled ».
ENV_PUBLIC_MODE = "YTCLIPS_PUBLIC_MODE"
# Fuseau horaire du « un clip public par jour » (défaut : UTC).
ENV_TIMEZONE = "YTCLIPS_TIMEZONE"
# Hôtes depuis lesquels « ytclips import » a le droit de télécharger (vide : import désactivé).
ENV_IMPORT_HOSTS = "YTCLIPS_IMPORT_ALLOWED_HOSTS"

# Modifier la visibilité d'une vidéo exige une permission plus large que l'envoi.
SCOPE_MANAGE = "https://www.googleapis.com/auth/youtube.force-ssl"
MANAGE_SCOPES = {
    SCOPE_MANAGE,
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtubepartner",
}

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def env(name: str) -> str:
    """Valeur d'une variable, sans les blancs ou sauts de ligne collés par erreur autour."""
    return (os.environ.get(name) or "").strip()


def data_dir() -> Path:
    """Historique et journaux (ignorés par git)."""
    return Path(os.environ.get("YTCLIPS_DATA_DIR") or PROJECT_ROOT / "data").expanduser()


def config_dir() -> Path:
    """Identifiants OAuth : toujours hors du dépôt."""
    return Path(os.environ.get("YTCLIPS_CONFIG_DIR") or "~/.config/ytclips").expanduser()


def history_path() -> Path:
    return data_dir() / "history.sqlite3"


def log_path() -> Path:
    return data_dir() / "logs" / "ytclips.log"


def client_secret_path() -> Path:
    return config_dir() / "client_secret.json"


def token_path() -> Path:
    return config_dir() / "token.json"


def inside_git_repo(path: Path) -> bool:
    path = path.expanduser().resolve()
    return any((parent / ".git").exists() for parent in (path, *path.parents))
