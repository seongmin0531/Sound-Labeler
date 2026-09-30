"""Configuration. The detector defaults were tuned on synthetic cabin recordings and are fixed
(not exposed in the UI); their digest is written into every export for reproducibility."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


@dataclass
class DetectorConfig:
    # Search band for the beep tone (Hz). Typical vehicle chimes/warnings sit in 500-5000 Hz.
    fmin_hz: float = 400.0
    fmax_hz: float = 8000.0
    # STFT analysis
    win_ms: float = 40.0
    hop_ms: float = 5.0
    smooth_time_ms: float = 20.0
    smooth_freq_bins: int = 3
    # Per-bin background = this percentile of the smoothed power over time (dB).
    noise_percentile: float = 25.0
    prominence_halfwidth_hz: float = 400.0
    # "peak": max over band of (power - per-bin background), dB.
    # "prominence": peak minus the median of the whitened spectrum within
    #   +-prominence_halfwidth_hz of the peak (main lobe excluded), dB.
    #   Narrowband tones have high prominence; broadband bursts (door slams, bumps) do not.
    # Hysteresis: an event must contain at least one frame above the *_high thresholds
    # and extends while frames stay above the *_low thresholds.
    peak_db_high: float = 15.0
    peak_db_low: float = 12.0
    prominence_db_high: float = 15.0
    prominence_db_low: float = 10.0
    # Post-processing
    min_event_ms: float = 30.0
    merge_gap_ms: float = 20.0
    # Boundary refinement on the narrowband analytic envelope (sample resolution)
    refine: bool = True
    refine_drop_db: float = 15.0
    refine_min_bandwidth_hz: float = 150.0
    refine_rel_bandwidth: float = 0.08

    # Parameters that change the STFT features (used for caching during tuning).
    FEATURE_KEYS = ("fmin_hz", "fmax_hz", "win_ms", "hop_ms", "smooth_time_ms",
                    "smooth_freq_bins", "noise_percentile", "prominence_halfwidth_hz")


@dataclass
class FrameConfig:
    # Region boundaries snap to this grid (the finest labeling unit).
    frame_ms: float = 100.0
    # Keep the trailing frame shorter than frame_ms so a region can reach the end of the file.
    include_partial: bool = True


@dataclass
class Config:
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    frames: FrameConfig = field(default_factory=FrameConfig)

    def to_dict(self) -> dict:
        return {"detector": asdict(self.detector), "frames": asdict(self.frames)}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Config":
        d = d or {}
        return cls(detector=_build(DetectorConfig, d.get("detector", {})),
                   frames=_build(FrameConfig, d.get("frames", {})))

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def digest(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def _build(klass, d: dict):
    names = {f.name: f.type for f in fields(klass)}
    unknown = set(d) - set(names)
    if unknown:
        raise ValueError(f"Unknown {klass.__name__} keys: {sorted(unknown)}")
    defaults = klass()
    kwargs = {}
    for k, v in d.items():
        kwargs[k] = type(getattr(defaults, k))(v)
    return klass(**kwargs)
