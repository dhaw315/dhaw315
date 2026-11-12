"""Propositions d'extraits conservées dans le stockage durable : une fiche JSON par extrait."""

from __future__ import annotations

import json

from .ledger import now

PREFIX = "state/proposals/"
PROPOSED, APPROVED, REJECTED, PUBLISHED = "proposed", "approved", "rejected", "published"
LABELS = {PROPOSED: "proposé", APPROVED: "validé", REJECTED: "refusé", PUBLISHED: "publié"}


class Book:
    def __init__(self, storage):
        self.storage = storage

    @staticmethod
    def _key(content_key: str) -> str:
        return f"{PREFIX}{content_key}.json"

    def create(self, record: dict) -> bool:
        """N'écrase jamais une proposition existante ni la décision prise à son sujet."""
        stamp = now()
        record = {"status": PROPOSED, "video_id": None, "privacy": None, "created_at": stamp, "updated_at": stamp, **record}
        return self.storage.write(
            self._key(record["content_key"]), json.dumps(record, ensure_ascii=False, indent=2).encode(), if_absent=True
        )

    def save(self, record: dict) -> None:
        record["updated_at"] = now()
        self.storage.write(self._key(record["content_key"]), json.dumps(record, ensure_ascii=False, indent=2).encode())

    def all(self) -> list[dict]:
        records = []
        for info in self.storage.list(PREFIX):
            raw = self.storage.read(info.key)
            if raw:
                records.append(json.loads(raw))
        return sorted(records, key=lambda r: (r["created_at"], r["source_key"], r["start_ms"]))

    def find(self, prefix: str) -> dict | None:
        """Proposition désignée par le début de son identifiant, s'il est sans ambiguïté."""
        matches = [record for record in self.all() if record["content_key"].startswith(prefix)]
        return matches[0] if len(matches) == 1 else None
