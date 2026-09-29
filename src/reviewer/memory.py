"""Persistent memory for the review agent.

Three things are remembered:

* rules     - team coding standards / architectural preferences (taught manually,
              extracted from old review comments, or learned from pushback).
* findings  - every suggestion the agent ever made, with the team's verdict.
* patterns  - aggregated stats per "pattern_key" (e.g. "missing-error-handling"):
              how often it was raised, accepted, rejected.

Together they let the agent (a) focus on mistakes your team really makes and
(b) stop repeating suggestions your team already rejected.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT NOT NULL UNIQUE,
    category   TEXT NOT NULL DEFAULT 'general',
    language   TEXT NOT NULL DEFAULT '*',
    source     TEXT NOT NULL DEFAULT 'manual',
    weight     REAL NOT NULL DEFAULT 1.0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id   TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    pattern_key TEXT NOT NULL,
    category    TEXT,
    severity    TEXT,
    file        TEXT,
    line        INTEGER,
    message     TEXT NOT NULL,
    suggestion  TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    reason      TEXT,
    created_at  REAL NOT NULL,
    resolved_at REAL
);
CREATE INDEX IF NOT EXISTS idx_findings_pattern ON findings(pattern_key, file);
CREATE INDEX IF NOT EXISTS idx_findings_fp ON findings(fingerprint);

CREATE TABLE IF NOT EXISTS patterns (
    pattern_key TEXT PRIMARY KEY,
    description TEXT,
    category    TEXT,
    seen        INTEGER NOT NULL DEFAULT 0,
    accepted    INTEGER NOT NULL DEFAULT 0,
    rejected    INTEGER NOT NULL DEFAULT 0,
    last_seen   REAL
);
"""

VALID_STATUSES = {"accepted", "rejected", "ignored"}

EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
    ".rb": "ruby", ".php": "php", ".cs": "csharp", ".cpp": "cpp", ".cc": "cpp",
    ".c": "c", ".h": "c", ".swift": "swift", ".sql": "sql", ".sh": "shell",
}


def language_of(path: str | None) -> str:
    if not path:
        return "*"
    return EXT_TO_LANG.get(Path(path).suffix.lower(), "*")


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", text.lower())).strip()


def make_fingerprint(pattern_key: str, file: str | None, message: str) -> str:
    raw = f"{pattern_key}|{file or ''}|{normalize(message)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:60] or "general"


