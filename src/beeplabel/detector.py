"""Automatic beep (narrowband tonal event) detection.

Pipeline
1. STFT power spectrum, smoothed over time and frequency.
2. Whitening: subtract each frequency bin's background level (a low percentile over time).
   This makes the detector robust to coloured cabin noise (engine, road, HVAC).
3. Per analysis frame, inside [fmin, fmax]:
     peak       = max_f  whitened(f)                (how far above background the tone rises)
     prominence = peak - median_f whitened(f)       (narrowband vs broadband)
4. Hysteresis thresholding -> coarse events, min-duration / gap merging.
5. Boundary refinement: zero-phase band-pass analytic envelope around the tone frequency,
   onset/offset where the envelope crosses (event level - refine_drop_db). Sample resolution.

The output is a suggestion. For publication-grade ground truth every file must be
reviewed by a human in the UI; use `beeplabel evaluate` to report detector accuracy
against the reviewed labels.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .config import DetectorConfig

EPS = 1e-12


# numpy-only DSP (no scipy: its compiled modules are blocked by some Windows app-control policies)

def smooth(x: np.ndarray, k: int, axis: int = 0) -> np.ndarray:
    """Centred moving average with edge replication (like scipy uniform_filter1d, mode='nearest')."""
    if k <= 1 or x.shape[axis] == 0:
        return x
    x = np.moveaxis(np.asarray(x, dtype=np.float64), axis, 0)
    left, right = k // 2, k - 1 - k // 2
    xp = np.concatenate([np.repeat(x[:1], left, 0), x, np.repeat(x[-1:], right, 0)])
    c = np.cumsum(xp, axis=0)
    c = np.concatenate([np.zeros_like(c[:1]), c])
    return np.moveaxis((c[k:] - c[:-k]) / k, 0, axis)


def band_envelope(x: np.ndarray, sr: int, lo: float, hi: float, taper_hz: float = 100.0) -> np.ndarray:
    """Amplitude envelope of the zero-phase band-limited analytic signal in [lo, hi] Hz."""
    n = len(x)
    nfft = 1 << int(np.ceil(np.log2(n + 4096)))
    X = np.fft.fft(x, nfft)
    f = np.fft.fftfreq(nfft, 1.0 / sr)
    mask = np.zeros(nfft)
    pos = f > 0
    fp = f[pos]
    m = np.where((fp >= lo) & (fp <= hi), 1.0, 0.0)
    below = (fp < lo) & (fp > lo - taper_hz)
    above = (fp > hi) & (fp < hi + taper_hz)
    m[below] = 0.5 + 0.5 * np.cos(np.pi * (lo - fp[below]) / taper_hz)
    m[above] = 0.5 + 0.5 * np.cos(np.pi * (fp[above] - hi) / taper_hz)
    mask[pos] = 2.0 * m
    return np.abs(np.fft.ifft(X * mask)[:n])


@dataclass
class Features:
    sample_rate: int
    num_samples: int
    hop: int
    n_fft: int
    centers: np.ndarray      # sample index at the centre of each analysis frame
    peak_db: np.ndarray
    prominence_db: np.ndarray
    peak_hz: np.ndarray


@dataclass
class Event:
    start: int               # first sample (inclusive)
    end: int                 # last sample (exclusive)
    f0_hz: float | None = None
    source: str = "auto"

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["f0_hz"] is not None:
            d["f0_hz"] = round(float(d["f0_hz"]), 1)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        return cls(int(d["start"]), int(d["end"]), d.get("f0_hz"), d.get("source", "manual"))


def compute_features(y: np.ndarray, sr: int, cfg: DetectorConfig) -> Features:
    y = np.asarray(y, dtype=np.float64)
    n = len(y)
    win = max(16, int(round(sr * cfg.win_ms / 1000)))
    n_fft = 1 << int(np.ceil(np.log2(win)))
    hop = max(1, int(round(sr * cfg.hop_ms / 1000)))

    # Centred frames: frame k covers [k*hop - win/2, k*hop + win/2).
    n_frames = 1 + (n - 1) // hop if n > 0 else 0
    pad = win // 2
    ypad = np.concatenate([np.zeros(pad), y, np.zeros(win + hop)])
    frames = sliding_window_view(ypad, win)[::hop][:n_frames]
    window = np.hanning(win)
    spec = np.abs(np.fft.rfft(frames * window, n=n_fft, axis=1)) ** 2  # [T, F]

    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    fmax = min(cfg.fmax_hz, 0.98 * sr / 2)
    band = (freqs >= cfg.fmin_hz) & (freqs <= fmax)
    if band.sum() < 3:
        raise ValueError(f"Band {cfg.fmin_hz}-{fmax} Hz is too narrow for sr={sr}")
    spec = spec[:, band]
    bfreqs = freqs[band]

    t_smooth = max(1, int(round(cfg.smooth_time_ms / cfg.hop_ms)))
    if t_smooth > 1:
        spec = smooth(spec, t_smooth, axis=0)
    if cfg.smooth_freq_bins > 1:
        spec = smooth(spec, cfg.smooth_freq_bins, axis=1)

    sdb = 10 * np.log10(spec + EPS)
    background = np.percentile(sdb, cfg.noise_percentile, axis=0)
    white = sdb - background[None, :]
    ipk = np.argmax(white, axis=1)
    rows = np.arange(len(white))
    peak = white[rows, ipk]

    # Local prominence: peak vs the median of its spectral neighbourhood, excluding the
    # main lobe. Broadband bursts lift the neighbourhood too, so they score low.
    df = sr / n_fft
    guard = int(np.ceil(2 * n_fft / win)) + cfg.smooth_freq_bins // 2 + 1
    half = max(guard + 4, int(round(cfg.prominence_halfwidth_hz / df)))
    offs = np.concatenate([np.arange(-half, -guard), np.arange(guard + 1, half + 1)])
    nb = white.shape[1]
    idx = ipk[:, None] + offs[None, :]
    valid = (idx >= 0) & (idx < nb)
    neigh = np.where(valid, white[rows[:, None], np.clip(idx, 0, nb - 1)], np.nan)
    prom = peak - np.nanmedian(neigh, axis=1)

    return Features(sr, n, hop, n_fft, np.arange(n_frames) * hop,
                    peak.astype(np.float32), prom.astype(np.float32),
                    bfreqs[ipk].astype(np.float32))


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index runs where mask is True."""
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def _merge(events: list[Event], gap: int) -> list[Event]:
    events = sorted(events, key=lambda e: e.start)
    out: list[Event] = []
    for e in events:
        if out and e.start - out[-1].end <= gap:
            prev = out[-1]
            f0s = [f for f in (prev.f0_hz, e.f0_hz) if f is not None]
            out[-1] = Event(prev.start, max(prev.end, e.end),
                            float(np.mean(f0s)) if f0s else None, prev.source)
        else:
            out.append(Event(e.start, e.end, e.f0_hz, e.source))
    return out


