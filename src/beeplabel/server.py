"""Local/LAN web server for the labeling UI (FastAPI). Login is by id only (shared team use)."""

from __future__ import annotations

import base64
import io
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import numpy as np
import soundfile as sf
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, regions
from . import export as ex
from .audio import to_mono
from .db import region_text
from .project import App

STATIC = Path(__file__).parent / "static"
COOKIE = "beeplabel_session"
ID_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,32}$")


def _spectrogram(y: np.ndarray, sr: int, max_cols: int = 4000, fmax: float = 8000.0) -> dict:
    n_fft = 1024 if sr >= 32000 else 512
    hop = max(n_fft // 8, int(np.ceil(len(y) / max_cols)))
    n_cols = max(1, 1 + (len(y) - 1) // hop)
    ypad = np.concatenate([np.zeros(n_fft // 2), y, np.zeros(n_fft + hop)])
    idx = np.arange(n_cols)[:, None] * hop + np.arange(n_fft)[None, :]
    S = np.abs(np.fft.rfft(ypad[idx] * np.hanning(n_fft), axis=1)) ** 2
    freqs = np.fft.rfftfreq(n_fft, 1 / sr)
    keep = freqs <= min(fmax, sr / 2)
    db = 10 * np.log10(S[:, keep] + 1e-12)
    top = np.percentile(db, 99.9)
    img = np.clip((db - (top - 80)) / 80 * 255, 0, 255).astype(np.uint8)
    return {"data": base64.b64encode(img.tobytes()).decode(), "n_time": int(img.shape[0]),
            "n_freq": int(img.shape[1]), "fmax": float(freqs[keep][-1]), "hop": int(hop)}


def create_app(app_state: App) -> FastAPI:
    A = app_state
    db = A.db
    api = FastAPI(title="beeplabel", version=__version__)

    # ---------- auth helpers ----------
    def current_user(request: Request) -> dict:
        u = db.session_user(request.cookies.get(COOKIE))
        if not u:
            raise HTTPException(401, "로그인이 필요합니다")
        return u

    def need_dataset():
        if not A.dataset:
            raise HTTPException(409, "WAV 데이터 폴더가 지정되지 않았습니다. 설정에서 경로를 지정하세요.")

    def rel(idx: int) -> str:
        need_dataset()
        if not 0 <= idx < len(A.files):
            raise HTTPException(404, "파일 번호가 범위를 벗어났습니다. 목록을 새로고침하세요.")
        return A.files[idx]

    def settings_payload() -> dict:
        return {"dataset_path": A.dataset_key, "n_files": len(A.files),
                "max_region_ms": regions.MAX_MS, "data_dir": str(A.data_dir)}

    # ---------- public ----------
    @api.get("/api/public")
    def public():
        return {"version": __version__, "users": [u["id"] for u in db.users()]}

    def _start_session(response: Response, uid: str) -> dict:
        token = db.new_session(uid)
        db.log(uid, "로그인")
        response.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
        return db.get_user(uid)

    @api.post("/api/auth/login")
    def login(response: Response, body: dict = Body(...)):
        """Id-only login. Unknown ids get 404 so the UI can confirm creating a new id
        (protects against typos splitting one person's work across two ids)."""
        uid = (body.get("user_id") or "").strip()
        if not ID_RE.match(uid):
            raise HTTPException(400, "아이디는 영문/숫자/._- 2~32자로 입력하세요.")
        if not db.get_user(uid):
            if not body.get("create"):
                raise HTTPException(404, "처음 보는 아이디입니다.")
            name = (body.get("display_name") or "").strip()[:40]
            db.create_user(uid, name)
            db.log(uid, "아이디 생성", detail=name or None)
        return _start_session(response, uid)

    @api.post("/api/auth/logout")
    def logout(request: Request, response: Response, body: dict = Body(default={}),
               u: dict = Depends(current_user)):
        db.log(u["id"], "저장 후 로그아웃" if body.get("saved") else "로그아웃")
        db.end_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @api.get("/api/me")
    def me(u: dict = Depends(current_user)):
        return {"user": u, "settings": settings_payload(), "version": __version__}

    @api.put("/api/me/prefs")
    def prefs(body: dict = Body(...), u: dict = Depends(current_user)):
        theme = body.get("theme")
        if theme is not None and theme not in ("dark", "light"):
            raise HTTPException(400, "theme")
        db.set_prefs(u["id"], **{k: v for k, v in body.items() if k in ("theme", "last_file")})
        return db.get_user(u["id"])

    # ---------- settings (shared by everyone) ----------
    @api.get("/api/settings")
    def get_settings(u: dict = Depends(current_user)):
        return settings_payload()

    @api.put("/api/settings")
    def put_settings(body: dict = Body(...), u: dict = Depends(current_user)):
        if "dataset_path" in body:
            old = A.dataset_key
            try:
                A.set_dataset(body["dataset_path"].strip().strip('"'))
            except (FileNotFoundError, OSError) as e:
                raise HTTPException(400, str(e))
            if old != A.dataset_key:
                db.log(u["id"], "데이터 폴더 변경", before=old, after=A.dataset_key,
                       detail=f"WAV {len(A.files)}개")
        return settings_payload()

    @api.post("/api/rescan")
    def rescan(u: dict = Depends(current_user)):
        need_dataset()
        A.rescan()
        return settings_payload()

    # ---------- files ----------
    @api.get("/api/files")
    def files(u: dict = Depends(current_user)):
        if not A.dataset:
            return []
        mine = {l["file"]: l for l in db.labels(dataset=A.dataset_key, user_id=u["id"])}
        counts: dict[str, int] = {}
        for l in db.labels(dataset=A.dataset_key):
            counts[l["file"]] = counts.get(l["file"], 0) + 1
        out = []
        for i, r in enumerate(A.files):
            info = A.info(r)
            l = mine.get(r)
            out.append({"idx": i, "file": r, "duration": round(info.duration, 3),
                        "status": ("reviewed" if l["reviewed"] else "draft") if l else "none",
                        "region": region_text(l["start_s"], l["end_s"]) if l else "",
                        "n_users": counts.get(r, 0)})
        return out

    @api.get("/api/files/{idx}/view")
    def view(idx: int, u: dict = Depends(current_user)):
        r = rel(idx)
        data, sr = A.load(r)
        y = to_mono(data)
        b = A.bounds(r)
        env_step = max(1, int(np.ceil(len(y) / 20000)))
        n = len(y) // env_step * env_step
        blocks = y[:n].reshape(-1, env_step) if n else np.zeros((0, 1))
        return {"file": r, "sr": sr, "num_samples": len(y), "channels": data.shape[1],
                "frame_starts": b[:, 0].tolist(), "frame_ends": b[:, 1].tolist(),
                "frame_ms": A.config.frames.frame_ms, "max_region_ms": regions.MAX_MS,
                "auto": A.auto_region(r),
                "wave": {"step": env_step,
                         "min": np.round(blocks.min(axis=1), 4).tolist() if n else [],
                         "max": np.round(blocks.max(axis=1), 4).tolist() if n else []},
                "spec": _spectrogram(y, sr)}

    @api.get("/api/files/{idx}/audio")
    def audio(idx: int, u: dict = Depends(current_user)):
        data, sr = A.load(rel(idx))
        buf = io.BytesIO()
        sf.write(buf, np.clip(data, -1, 1), sr, format="WAV", subtype="PCM_16")
        return Response(buf.getvalue(), media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @api.get("/api/files/{idx}/label")
    def get_label(idx: int, u: dict = Depends(current_user)):
        return {"label": db.get_label(A.dataset_key, rel(idx), u["id"])}

    @api.put("/api/files/{idx}/label")
    def put_label(idx: int, body: dict = Body(...), u: dict = Depends(current_user)):
        r = rel(idx)
        if body.get("file") != r:
            raise HTTPException(409, "파일 목록이 바뀌었습니다. 새로고침하세요.")
        info = A.info(r)
        try:
            st, en = regions.validate(body.get("start_sample"), body.get("end_sample"),
                                      info.num_samples, info.sample_rate)
        except ValueError as err:
            raise HTTPException(400, str(err))
        ss, es = regions.seconds(info.sample_rate, st, en)
        label, actions = db.save_label(
            A.dataset_key, r, u["id"], start_sample=st, end_sample=en, start_s=ss, end_s=es,
            reviewed=bool(body.get("reviewed")), memo=str(body.get("memo") or "")[:2000],
            sample_rate=info.sample_rate, num_samples=info.num_samples, sha256=A.sha256(r))
        db.set_prefs(u["id"], last_file=r)
        return {"label": label, "actions": actions}

    @api.get("/api/files/{idx}/history")
    def history(idx: int, u: dict = Depends(current_user)):
        r = rel(idx)
        others = [l for l in db.labels(dataset=A.dataset_key) if l["file"] == r]
        return {"history": db.audit(dataset=A.dataset_key, file=r, limit=200),
                "labels": [{"user_id": l["user_id"], "region": region_text(l["start_s"], l["end_s"]),
                            "reviewed": l["reviewed"], "memo": l["memo"], "updated_at": l["updated_at"]}
                           for l in others]}

    # ---------- export ----------
    @api.get("/api/export/{kind}")
    def export(kind: str, reviewed_only: bool = False, u: dict = Depends(current_user)):
        need_dataset()
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        data = ex.collect(A, reviewed_only=reviewed_only, with_auto=kind in ("xlsx", "labels.csv"))
        if kind == "xlsx":
            body, name, mt = ex.excel(A, data, u["id"]), f"beep_labels_{stamp}.xlsx", \
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif kind == "labels.csv":
            body, name, mt = ex.labels_csv(data), f"beep_labels_{stamp}.csv", "text/csv"
        elif kind == "frames.csv":
            body, name, mt = ex.frames_csv(A, data), f"beep_frames_{stamp}.csv", "text/csv"
        elif kind == "audit.csv":
            body, name, mt = ex.audit_csv(data), f"beep_audit_{stamp}.csv", "text/csv"
        else:
            raise HTTPException(404, "unknown export")
        db.log(u["id"], "내보내기", detail=f"{name} (검수 완료만: {'예' if reviewed_only else '아니오'})")
        return Response(body, media_type=mt, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    api.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return api