class Memory:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        if str(db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)  # wait, don't fail, if another request writes
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def reset(self) -> None:
        """Wipe everything (used by the demo's reset button)."""
        for table in ("findings", "patterns", "rules"):
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.execute("DELETE FROM sqlite_sequence")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ rules
    def add_rule(self, text: str, category: str = "general", language: str = "*",
                 source: str = "manual", weight: float = 1.0) -> int | None:
        """Add a rule. Returns its id, or None if an identical rule already exists."""
        text = text.strip()
        if not text:
            return None
        try:
            cur = self.conn.execute(
                "INSERT INTO rules(text, category, language, source, weight, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (text, category, language, source, weight, time.time()),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # Same rule again -> reinforce it instead of duplicating.
            self.conn.execute("UPDATE rules SET weight = weight + 0.5 WHERE text = ?", (text,))
            self.conn.commit()
            return None

    def list_rules(self, languages: Iterable[str] | None = None, limit: int | None = None):
        sql = "SELECT * FROM rules"
        params: list = []
        if languages is not None:
            langs = set(languages) | {"*"}
            sql += f" WHERE language IN ({','.join('?' * len(langs))})"
            params += sorted(langs)
        sql += " ORDER BY weight DESC, id ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def delete_rule(self, rule_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        self.conn.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------------- findings
    def record_review(self, review_id: str, findings: list[dict]) -> list[dict]:
        """Persist findings from one review and update pattern stats.

        Each finding dict gets an ``id`` added. Returns the same list.
        """
        now = time.time()
        for f in findings:
            fp = make_fingerprint(f["pattern_key"], f.get("file"), f["message"])
            cur = self.conn.execute(
                "INSERT INTO findings(review_id, fingerprint, pattern_key, category, severity, "
                "file, line, message, suggestion, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (review_id, fp, f["pattern_key"], f.get("category"), f.get("severity"),
                 f.get("file"), f.get("line"), f["message"], f.get("suggestion"), now),
            )
            f["id"] = cur.lastrowid
            f["fingerprint"] = fp
            self.conn.execute(
                "INSERT INTO patterns(pattern_key, description, category, seen, last_seen) "
                "VALUES (?,?,?,1,?) ON CONFLICT(pattern_key) DO UPDATE SET "
                "seen = seen + 1, last_seen = excluded.last_seen",
                (f["pattern_key"], f["message"][:200], f.get("category"), now),
            )
        self.conn.commit()
        return findings

    def get_finding(self, finding_id: int):
        return self.conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()

    def set_status(self, finding_id: int, status: str, reason: str | None = None) -> bool:
        """Record the team's verdict on a finding and learn from it."""
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        row = self.get_finding(finding_id)
        if row is None:
            return False
        if row["status"] == status:
            return True  # idempotent; don't double count

        # Undo the previous verdict's counter (if the user changes their mind).
        if row["status"] in ("accepted", "rejected"):
            self.conn.execute(
                f"UPDATE patterns SET {row['status']} = MAX({row['status']} - 1, 0) "
                "WHERE pattern_key = ?", (row["pattern_key"],))

        self.conn.execute(
            "UPDATE findings SET status = ?, reason = ?, resolved_at = ? WHERE id = ?",
            (status, reason, time.time(), finding_id))
        if status in ("accepted", "rejected"):
            self.conn.execute(
                f"UPDATE patterns SET {status} = {status} + 1 WHERE pattern_key = ?",
                (row["pattern_key"],))
        self.conn.commit()

        # A rejection with a reason is a team convention we should remember.
        if status == "rejected" and reason:
            self.add_rule(
                f"Do NOT flag: {row['message'][:160]} (team says: {reason})",
                category="exception", language=language_of(row["file"]),
                source="feedback", weight=1.5)
        return True

    # ----------------------------------------------------------- suppression
    def is_suppressed(self, pattern_key: str, file: str | None, message: str,
                      reject_threshold: int = 2) -> bool:
        """Should we stay quiet about this finding?"""
        fp = make_fingerprint(pattern_key, file, message)
        # 1. This exact suggestion was already rejected.
        if self.conn.execute(
            "SELECT 1 FROM findings WHERE fingerprint = ? AND status = 'rejected' LIMIT 1",
            (fp,)).fetchone():
            return True
        # 2. Same pattern rejected before on the same file.
        if file and self.conn.execute(
            "SELECT 1 FROM findings WHERE pattern_key = ? AND file = ? AND status = 'rejected' "
            "LIMIT 1", (pattern_key, file)).fetchone():
            return True
        # 3. Team-wide: pattern rejected repeatedly and rejections beat acceptances.
        p = self.conn.execute(
            "SELECT accepted, rejected FROM patterns WHERE pattern_key = ?",
            (pattern_key,)).fetchone()
        if p and p["rejected"] >= reject_threshold and p["rejected"] > p["accepted"]:
            return True
        return False

    # ---------------------------------------------------------- prompt input
    def frequent_mistakes(self, limit: int = 10):
        """Patterns the team has *accepted* most often = mistakes that really recur."""
        return self.conn.execute(
            "SELECT pattern_key, description, category, seen, accepted, rejected FROM patterns "
            "WHERE accepted > 0 AND accepted >= rejected ORDER BY accepted DESC, seen DESC LIMIT ?",
            (limit,)).fetchall()

    def recent_pushback(self, limit: int = 15):
        """Most recent rejections, without repeats of the same (pattern, reason)."""
        rows = self.conn.execute(
            "SELECT pattern_key, message, reason, file FROM findings WHERE status = 'rejected' "
            "ORDER BY resolved_at DESC, id DESC LIMIT ?", (limit * 4,)).fetchall()
        seen, out = set(), []
        for r in rows:
            key = (r["pattern_key"], r["reason"])
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            if len(out) == limit:
                break
        return out

    # ------------------------------------------------------------- dashboard
    def list_findings(self, status: str | None = None, limit: int = 100) -> list[dict]:
        sql, params = "SELECT * FROM findings", []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def list_patterns(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM patterns ORDER BY seen DESC, last_seen DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows.fetchall()]

    def stats(self) -> dict:
        q = lambda sql: self.conn.execute(sql).fetchone()[0]  # noqa: E731
        total = q("SELECT COUNT(*) FROM findings")
        acc = q("SELECT COUNT(*) FROM findings WHERE status='accepted'")
        rej = q("SELECT COUNT(*) FROM findings WHERE status='rejected'")
        judged = acc + rej
        return {
            "rules": q("SELECT COUNT(*) FROM rules"),
            "findings": total,
            "accepted": acc,
            "rejected": rej,
            "open": q("SELECT COUNT(*) FROM findings WHERE status='open'"),
            "acceptance_rate": (acc / judged) if judged else None,
            "patterns": q("SELECT COUNT(*) FROM patterns"),
        }
