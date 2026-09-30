"""One beep region per sample: a free-position span of at most 100 ms.

A region is [start, end) in sample indices with 0 < end - start <= 100 ms. The audio is
never cut: the region only marks where the beep is. None means "no beep in this sample".
Exports also give the 100 ms window centred on the region (the clip to cut later).
"""

from __future__ import annotations

import numpy as np

from .detector import Event

MAX_MS = 100.0


def max_len(sr: int) -> int:
    return int(round(sr * MAX_MS / 1000))


def validate(start, end, n: int, sr: int) -> tuple[int, int] | tuple[None, None]:
    if start is None or end is None:
        return None, None
    s, e = int(start), int(end)
    if not (0 <= s < e <= n):
        raise ValueError(f"구간이 파일 범위를 벗어났습니다: [{s}, {e}) / {n}")
    if e - s > max_len(sr):
        raise ValueError(f"구간은 최대 {MAX_MS:g} ms 입니다 (현재 {1000 * (e - s) / sr:.1f} ms)")
    return s, e


def seconds(sr: int, s, e) -> tuple[float | None, float | None]:
    if s is None:
        return None, None
    return round(s / sr, 6), round(e / sr, 6)


def centered(center: int, n: int, sr: int) -> tuple[int, int]:
    """100 ms window centred on `center` (+-50 ms), shifted to stay inside the file."""
    L = min(max_len(sr), n)
    s = int(center) - L // 2
    s = max(0, min(n - L, s))
    return s, s + L


def suggest(events: list[Event], n: int, sr: int) -> tuple[int, int] | tuple[None, None]:
    """First detected beep: from its onset, at most 100 ms long."""
    if not events:
        return None, None
    ev = min(events, key=lambda e: e.start)
    s = max(0, ev.start)
    return s, min(ev.end, s + max_len(sr), n)


def clip_window(s, e, n: int, sr: int) -> tuple[int, int] | tuple[None, None]:
    if s is None:
        return None, None
    return centered((s + e) // 2, n, sr)


def frame_overlap(bounds: np.ndarray, s, e) -> np.ndarray:
    """Samples of [s, e) inside each 100 ms grid frame."""
    if s is None:
        return np.zeros(len(bounds), dtype=np.int64)
    return np.clip(np.minimum(bounds[:, 1], e) - np.maximum(bounds[:, 0], s), 0, None)