def _refine(y: np.ndarray, sr: int, ev: Event, cfg: DetectorConfig, search: int) -> Event:
    if ev.f0_hz is None:
        return ev
    n = len(y)
    bw = max(cfg.refine_min_bandwidth_hz, cfg.refine_rel_bandwidth * ev.f0_hz)
    lo, hi = ev.f0_hz - bw / 2, ev.f0_hz + bw / 2
    nyq = sr / 2
    if lo <= 20 or hi >= 0.99 * nyq:
        return ev
    a = max(0, ev.start - search)
    b = min(n, ev.end + search)
    # Background taken from a wider context so it is not dominated by the event itself.
    ctx_a = max(0, a - int(0.5 * sr))
    ctx_b = min(n, b + int(0.5 * sr))
    seg = y[ctx_a:ctx_b]
    if len(seg) < 64:
        return ev
    env = band_envelope(seg, sr, lo, hi, taper_hz=bw / 2)
    env = smooth(env, max(1, int(sr * 0.001)))
    core_a, core_b = ev.start - ctx_a, ev.end - ctx_a
    core = env[core_a:core_b]
    if len(core) == 0:
        return ev
    level = np.median(core)
    outside = np.concatenate([env[: max(0, core_a - search // 2)], env[core_b + search // 2:]])
    noise = np.percentile(outside, 20) if len(outside) > sr * 0.02 else 0.0
    thr = level * 10 ** (-cfg.refine_drop_db / 20)
    thr = max(thr, 2.0 * noise)
    if thr >= level:
        return ev
    above = env >= thr
    lo_lim, hi_lim = a - ctx_a, b - ctx_a
    # Anchor at the strongest point of the core and grow outward.
    anchor = core_a + int(np.argmax(core))
    if not above[anchor]:
        return ev
    s = anchor
    while s > lo_lim and above[s - 1]:
        s -= 1
    e = anchor
    while e < hi_lim and above[e]:
        e += 1
    return Event(int(s + ctx_a), int(e + ctx_a), ev.f0_hz, ev.source)


def detect_from_features(y: np.ndarray, feats: Features, cfg: DetectorConfig) -> list[Event]:
    sr, hop, n = feats.sample_rate, feats.hop, feats.num_samples
    strong = (feats.peak_db >= cfg.peak_db_high) & (feats.prominence_db >= cfg.prominence_db_high)
    weak = (feats.peak_db >= cfg.peak_db_low) & (feats.prominence_db >= cfg.prominence_db_low)

    events: list[Event] = []
    for a, b in _runs(weak):
        if not strong[a:b].any():
            continue
        f0 = float(np.median(feats.peak_hz[a:b][strong[a:b]]))
        start = max(0, int(feats.centers[a]) - hop // 2)
        end = min(n, int(feats.centers[b - 1]) + hop - hop // 2)
        events.append(Event(start, end, f0))

    gap = int(round(sr * cfg.merge_gap_ms / 1000))
    events = _merge(events, gap)
    if cfg.refine and len(y):
        search = int(round(sr * cfg.win_ms / 1000))
        events = _merge([_refine(y, sr, e, cfg, search) for e in events], gap)
    min_len = int(round(sr * cfg.min_event_ms / 1000))
    return [e for e in events if e.end - e.start >= min_len]


def detect(y: np.ndarray, sr: int, cfg: DetectorConfig) -> tuple[list[Event], Features]:
    feats = compute_features(y, sr, cfg)
    return detect_from_features(y, feats, cfg), feats
