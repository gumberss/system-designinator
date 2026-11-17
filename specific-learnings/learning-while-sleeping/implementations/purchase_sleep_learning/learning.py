"""CSV input, persistent experiences, and retrieval of teacher-verified examples."""

from __future__ import annotations

import csv
import hashlib
import json
import random
import re
import sqlite3
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class DemoError(Exception):
    """An actionable input, configuration, or model-response error."""


@dataclass(frozen=True)
class Purchase:
    id: str
    description: str
    expected_category: str | None = None


@dataclass(frozen=True)
class Prediction:
    category: str
    reason: str


@dataclass(frozen=True)
class Assessment:
    category: str
    reason: str
    certain: bool


@dataclass(frozen=True)
class Memory:
    description: str
    category: str
    reason: str
    support: int


def normalize(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.findall(r"[a-z0-9]+", ascii_text.lower()))


def load_categories(path: Path) -> dict[str, str]:
    categories = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(categories, dict) or not categories:
        raise DemoError("Categories must be a non-empty JSON object.")
    if any(
        not isinstance(key, str) or not key.strip()
        or not isinstance(value, str) or not value.strip()
        for key, value in categories.items()
    ):
        raise DemoError("Every category needs a non-empty name and definition.")
    if "indeterminado" not in categories:
        raise DemoError("Categories must include 'indeterminado' for insufficient evidence.")
    return categories


def load_purchases(
    path: Path, categories: dict[str, str], *, require_labels: bool = False
) -> list[Purchase]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(8192)
        stream.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error as exc:
            raise DemoError("Cannot detect CSV delimiter; use comma, semicolon, or tab.") from exc
        reader = csv.DictReader(stream, dialect=dialect, strict=True)
        headers = reader.fieldnames or []
        if len(set(headers)) != len(headers):
            raise DemoError("CSV has duplicate column names.")
        required = {"id", "description"}
        if require_labels:
            required.add("expected_category")
        if not required.issubset(headers):
            raise DemoError(f"CSV must contain columns: {', '.join(sorted(required))}.")
        purchases = []
        seen_ids = set()
        seen_descriptions = set()
        for row in reader:
            if None in row or any(value is None for value in row.values()):
                raise DemoError(f"Malformed CSV row near line {reader.line_num}.")
            purchase_id = row["id"].strip()
            description = row["description"].strip()
            expected = row.get("expected_category", "").strip() or None
            if not purchase_id or not description or not normalize(description):
                raise DemoError(f"Empty id or description near line {reader.line_num}.")
            if len(description) > 2000:
                raise DemoError(f"Description too long for purchase {purchase_id!r} (max 2000).")
            if purchase_id in seen_ids:
                raise DemoError(f"Duplicate purchase id: {purchase_id!r}.")
            if require_labels and normalize(description) in seen_descriptions:
                raise DemoError("Evaluation CSV must not repeat normalized descriptions.")
            if (expected is not None and expected not in categories) or (
                require_labels and expected is None
            ):
                raise DemoError(f"Invalid expected_category for purchase {purchase_id!r}.")
            seen_ids.add(purchase_id)
            seen_descriptions.add(normalize(description))
            purchases.append(Purchase(purchase_id, description, expected))
    if not purchases:
        raise DemoError("CSV contains no purchases.")
    return purchases


STOP_WORDS = {"de", "da", "do", "das", "dos", "em", "no", "na", "compra", "pagamento", "cartao"}


def retrieve_memories(description: str, memories: Iterable[Memory], limit: int) -> list[Memory]:
    query = set(normalize(description).split()) - STOP_WORDS
    ranked = []
    for memory in memories:
        terms = set(normalize(memory.description).split()) - STOP_WORDS
        overlap = len(query & terms)
        if overlap:
            similarity = overlap / len(query | terms)
            ranked.append((similarity, memory.support, memory.description, memory))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in ranked[:limit]]


def choose_audits(
    decisions: list[tuple[int, Purchase, Prediction]], size: int, rng: random.Random
) -> list[tuple[int, Purchase, Prediction]]:
    sampled_ids = {item[0] for item in rng.sample(decisions, min(size, len(decisions)))}
    return [
        item for item in decisions
        if item[0] in sampled_ids or item[2].category == "indeterminado"
    ]


