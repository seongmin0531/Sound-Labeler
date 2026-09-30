"""SQLite storage: accounts, sessions, settings, per-user labels and a full audit log.

Everything is in one file (default: ~/beeplabel_data/beeplabel.db) so it survives restarts,
logouts and re-logins, and can be backed up by copying a single file.
"""

from __future__ import annotations

import hashlib
import hmac
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
    pw_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
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
    start_frame INTEGER,          -- NULL = no beep in this sample
    end_frame INTEGER,            -- exclusive
    start_s REAL,
    end_s REAL,
    reviewed INTEGER NOT NULL DEFAULT 0,
    memo TEXT NOT NULL DEFAULT '',
    sample_rate INTEGER,
    num_samples INTEGER,
    frame_ms REAL,
    sha256 TEXT,
    n_edits INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset, file, user_id)
);
CREATE TABLE IF NOT EXISTS auto_regions (
    dataset TEXT NOT NULL,
    file TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    start_frame INTEGER,
    end_frame INTEGER,
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

PBKDF2_ITER = 200_000


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ITER).hex()


class DB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.conn() as c:
            c.executescript(SCHEMA)

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
    def create_user(self, user_id: str, password: str, display_name: str = "") -> dict:
        salt = secrets.token_hex(16)
        with self.conn() as c:
            first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
            c.execute("INSERT INTO users(id,display_name,pw_hash,salt,is_admin,created_at) "
                      "VALUES(?,?,?,?,?,?)",
                      (user_id, display_name or user_id, _hash_pw(password, salt), salt,
                       1 if first else 0, now()))
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict | None:
        with self.conn() as c:
            r = c.execute("SELECT id,display_name,is_admin,theme,last_file,created_at,last_login "
                          "FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(r) if r else None

    def users(self) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT id,display_name,is_admin,created_at,last_login FROM users ORDER BY id")]

    def check_password(self, user_id: str, password: str) -> bool:
        with self.conn() as c:
            r = c.execute("SELECT pw_hash,salt FROM users WHERE id=?", (user_id,)).fetchone()
        if not r:
            _hash_pw(password, "00" * 16)  # constant-ish time for unknown ids
            return False
        return hmac.compare_digest(r["pw_hash"], _hash_pw(password, r["salt"]))

    def set_password(self, user_id: str, password: str) -> None:
        salt = secrets.token_hex(16)
        with self.conn() as c:
            c.execute("UPDATE users SET pw_hash=?, salt=? WHERE id=?",
                      (_hash_pw(password, salt), salt, user_id))
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

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

    def save_label(self, dataset: str, file: str, user_id: str, *, start_frame, end_frame,
                   start_s, end_s, reviewed: bool, memo: str, sample_rate: int,
                   num_samples: int, frame_ms: float, sha256: str) -> tuple[dict, list[str]]:
        """Upserts the label and writes one audit row per kind of change. Returns (label, actions)."""
        t = now()
        with self.conn() as c:
            old = c.execute("SELECT * FROM labels WHERE dataset=? AND file=? AND user_id=?",
                            (dataset, file, user_id)).fetchone()
            old = dict(old) if old else None
            actions = []
            new_region = (start_frame, end_frame)
            old_region = (old["start_frame"], old["end_frame"]) if old else None
            if old is None:
                actions.append(("구간 지정" if start_frame is not None else "비프 없음 지정",
                                None, region_text(start_s, end_s)))
            elif old_region != new_region:
                if start_frame is None:
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
                c.execute("""INSERT INTO labels(dataset,file,user_id,start_frame,end_frame,start_s,
                             end_s,reviewed,memo,sample_rate,num_samples,frame_ms,sha256,n_edits,
                             created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (dataset, file, user_id, start_frame, end_frame, start_s, end_s,
                           int(reviewed), memo or "", sample_rate, num_samples, frame_ms, sha256,
                           len(actions), t, t))
            elif actions:
                c.execute("""UPDATE labels SET start_frame=?,end_frame=?,start_s=?,end_s=?,
                             reviewed=?,memo=?,sample_rate=?,num_samples=?,frame_ms=?,sha256=?,
                             n_edits=n_edits+?,updated_at=? WHERE dataset=? AND file=? AND user_id=?""",
                          (start_frame, end_frame, start_s, end_s, int(reviewed), memo or "",
                           sample_rate, num_samples, frame_ms, sha256, len(actions), t,
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
            r = c.execute("SELECT * FROM auto_regions WHERE dataset=? AND file=? AND sha256=? "
                          "AND config_digest=?", (dataset, file, sha256, digest)).fetchone()
        return dict(r) if r else None

    def put_auto(self, dataset, file, sha256, digest, start_frame, end_frame, start_s, end_s):
        with self.conn() as c:
            c.execute("INSERT OR REPLACE INTO auto_regions VALUES(?,?,?,?,?,?,?,?)",
                      (dataset, file, sha256, digest, start_frame, end_frame, start_s, end_s))

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


def region_text(start_s, end_s) -> str:
    if start_s is None:
        return "비프 없음"
    return f"{start_s:.1f}~{end_s:.1f}초"
