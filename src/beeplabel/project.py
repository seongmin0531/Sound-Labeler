"""Application state shared by the server and the CLI: the DB plus WAV folders.

Every id has its own WAV folder (a local folder or a NAS path such as \\\\NAS\\beep or Z:\\beep),
remembered in the DB and reopened on the next login. A server-wide default folder
(``beeplabel serve --dataset``) is used for ids that have not chosen one yet.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from . import regions
from .audio import AudioInfo, audio_info, frame_bounds, load_audio, scan_dataset, sha256_file, to_mono
from .config import Config
from .db import DB
from .detector import detect

DEFAULT_DATA_DIR = Path(os.environ.get("BEEPLABEL_DATA", Path.home() / "beeplabel_data"))


def normalize_path(path: str | Path) -> Path:
    p = Path(str(path).strip().strip('"')).expanduser()
    try:
        return p.resolve()
    except OSError:  # unreachable network share
        return p


class Dataset:
    """One WAV folder: file list, cached audio info/hash, detector suggestions."""

    def __init__(self, app: "App", path: Path):
        self.app, self.db, self.config = app, app.db, app.config
        self.dataset = path
        self._lock = threading.Lock()
        self.rescan()

    @property
    def dataset_key(self) -> str:
        return str(self.dataset)

    def rescan(self) -> None:
        with self._lock:
            self.files = scan_dataset(self.dataset)
            self._info: dict[str, AudioInfo] = {}
            self._sha: dict[str, str] = {}

    def info(self, rel: str) -> AudioInfo:
        if rel not in self._info:
            self._info[rel] = audio_info(self.dataset, rel)
        return self._info[rel]

    def sha256(self, rel: str) -> str:
        if rel not in self._sha:
            self._sha[rel] = sha256_file(self.dataset / rel)
        return self._sha[rel]

    def load(self, rel: str):
        return load_audio(self.dataset / rel)

    def bounds(self, rel: str):
        i = self.info(rel)
        fc = self.config.frames
        return frame_bounds(i.num_samples, i.sample_rate, fc.frame_ms, fc.include_partial)

    def auto_region(self, rel: str) -> dict:
        """Detector's suggested region (first beep onset, <= 100 ms), cached per file content."""
        sha, digest = self.sha256(rel), self.config.digest()
        cached = self.db.get_auto(self.dataset_key, rel, sha, digest)
        if cached:
            return cached
        data, sr = self.load(rel)
        events, _ = detect(to_mono(data), sr, self.config.detector)
        s, e = regions.suggest(events, len(data), sr)
        ss, es = regions.seconds(sr, s, e)
        self.db.put_auto(self.dataset_key, rel, sha, digest, s, e, ss, es)
        return {"start_sample": s, "end_sample": e, "start_s": ss, "end_s": es}


class App:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR).resolve()
        self.db = DB(self.data_dir / "beeplabel.db")
        self.config = Config()  # fixed, tuned defaults (not user-adjustable)
        self._datasets: dict[str, Dataset] = {}
        self._lock = threading.Lock()

    def open(self, path: str | Path) -> Dataset:
        p = normalize_path(path)
        if not p.is_dir():
            raise FileNotFoundError(f"폴더를 찾을 수 없습니다 (NAS라면 연결 상태를 확인하세요): {p}")
        with self._lock:
            ds = self._datasets.get(str(p))
            if ds is None:
                ds = self._datasets[str(p)] = Dataset(self, p)
        return ds

    # ---------- per-id folder ----------
    @property
    def default_path(self) -> str:
        return self.db.get_setting("dataset_path") or ""

    def set_default(self, path: str | Path) -> Dataset:
        ds = self.open(path)
        self.db.set_setting("dataset_path", ds.dataset_key)
        return ds

    def path_for(self, user_id: str) -> str:
        u = self.db.get_user(user_id) or {}
        return u.get("dataset_path") or self.default_path

    def dataset_for(self, user_id: str) -> Dataset | None:
        path = self.path_for(user_id)
        if not path:
            return None
        try:
            return self.open(path)
        except (FileNotFoundError, OSError):
            return None

    def set_user_dataset(self, user_id: str, path: str | Path) -> Dataset:
        ds = self.open(path)
        self.db.set_prefs(user_id, dataset_path=ds.dataset_key)
        return ds
