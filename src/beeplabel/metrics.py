"""Frame- and event-level metrics for detector evaluation and inter-annotator agreement."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .detector import Event


@dataclass
class FrameStats:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    def add(self, ref: np.ndarray, hyp: np.ndarray) -> None:
        ref = np.asarray(ref, bool)
        hyp = np.asarray(hyp, bool)
        if ref.shape != hyp.shape:
            raise ValueError(f"Frame count mismatch: {ref.shape} vs {hyp.shape}")
        self.tp += int((ref & hyp).sum())
        self.fp += int((~ref & hyp).sum())
        self.fn += int((ref & ~hyp).sum())
        self.tn += int((~ref & ~hyp).sum())

    def summary(self) -> dict:
        tp, fp, fn, tn = self.tp, self.fp, self.fn, self.tn
        n = tp + fp + fn + tn
        p = tp / (tp + fp) if tp + fp else float("nan")
        r = tp / (tp + fn) if tp + fn else float("nan")
        f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else float("nan")
        acc = (tp + tn) / n if n else float("nan")
        # Cohen's kappa
        pe = (((tp + fp) * (tp + fn)) + ((fn + tn) * (fp + tn))) / (n * n) if n else float("nan")
        kappa = (acc - pe) / (1 - pe) if n and pe != 1 else float("nan")
        return {"frames": n, "tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": p,
                "recall": r, "f1": f1, "accuracy": acc, "cohen_kappa": kappa}


@dataclass
class EventStats:
    """Event-based matching in the style of sed_eval: onset within collar, offset within
    max(collar, offset_rel * reference duration). One-to-one greedy matching by onset error."""

    collar_s: float = 0.05
    offset_rel: float = 0.2
    tp: int = 0
    fp: int = 0
    fn: int = 0
    onset_err_ms: list = field(default_factory=list)
    offset_err_ms: list = field(default_factory=list)

    def add(self, ref: list[Event], hyp: list[Event], sr: int) -> None:
        collar = self.collar_s * sr
        pairs = []
        for i, r in enumerate(ref):
            off_tol = max(collar, self.offset_rel * (r.end - r.start))
            for j, h in enumerate(hyp):
                do, df = abs(h.start - r.start), abs(h.end - r.end)
                if do <= collar and df <= off_tol:
                    pairs.append((do, i, j, h.start - r.start, h.end - r.end))
        used_r, used_h = set(), set()
        for _, i, j, so, sf in sorted(pairs):
            if i in used_r or j in used_h:
                continue
            used_r.add(i)
            used_h.add(j)
            self.onset_err_ms.append(1000 * so / sr)
            self.offset_err_ms.append(1000 * sf / sr)
        self.tp += len(used_r)
        self.fn += len(ref) - len(used_r)
        self.fp += len(hyp) - len(used_h)

    def summary(self) -> dict:
        tp, fp, fn = self.tp, self.fp, self.fn
        p = tp / (tp + fp) if tp + fp else float("nan")
        r = tp / (tp + fn) if tp + fn else float("nan")
        f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else float("nan")
        on = np.abs(self.onset_err_ms) if self.onset_err_ms else np.array([np.nan])
        off = np.abs(self.offset_err_ms) if self.offset_err_ms else np.array([np.nan])
        return {"collar_ms": 1000 * self.collar_s, "tp": tp, "fp": fp, "fn": fn,
                "precision": p, "recall": r, "f1": f1,
                "onset_mae_ms": float(np.mean(on)), "onset_median_ms": float(np.median(on)),
                "offset_mae_ms": float(np.mean(off)), "offset_median_ms": float(np.median(off))}


def fmt(d: dict) -> str:
    parts = []
    for k, v in d.items():
        parts.append(f"{k}={v:.4f}" if isinstance(v, float) else f"{k}={v}")
    return "  ".join(parts)
