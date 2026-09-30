"""One beep region per sample, snapped to the 100 ms frame grid.

A region is [start_frame, end_frame) in frame indices; its time range is
[frame_start[start_frame], frame_end[end_frame - 1]) in samples. The audio is never cut:
the region only marks where the beep is. No region (None) means "no beep in this sample".
"""

from __future__ import annotations

import numpy as np

from .detector import Event


def validate(start_frame, end_frame, n_frames: int) -> tuple[int, int] | tuple[None, None]:
    if start_frame is None or end_frame is None:
        return None, None
    s, e = int(start_frame), int(end_frame)
    if not (0 <= s < e <= n_frames):
        raise ValueError(f"invalid region [{s}, {e}) for {n_frames} frames")
    return s, e


def seconds(bounds: np.ndarray, sr: int, s, e) -> tuple[float | None, float | None]:
    if s is None:
        return None, None
    return round(float(bounds[s, 0]) / sr, 6), round(float(bounds[e - 1, 1]) / sr, 6)


def suggest(events: list[Event], bounds: np.ndarray) -> tuple[int, int] | tuple[None, None]:
    """Smallest frame-aligned region covering every detected beep (first onset -> last offset)."""
    if not events or len(bounds) == 0:
        return None, None
    first = min(e.start for e in events)
    last = max(e.end for e in events)
    s = int(np.searchsorted(bounds[:, 1], first, side="right"))       # frame containing onset
    e = int(np.searchsorted(bounds[:, 0], last, side="left"))         # first frame starting at/after offset
    s = min(s, len(bounds) - 1)
    e = max(e, s + 1)
    return s, min(e, len(bounds))


def frame_vector(n_frames: int, s, e) -> np.ndarray:
    v = np.zeros(n_frames, dtype=np.int8)
    if s is not None:
        v[s:e] = 1
    return v
