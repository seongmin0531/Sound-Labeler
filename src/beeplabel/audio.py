"""Audio I/O, dataset scanning and exact sample-level framing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

AUDIO_EXTS = {".wav", ".flac", ".ogg", ".mp3", ".aiff", ".aif"}


@dataclass
class AudioInfo:
    relpath: str
    sample_rate: int
    num_samples: int
    channels: int
    subtype: str

    @property
    def duration(self) -> float:
        return self.num_samples / self.sample_rate


def scan_dataset(root: str | Path) -> list[str]:
    """Sorted POSIX relative paths of all audio files under root (hidden dirs skipped)."""
    root = Path(root)
    out = []
    for p in root.rglob("*"):
        if p.suffix.lower() not in AUDIO_EXTS or not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue
        out.append(rel.as_posix())
    return sorted(out)


def audio_info(root: str | Path, relpath: str) -> AudioInfo:
    i = sf.info(str(Path(root) / relpath))
    return AudioInfo(relpath, int(i.samplerate), int(i.frames), int(i.channels), str(i.subtype))


def load_audio(path: str | Path) -> tuple[np.ndarray, int]:
    """Returns (data[n_samples, channels] float64, sample_rate). No resampling, ever."""
    data, sr = sf.read(str(path), dtype="float64", always_2d=True)
    return data, int(sr)


def to_mono(data: np.ndarray) -> np.ndarray:
    return data.mean(axis=1) if data.ndim == 2 else data


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


def frame_bounds(num_samples: int, sample_rate: int, frame_ms: float,
                 include_partial: bool = True) -> np.ndarray:
    """[n_frames, 2] int64 array of [start, end) sample indices.

    Boundary k is round(k * sr * frame_ms / 1000), so boundaries never drift even when
    sr * frame_ms / 1000 is not an integer (e.g. 11025 Hz). Frame lengths may then
    differ by one sample. The trailing short frame is kept only if include_partial.
    """
    step = sample_rate * frame_ms / 1000.0
    n_full = int(np.floor(num_samples / step + 1e-9))
    k = np.arange(n_full + 1)
    edges = np.rint(k * step).astype(np.int64)
    edges = edges[edges <= num_samples]
    if include_partial and edges[-1] < num_samples:
        edges = np.append(edges, num_samples)
    return np.stack([edges[:-1], edges[1:]], axis=1)


def nominal_frame_len(sample_rate: int, frame_ms: float) -> float:
    return sample_rate * frame_ms / 1000.0
