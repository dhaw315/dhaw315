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