class Store:
    def __init__(self, path: Path, categories: dict[str, str]):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL,
                purchase_id TEXT NOT NULL,
                description TEXT NOT NULL,
                memory_key TEXT NOT NULL,
                category TEXT NOT NULL,
                reason TEXT NOT NULL,
                model TEXT NOT NULL,
                memory_revision INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audits (
                decision_id INTEGER PRIMARY KEY REFERENCES decisions(id),
                category TEXT NOT NULL,
                reason TEXT NOT NULL,
                certain INTEGER NOT NULL CHECK (certain IN (0, 1)),
                reward INTEGER CHECK (reward IN (-1, 1)),
                model TEXT NOT NULL,
                consolidated INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memories (
                memory_key TEXT PRIMARY KEY,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                reason TEXT NOT NULL,
                support INTEGER NOT NULL
            );
            """
        )
        taxonomy = json.dumps(categories, sort_keys=True, ensure_ascii=True)
        fingerprint = hashlib.sha256(taxonomy.encode()).hexdigest()
        stored = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'taxonomy'"
        ).fetchone()
        if stored and stored["value"] != fingerprint:
            self.connection.close()
            raise DemoError("Categories changed. Use a new --db instead of mixing taxonomies.")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('taxonomy', ?)", (fingerprint,)
            )
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('revision', '0')"
            )

    def close(self) -> None:
        self.connection.close()

    @property
    def revision(self) -> int:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = 'revision'"
        ).fetchone()
        return int(row["value"])

    def record_decision(
        self, run_id: str, purchase: Purchase, prediction: Prediction, model: str
    ) -> int:
        with self.connection:
            cursor = self.connection.execute(
                """INSERT INTO decisions
                   (run_id, purchase_id, description, memory_key, category, reason,
                    model, memory_revision, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, purchase.id, purchase.description, normalize(purchase.description),
                    prediction.category, prediction.reason, model, self.revision,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        if cursor.lastrowid is None:
            raise DemoError("Database did not return a decision id.")
        return cursor.lastrowid

    def record_audit(self, decision_id: int, assessment: Assessment, model: str) -> int | None:
        decision = self.connection.execute(
            "SELECT category FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        if decision is None:
            raise DemoError(f"Cannot audit missing decision {decision_id}.")
        reward = None
        if assessment.certain:
            reward = 1 if decision["category"] == assessment.category else -1
        with self.connection:
            self.connection.execute(
                """INSERT INTO audits (decision_id, category, reason, certain, reward, model)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    decision_id, assessment.category, assessment.reason,
                    int(assessment.certain), reward, model,
                ),
            )
        return reward

    def memories(self) -> list[Memory]:
        rows = self.connection.execute(
            "SELECT description, category, reason, support FROM memories ORDER BY memory_key"
        )
        return [Memory(**dict(row)) for row in rows]

    def sleep(self) -> dict:
        pending = self.connection.execute(
            "SELECT COUNT(*) FROM audits WHERE consolidated = 0"
        ).fetchone()[0]
        if not pending:
            return {"pending_audits": 0, "memories": len(self.memories()), "revision": self.revision}
        evidence = self.connection.execute(
            """SELECT d.memory_key, d.description, a.category, a.reason
               FROM audits a JOIN decisions d ON d.id = a.decision_id
               WHERE a.certain = 1 ORDER BY d.id"""
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for row in evidence:
            groups[row["memory_key"]].append(row)
        conflicts = 0
        with self.connection:
            self.connection.execute("DELETE FROM memories")
            for key, rows in groups.items():
                if len({row["category"] for row in rows}) != 1:
                    conflicts += 1
                    continue
                latest = rows[-1]
                self.connection.execute(
                    "INSERT INTO memories VALUES (?, ?, ?, ?, ?)",
                    (
                        key, latest["description"], latest["category"],
                        latest["reason"], len(rows),
                    ),
                )
            self.connection.execute("UPDATE audits SET consolidated = 1")
            self.connection.execute(
                "UPDATE metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'revision'"
            )
        return {
            "pending_audits": pending, "memories": len(self.memories()),
            "conflicting_descriptions_excluded": conflicts, "revision": self.revision,
        }

    def assert_holdout(self, purchases: list[Purchase]) -> None:
        learned = {
            row[0] for row in self.connection.execute("SELECT DISTINCT memory_key FROM decisions")
        }
        overlap = [purchase.id for purchase in purchases if normalize(purchase.description) in learned]
        if overlap:
            raise DemoError(
                "Evaluation overlaps learning descriptions; use unseen purchases. "
                f"Overlapping ids: {', '.join(overlap[:5])}."
            )

    def stats(self) -> dict:
        decisions = self.connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        row = self.connection.execute(
            """SELECT COUNT(*) AS audited, COUNT(reward) AS trusted,
                      SUM(CASE WHEN reward = 1 THEN 1 ELSE 0 END) AS agreements,
                      SUM(CASE WHEN consolidated = 0 THEN 1 ELSE 0 END) AS pending
               FROM audits"""
        ).fetchone()
        return {
            "decisions": decisions,
            "audited": row["audited"],
            "audit_coverage": row["audited"] / decisions if decisions else None,
            "trusted_audits": row["trusted"],
            "uncertain_audits": row["audited"] - row["trusted"],
            "teacher_agreement": row["agreements"] / row["trusted"] if row["trusted"] else None,
            "pending_sleep": row["pending"] or 0,
            "memories": len(self.memories()),
            "revision": self.revision,
        }
