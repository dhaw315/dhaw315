"""Historique partagé, conservé dans le stockage durable : une fiche JSON par tentative.

Une tentative n'est créée que par une écriture « seulement si absent ». Deux
exécutions simultanées ne peuvent donc pas réserver le même extrait, et une
tentative n'est reprise que si son auteur s'est arrêté avant de contacter YouTube.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

PENDING, UPLOADED, FAILED = "pending", "uploaded", "failed"
CLAIMED, RENDERED, UPLOADING, FINISHED = "claimed", "rendered", "uploading", "finished"
TAKE, DONE, BUSY, BLOCKED, EXHAUSTED = "take", "done", "busy", "blocked", "exhausted"

HISTORY = "state/history/"
INDEX = "state/index/"

# Échecs qui ne disent rien de la source : ils ne consomment pas d'essai.
UNCOUNTED = {"auth", "released", "superseded"}


def content_key(source_sha256: str, start_ms: int, end_ms: int) -> str:
    """Identité d'un extrait : contenu de la source et bornes, quel que soit le nom du fichier."""
    return hashlib.sha256(f"{source_sha256}:{start_ms}:{end_ms}".encode()).hexdigest()[:40]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def age_seconds(stamp: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()


@dataclass
class Decision:
    action: str
    reason: str = ""
    attempts: list[dict] = field(default_factory=list)


class Ledger:
    def __init__(self, storage):
        self.storage = storage

    # --- index : objet source -> extrait, pour éviter de retélécharger ---

    def lookup(self, fingerprint: str) -> dict | None:
        raw = self.storage.read(f"{INDEX}{fingerprint}.json")
        return json.loads(raw) if raw else None

    def remember(self, fingerprint: str, data: dict) -> None:
        self.storage.write(f"{INDEX}{fingerprint}.json", json.dumps(data, ensure_ascii=False).encode())

    # --- tentatives ---

    @staticmethod
    def _key(key: str, number: int) -> str:
        return f"{HISTORY}{key}/attempt-{number:04d}.json"

    def attempts(self, key: str) -> list[dict]:
        records = []
        for info in self.storage.list(f"{HISTORY}{key}/"):
            raw = self.storage.read(info.key)
            if raw:
                records.append(json.loads(raw))
        return sorted(records, key=lambda record: record["attempt"])

    def decide(self, key: str, stale_seconds: float, max_attempts: int) -> Decision:
        attempts = self.attempts(key)
        for record in attempts:
            if record["status"] == UPLOADED:
                return Decision(DONE, f"déjà envoyé (vidéo {record.get('video_id') or 'inconnue'})", attempts)
        for record in attempts:
            if record["status"] == PENDING and record.get("phase") == UPLOADING:
                return Decision(
                    BLOCKED,
                    "envoi interrompu, issue inconnue : vérifier YouTube Studio, puis "
                    f"« ytclips history --remote --release {key[:12]} » si aucune vidéo n'a été créée",
                    attempts,
                )
        last = attempts[-1] if attempts else None
        if last and last["status"] == PENDING and age_seconds(last["updated_at"]) < stale_seconds:
            minutes = int(age_seconds(last["updated_at"]) // 60)
            return Decision(BUSY, f"en cours dans une autre exécution (depuis {minutes} min)", attempts)
        counted = [record for record in attempts if record.get("kind") not in UNCOUNTED]
        if len(counted) >= max_attempts:
            error = (counted[-1].get("error") or "interrompu sans message")[:200]
            return Decision(EXHAUSTED, f"abandonné après {len(counted)} essai(s) : {error}", attempts)
        return Decision(TAKE, "", attempts)

    def claim(self, key: str, number: int, fields: dict) -> dict | None:
        """Crée la tentative n° `number` ; None si une autre exécution l'a déjà créée."""
        stamp = now()
        record = {
            "content_key": key, "attempt": number, "status": PENDING, "phase": CLAIMED,
            "kind": None, "video_id": None, "privacy": None, "error": None,
            "created_at": stamp, "updated_at": stamp, **fields,
        }
        created = self.storage.write(
            self._key(key, number), json.dumps(record, ensure_ascii=False).encode(), if_absent=True
        )
        return record if created else None

    def update(self, record: dict, **fields) -> None:
        record.update(fields, updated_at=now())
        self.storage.write(
            self._key(record["content_key"], record["attempt"]),
            json.dumps(record, ensure_ascii=False).encode(),
        )

    def fail(self, record: dict, kind: str, error: str) -> None:
        self.update(record, status=FAILED, kind=kind, error=error[:1000])

    def conflict(self, record: dict) -> str | None:
        """À appeler juste avant l'envoi, une fois la phase « uploading » écrite."""
        for other in self.attempts(record["content_key"]):
            if other["attempt"] == record["attempt"]:
                continue
            if other["attempt"] > record["attempt"]:
                return f"tentative remplacée par la n° {other['attempt']}"
            if other["status"] == UPLOADED or (other["status"] == PENDING and other.get("phase") == UPLOADING):
                return f"la tentative n° {other['attempt']} a déjà engagé l'envoi"
        return None

    # --- consultation ---

    def records(self) -> list[dict]:
        records = []
        for info in self.storage.list(HISTORY):
            raw = self.storage.read(info.key)
            if raw:
                records.append(json.loads(raw))
        return sorted(records, key=lambda record: (record["created_at"], record["content_key"], record["attempt"]))

    def release(self, prefix: str) -> dict | None:
        """Libère une tentative « pending » désignée par le début de sa clé d'extrait."""
        pending = [
            record for record in self.records()
            if record["content_key"].startswith(prefix) and record["status"] == PENDING
        ]
        if len({record["content_key"] for record in pending}) != 1:
            return None
        record = pending[-1]
        self.fail(record, "released", (record.get("error") or "") + " [libéré à la main]")
        return record
