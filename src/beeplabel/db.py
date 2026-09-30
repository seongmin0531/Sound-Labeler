"""SQLite storage: user ids, sessions, settings, per-user labels and a full audit log.

Login is by id only (the team shares one server by design), so an id identifies who did
the work but is not proof of identity. Everything is in one file
(default: ~/beeplabel_data/beeplabel.db) and survives restarts, logouts and re-logins.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    pw_hash TEXT NOT NULL DEFAULT '',     -- unused (id-only login), kept for old databases
    salt TEXT NOT NULL DEFAULT '',
    is_admin INTEGER NOT NULL DEFAULT 0,  -- unused, kept for old databases
    theme TEXT NOT NULL DEFAULT 'dark',
    last_file TEXT,
    created_at TEXT NOT NULL,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS labels (
    dataset TEXT NOT NULL,
    file TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id),
    start_sample INTEGER,         -- NULL = no beep in this sample
    end_sample INTEGER,           -- exclusive; end - start <= 100 ms
    start_s REAL,
    end_s REAL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    memo TEXT NOT NULL DEFAULT '',
    sample_rate INTEGER,
    num_samples INTEGER,
    sha256 TEXT,
    n_edits INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset, file, user_id)
);
CREATE TABLE IF NOT EXISTS auto_regions2 (
    dataset TEXT NOT NULL,
    file TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    start_sample INTEGER,
    end_sample INTEGER,
    start_s REAL,
    end_s REAL,
    PRIMARY KEY (dataset, file)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id TEXT,
    dataset TEXT,
    file TEXT,
    action TEXT NOT NULL,
    before TEXT,
    after TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS audit_file ON audit(dataset, file);
CREATE INDEX IF NOT EXISTS audit_user ON audit(user_id);
"""


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def region_text(start_s, end_s) -> str:
    if start_s is None:
        return "비프 없음"
    return f"{start_s:.3f}~{end_s:.3f}초"


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c):
        cols = {r["name"] for r in c.execute("PRAGMA table_info(labels)")}
        if "start_sample" not in cols:  # v0.2 stored 100 ms-grid frame indices
            c.execute("ALTER TABLE labels ADD COLUMN start_sample INTEGER")
            c.execute("ALTER TABLE labels ADD COLUMN end_sample INTEGER")
            c.execute("""UPDATE labels SET
                start_sample = CAST(ROUND(start_s * sample_rate) AS INTEGER),
                end_sample = CAST(ROUND(end_s * sample_rate) AS INTEGER)
                WHERE start_s IS NOT NULL""")

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with self._lock:
                yield c
                c.commit()
        finally:
            c.close()

    # ---------- settings ----------
    def get_setting(self, key: str, default=None):
        with self.conn() as c:
            r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set_setting(self, key: str, value: str) -> None:
        with self.conn() as c:
            c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------- users ----------
    def create_user(self, user_id: str, display_name: str = "") -> dict:
        with self.conn() as c:
            c.execute("INSERT INTO users(id,display_name,created_at) VALUES(?,?,?)",
                      (user_id, display_name or user_id, now()))
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT id,display_name,theme,last_file,created_at,last_login "
                          "FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(r) if r else None

    def users(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id,display_name,created_at,last_login FROM users ORDER BY id")]

    def set_prefs(self, user_id: str, **prefs) -> None:
        allowed = {k: v for k, v in prefs.items() if k in ("theme", "last_file", "display_name")}
        if not allowed:
            return
        cols = ", ".join(f"{k}=?" for k in allowed)
        with self.conn() as c:
            c.execute(f"UPDATE users SET {cols} WHERE id=?", (*allowed.values(), user_id))

    # ---------- sessions ----------
    def new_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        t = now()
        with self.conn() as c:
            c.execute("INSERT INTO sessions(token,user_id,created_at,last_seen) VALUES(?,?,?,?)",
                      (token, user_id, t, t))
            c.execute("UPDATE users SET last_login=? WHERE id=?", (t, user_id))
        return token

    def session_user(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.conn() as c:
            r = c.execute("SELECT user_id FROM sessions WHERE token=?", (token,)).fetchone()
            if not r:
                return None
            c.execute("UPDATE sessions SET last_seen=? WHERE token=?", (now(), token))
        return self.get_user(r["user_id"])

    def end_session(self, token: str) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (token,))

    # ---------- labels ----------
    def get_label(self, dataset: str, file: str, user_id: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM labels WHERE dataset=? AND file=? AND user_id=?",
                          (dataset, file, user_id)).fetchone()
        return dict(r) if r else None

    def labels(self, dataset: str | None = None, user_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM labels WHERE 1=1", []
        if dataset is not None:
            q += " AND dataset=?"
            args.append(dataset)
        if user_id is not None:
            q += " AND user_id=?"
            args.append(user_id)
        with self.conn() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY file, user_id", args)]

    def save_label(self, dataset: str, file: str, user_id: str, *, start_sample, end_sample,
                   start_s, end_s, reviewed: bool, memo: str, sample_rate: int,
                   num_samples: int, sha256: str) -> tuple[dict, list[str]]:
        """Upserts the label and writes one audit row per kind of change. Returns (label, actions)."""
        t = now()
        with self.conn() as c:
            old = c.execute("SELECT * FROM labels WHERE dataset=? AND file=? AND user_id=?",
                            (dataset, file, user_id)).fetchone()
            old = dict(old) if old else None
            actions = []
            new_region = (start_sample, end_sample)
            old_region = (old["start_sample"], old["end_sample"]) if old else None
            if old is None:
                actions.append(("구간 지정" if start_sample is not None else "비프 없음 지정",
                                None, region_text(start_s, end_s)))
            elif old_region != new_region:
                if start_sample is None:
                    act = "구간 삭제(비프 없음)"
                elif old_region[0] is None:
                    act = "구간 지정"
                else:
                    act = "구간 수정"
                actions.append((act, region_text(old["start_s"], old["end_s"]),
                                region_text(start_s, end_s)))
            if old is not None and (old["memo"] or "") != (memo or ""):
                actions.append(("메모 수정", old["memo"] or "", memo or ""))
            elif old is None and memo:
                actions.append(("메모 작성", "", memo))
            if bool(old and old["reviewed"]) != bool(reviewed):
                actions.append(("검수 완료" if reviewed else "검수 취소", None,
                                region_text(start_s, end_s)))
            if old is None:
                c.execute("""INSERT INTO labels(dataset,file,user_id,start_sample,end_sample,start_s,
                             end_s,reviewed,memo,sample_rate,num_samples,sha256,n_edits,
                             created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (dataset, file, user_id, start_sample, end_sample, start_s, end_s,
                           int(reviewed), memo or "", sample_rate, num_samples, sha256,
                           len(actions), t, t))
            elif actions:
                c.execute("""UPDATE labels SET start_sample=?,end_sample=?,start_s=?,end_s=?,
                             reviewed=?,memo=?,sample_rate=?,num_samples=?,sha256=?,
                             n_edits=n_edits+?,updated_at=? WHERE dataset=? AND file=? AND user_id=?""",
                          (start_sample, end_sample, start_s, end_s, int(reviewed), memo or "",
                           sample_rate, num_samples, sha256, len(actions), t,
                           dataset, file, user_id))
            for act, before, after in actions:
                c.execute("INSERT INTO audit(ts,user_id,dataset,file,action,before,after) "
                          "VALUES(?,?,?,?,?,?,?)", (t, user_id, dataset, file, act, before, after))
            r = c.execute("SELECT * FROM labels WHERE dataset=? AND file=? AND user_id=?",
                          (dataset, file, user_id)).fetchone()
        return dict(r), [a[0] for a in actions]

    # ---------- auto suggestions ----------
    def get_auto(self, dataset: str, file: str, sha256: str, digest: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT * FROM auto_regions2 WHERE dataset=? AND file=? AND sha256=? "
                          "AND config_digest=?", (dataset, file, sha256, digest)).fetchone()
        return dict(r) if r else None

    def put_auto(self, dataset, file, sha256, digest, start_sample, end_sample, start_s, end_s):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO auto_regions2 VALUES(?,?,?,?,?,?,?,?)",
                      (dataset, file, sha256, digest, start_sample, end_sample, start_s, end_s))

    # ---------- audit ----------
    def log(self, user_id: str | None, action: str, dataset: str | None = None,
            file: str | None = None, before=None, after=None, detail=None) -> None:
        if isinstance(detail, (dict, list)):
            detail = json.dumps(detail, ensure_ascii=False)
        with self.conn() as c:
            c.execute("INSERT INTO audit(ts,user_id,dataset,file,action,before,after,detail) "
                      "VALUES(?,?,?,?,?,?,?,?)", (now(), user_id, dataset, file, action,
                                                  before, after, detail))

    def audit(self, dataset: str | None = None, file: str | None = None,
              user_id: str | None = None, limit: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM audit WHERE 1=1", []
        for col, v in (("dataset", dataset), ("file", file), ("user_id", user_id)):
            if v is not None:
                q += f" AND {col}=?"
                args.append(v)
        q += " ORDER BY id DESC"
        if limit:
            q += f" LIMIT {int(limit)}"
        with self.conn() as c:
            return [dict(r) for r in c.execute(q, args)]
