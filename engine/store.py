"""Run history and mailbox state.

Postgres (Supabase) when DATABASE_URL is set, SQLite otherwise. The engine only
ever touches the Store interface, so the demo runs with no database at all and
the same code path serves a hosted Supabase project in production.

History is the point. A single reputation reading is noise; the same mailbox
sliding for four consecutive runs is a decision. Storing every reading is also
what lets a benched mailbox prove it served its rest period.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "sql" / "schema.sql"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sqlite_schema(sql: str) -> str:
    """Reduce the Postgres schema to types SQLite accepts.

    Keeping one schema file avoids the two drifting apart; SQLite is
    permissive enough that a handful of substitutions is the whole difference.
    """
    sql = sql.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
    sql = sql.replace("TIMESTAMPTZ", "TEXT")
    sql = sql.replace("BIGINT", "INTEGER")
    sql = sql.replace("BOOLEAN", "INTEGER")
    sql = sql.replace("REAL", "REAL")
    sql = re.sub(r"\bDEFAULT FALSE\b", "DEFAULT 0", sql)
    return sql


class Store:
    """Shared behaviour; subclasses supply the connection and placeholder style."""

    placeholder = "?"

    def __init__(self, conn):
        self.conn = conn

    # -- plumbing ---------------------------------------------------------

    def _sql(self, sql: str) -> str:
        if self.placeholder == "?":
            return sql
        return sql.replace("?", self.placeholder)

    def execute(self, sql: str, params: tuple = ()):
        cur = self.conn.cursor()
        cur.execute(self._sql(sql), params)
        return cur

    def executemany(self, sql: str, rows: Iterable[tuple]):
        rows = list(rows)
        if not rows:
            return
        cur = self.conn.cursor()
        cur.executemany(self._sql(sql), rows)

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- runs -------------------------------------------------------------

    def start_run(self, mode: str) -> str:
        run_id = uuid.uuid4().hex[:12]
        self.execute(
            "INSERT INTO runs (id, started_at, mode) VALUES (?, ?, ?)",
            (run_id, _now().isoformat(), mode),
        )
        self.commit()
        return run_id

    def finish_run(self, run_id: str, summary: str) -> None:
        self.execute(
            "UPDATE runs SET finished_at = ?, summary = ? WHERE id = ?",
            (_now().isoformat(), summary, run_id),
        )
        self.commit()

    # -- readings ---------------------------------------------------------

    def record_mailbox_health(self, run_id: str, readings: Iterable[dict]) -> None:
        ts = _now().isoformat()
        self.executemany(
            """INSERT INTO mailbox_health
               (run_id, observed_at, mailbox_id, email, domain, campaign_id,
                reputation, spam_rate, warmup_sent, smtp_ok, imap_ok, verdict, reasons)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    run_id, ts, r["mailbox_id"], r["email"], r["domain"],
                    r.get("campaign_id"), r.get("reputation"), r.get("spam_rate"),
                    r.get("warmup_sent"), int(bool(r.get("smtp_ok"))),
                    int(bool(r.get("imap_ok"))), r["verdict"],
                    "; ".join(r.get("reasons", [])),
                )
                for r in readings
            ],
        )
        self.commit()

    def record_domain_health(self, run_id: str, readings: Iterable[dict]) -> None:
        ts = _now().isoformat()
        self.executemany(
            """INSERT INTO domain_health
               (run_id, observed_at, domain, workspace, score, label, blacklisted,
                blacklists, spf, dkim, dmarc, verdict, reasons)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    run_id, ts, r["domain"], r.get("workspace"), r.get("score"),
                    r.get("label"), int(bool(r.get("blacklisted"))),
                    ",".join(r.get("blacklists", [])), int(bool(r.get("spf"))),
                    int(bool(r.get("dkim"))), int(bool(r.get("dmarc"))),
                    r["verdict"], "; ".join(r.get("reasons", [])),
                )
                for r in readings
            ],
        )
        self.commit()

    def record_action(self, run_id: str, action: dict, applied: bool, error: str | None = None) -> None:
        self.execute(
            """INSERT INTO actions
               (run_id, created_at, action, campaign_id, mailbox_id, target,
                reason, payload, applied, error)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, _now().isoformat(), action["action"], action.get("campaign_id"),
                action.get("mailbox_id"), action["target"], action["reason"],
                json.dumps(action.get("payload", {})), int(applied), error,
            ),
        )
        self.commit()

    # -- mailbox state ----------------------------------------------------

    def all_states(self) -> dict[int, dict]:
        cur = self.execute(
            """SELECT mailbox_id, email, domain, status, daily_cap, benched_at,
                      cooldown_until, bench_count, last_seen_at, notes
               FROM mailbox_state"""
        )
        cols = ["mailbox_id", "email", "domain", "status", "daily_cap", "benched_at",
                "cooldown_until", "bench_count", "last_seen_at", "notes"]
        return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}

    def upsert_state(self, mailbox_id: int, email: str, domain: str, **fields: Any) -> None:
        existing = self.execute(
            "SELECT mailbox_id FROM mailbox_state WHERE mailbox_id = ?", (mailbox_id,)
        ).fetchone()
        fields.setdefault("last_seen_at", _now().isoformat())
        if existing:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.execute(
                f"UPDATE mailbox_state SET {sets} WHERE mailbox_id = ?",
                (*fields.values(), mailbox_id),
            )
        else:
            cols = ["mailbox_id", "email", "domain", *fields.keys()]
            marks = ",".join("?" for _ in cols)
            fields.setdefault("status", "active")
            self.execute(
                f"INSERT INTO mailbox_state ({','.join(cols)}) VALUES ({marks})",
                (mailbox_id, email, domain, *fields.values()),
            )
        self.commit()

    def bench(self, mailbox_id: int, email: str, domain: str, rest_days: int) -> None:
        prior = self.execute(
            "SELECT bench_count FROM mailbox_state WHERE mailbox_id = ?", (mailbox_id,)
        ).fetchone()
        count = (prior[0] if prior else 0) + 1
        now = _now()
        self.upsert_state(
            mailbox_id, email, domain,
            status="benched",
            benched_at=now.isoformat(),
            cooldown_until=(now + timedelta(days=rest_days)).isoformat(),
            bench_count=count,
        )

    def retire(self, mailbox_id: int, email: str, domain: str, note: str) -> None:
        self.upsert_state(mailbox_id, email, domain, status="retired", notes=note)

    # -- trends -----------------------------------------------------------

    def domain_trend(self, domain: str, limit: int = 10) -> list[tuple]:
        """Recent verdicts for one domain, newest first. Feeds the digest."""
        cur = self.execute(
            """SELECT observed_at, score, blacklisted, verdict
               FROM domain_health WHERE domain = ?
               ORDER BY observed_at DESC LIMIT ?""",
            (domain, limit),
        )
        return cur.fetchall()


class SqliteStore(Store):
    placeholder = "?"

    @classmethod
    def open(cls, path: str) -> "SqliteStore":
        conn = sqlite3.connect(path)
        conn.executescript(_sqlite_schema(SCHEMA.read_text()))
        conn.commit()
        return cls(conn)


class PostgresStore(Store):
    placeholder = "%s"

    @classmethod
    def open(cls, dsn: str) -> "PostgresStore":
        try:
            import psycopg  # type: ignore
            conn = psycopg.connect(dsn)
        except ImportError:
            try:
                import psycopg2  # type: ignore
                conn = psycopg2.connect(dsn)
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise SystemExit(
                    "DATABASE_URL is set but no Postgres driver is installed.\n"
                    "Run: pip install 'psycopg[binary]'\n"
                    "Or clear DATABASE_URL to use the local SQLite store."
                ) from exc
        store = cls(conn)
        cur = conn.cursor()
        cur.execute(SCHEMA.read_text())
        conn.commit()
        return store


def open_store(config) -> Store:
    """Postgres when a DSN is configured, SQLite otherwise."""
    dsn = config.secrets.database_url
    if dsn:
        return PostgresStore.open(dsn)
    return SqliteStore.open(config.secrets.sqlite_path)
