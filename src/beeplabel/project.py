"""Application state shared by the server and the CLI: DB + current dataset folder."""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path

from . import regions
from .audio import AudioInfo, audio_info, frame_bounds, load_audio, scan_dataset, sha256_file, to_mono
from .config import Config
from .db import DB
from .detector import detect

DEFAULT_DATA_DIR = Path(os.environ.get("BEEPLABEL_DATA", Path.home() / "beeplabel_data"))


class App:
    def __init__(self, data_dir: str | Path | None = None):
        self.data_dir = Path(data_dir or DEFAULT_DATA_DIR).resolve()
        self.db = DB(self.data_dir / "beeplabel.db")
        self.config = Config()  # fixed, tuned defaults (not user-adjustable)
        self._lock = threading.Lock()
        self.dataset: Path | None = None
        self.files: list[str] = []
        saved = self.db.get_setting("dataset_path")
        if saved and Path(saved).is_dir():
            self.set_dataset(saved, persist=False)

    # ---------- dataset ----------
    def set_dataset(self, path: str | Path, persist: bool = True) -> None:
        p = Path(path).expanduser().resolve()
        if not p.is_dir():
            raise FileNotFoundError(f"폴더를 찾을 수 없습니다: {p}")
        with self._lock:
            self.dataset = p
            self.files = scan_dataset(p)
            self._info.cache_clear()
            self._sha.cache_clear()
        if persist:
            self.db.set_setting("dataset_path", str(p))

    def rescan(self) -> None:
        if self.dataset:
            self.set_dataset(self.dataset, persist=False)

    @property
    def dataset_key(self) -> str:
        return str(self.dataset) if self.dataset else ""

    @lru_cache(maxsize=None)
    def _info(self, root: str, rel: str) -> AudioInfo:
        return audio_info(root, rel)

    @lru_cache(maxsize=None)
    def _sha(self, root: str, rel: str) -> str:
        return sha256_file(Path(root) / rel)

    def info(self, rel: str) -> AudioInfo:
        return self._info(self.dataset_key, rel)

    def sha256(self, rel: str) -> str:
        return self._sha(self.dataset_key, rel)

    def load(self, rel: str):
        return load_audio(self.dataset / rel)

    def bounds(self, rel: str):
        i = self.info(rel)
        fc = self.config.frames
        return frame_bounds(i.num_samples, i.sample_rate, fc.frame_ms, fc.include_partial)

    # ---------- automatic suggestion ----------
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
