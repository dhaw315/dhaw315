"""Journalisation console + fichier, avec masquage des secrets."""

from __future__ import annotations

import logging
import re
from pathlib import Path

_PATTERNS = [
    re.compile(r"ya29\.[A-Za-z0-9_\-.]+"),
    re.compile(r"1//[A-Za-z0-9_\-]{10,}"),
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"),
    re.compile(
        r"(?i)((?:access_token|refresh_token|client_secret|id_token|code)[\"']?\s*[=:]\s*[\"']?)[^\s\"'&,}]+"
    ),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9_\-.]+"),
]
_known_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Déclare une valeur à masquer partout dans les journaux."""
    if value and len(value) >= 6:
        _known_secrets.add(value)


def redact(text: str) -> str:
    for secret in _known_secrets:
        text = text.replace(secret, "***")
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup(log_file: Path, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("ytclips")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(RedactingFormatter("%(levelname)s: %(message)s"))
    logger.addHandler(console)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
