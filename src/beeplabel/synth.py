"""Synthetic vehicle-cabin recordings with known beep boundaries (for tests and sanity checks)."""

from __future__ import annotations

import numpy as np


def cabin_noise(n: int, sr: int, rng: np.random.Generator) -> np.ndarray:
    # Road/engine rumble: strong low-frequency energy, 1/f-ish roll-off, weak broadband hiss.
    X = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1.0 / sr)
    shape = 6.0 / np.sqrt(1 + (f / 300.0) ** 4) + 1.0 / np.sqrt(1 + (f / 3000.0) ** 2) + 0.3
    x = np.fft.irfft(X * shape, n)
    return x / np.sqrt(np.mean(x ** 2))


def tone(n: int, sr: int, f0: float, ramp_ms: float = 5.0, harmonics: int = 3) -> np.ndarray:
    t = np.arange(n) / sr
    x = sum((0.5 ** k) * np.sin(2 * np.pi * f0 * (k + 1) * t) for k in range(harmonics))
    r = min(n // 2, int(sr * ramp_ms / 1000))
    if r:
        w = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, r))
        x[:r] *= w
        x[-r:] *= w[::-1]
    return x / np.sqrt(np.mean(x ** 2))


def make_clip(sr=44100, dur=4.0, snr_db=10.0, seed=0, f0=2000.0, n_beeps=5,
              beep_ms=(60, 300), impulses=3):
    """Returns (signal, [(start, end), ...]). SNR is beep RMS vs noise RMS over the whole band."""
    rng = np.random.default_rng(seed)
    n = int(sr * dur)
    y = cabin_noise(n, sr, rng) * 0.05
    events = []
    t = int(0.2 * sr)
    for _ in range(n_beeps):
        L = int(sr * rng.uniform(*beep_ms) / 1000)
        gap = int(sr * rng.uniform(0.08, 0.4))
        if t + L >= n - int(0.1 * sr):
            break
        amp = 0.05 * 10 ** (snr_db / 20)
        y[t:t + L] += amp * tone(L, sr, f0 * rng.uniform(0.97, 1.03))
        events.append((t, t + L))
        t += L + gap
    for _ in range(impulses):  # door slam / bump: broadband decaying burst
        p = int(rng.uniform(0.1, dur - 0.2) * sr)
        L = int(0.08 * sr)
        burst = rng.standard_normal(L) * np.exp(-np.arange(L) / (0.015 * sr)) * 0.5
        y[p:p + L] += burst
    return y, events
