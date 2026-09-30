import csv
import io

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from beeplabel import regions
from beeplabel.audio import frame_bounds
from beeplabel.config import DetectorConfig
from beeplabel.detector import Event, detect
from beeplabel.metrics import EventStats
from beeplabel.project import App
from beeplabel.server import create_app
from beeplabel.synth import make_clip

SR = 44100


@pytest.mark.parametrize("sr", [8000, 11025, 16000, 22050, 44100, 48000])
def test_frame_bounds_exact(sr):
    n = int(sr * 3.37)
    b = frame_bounds(n, sr, 100)
    assert b[0, 0] == 0 and b[-1, 1] == n
    assert np.all(b[1:, 0] == b[:-1, 1])
    assert np.all(b[:, 0] == np.rint(np.arange(len(b)) * sr / 10).astype(int))


def test_region_suggest_and_seconds():
    b = frame_bounds(SR * 3, SR, 100)
    ev = [Event(int(0.43 * SR), int(0.61 * SR)), Event(int(1.05 * SR), int(1.18 * SR))]
    s, e = regions.suggest(ev, b)
    assert (s, e) == (4, 12)                      # 0.4 s .. 1.2 s, covers both beeps
    assert regions.seconds(b, SR, s, e) == (0.4, 1.2)
    assert regions.suggest([], b) == (None, None)
    assert regions.frame_vector(len(b), s, e).sum() == 8
    with pytest.raises(ValueError):
        regions.validate(5, 5, len(b))


@pytest.mark.parametrize("snr", [10, 0])
def test_detector_accuracy(snr):
    es = EventStats(collar_s=0.02)
    for seed in range(6):
        for f0 in (800, 2000, 3500):
            y, ref = make_clip(snr_db=snr, seed=seed, f0=f0)
            hyp, _ = detect(y, SR, DetectorConfig())
            es.add([Event(s, e) for s, e in ref], hyp, SR)
    s = es.summary()
    assert s["f1"] >= 0.98 and s["onset_mae_ms"] < 3, s


def test_impulses_are_not_beeps():
    y, _ = make_clip(seed=3, n_beeps=0, impulses=8)
    assert detect(y, SR, DetectorConfig())[0] == []


@pytest.fixture
def env(tmp_path):
    ds = tmp_path / "wavs"
    (ds / "a").mkdir(parents=True)
    for i in range(3):
        y, _ = make_clip(snr_db=10, seed=i, dur=2.0, n_beeps=2)
        sf.write(ds / "a" / f"s{i}.wav", y / np.abs(y).max() * 0.9, SR)
    app = App(tmp_path / "data")
    return ds, app, TestClient(create_app(app))


def test_full_flow(env, tmp_path):
    ds, app, c = env
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/public").json()["has_users"] is False

    # first user becomes admin, sets the dataset path
    r = c.post("/api/auth/register", json={"user_id": "hong", "password": "pw1234", "display_name": "홍길동"})
    assert r.status_code == 200 and r.json()["is_admin"] == 1
    assert c.put("/api/settings", json={"dataset_path": str(ds)}).json()["n_files"] == 3
    files = c.get("/api/files").json()
    assert [f["file"] for f in files] == ["a/s0.wav", "a/s1.wav", "a/s2.wav"]

    v = c.get("/api/files/0/view").json()
    assert len(v["frame_starts"]) == 20 and v["auto"]["start_frame"] is not None
    assert c.get("/api/files/0/audio").headers["content-type"] == "audio/wav"

    # label: region 3..9 (0.3~0.9 s), then modify, memo, review
    put = lambda body: c.put("/api/files/0/label", json={"file": "a/s0.wav", **body})
    r = put({"start_frame": 3, "end_frame": 9, "reviewed": False, "memo": ""}).json()
    assert r["label"]["start_s"] == 0.3 and r["label"]["end_s"] == 0.9 and r["actions"] == ["구간 지정"]
    r = put({"start_frame": 2, "end_frame": 9, "reviewed": True, "memo": "비프 2회"}).json()
    assert set(r["actions"]) == {"구간 수정", "메모 작성", "검수 완료"} or set(r["actions"]) == {"구간 수정", "메모 수정", "검수 완료"}
    assert put({"start_frame": 2, "end_frame": 21, "reviewed": True, "memo": ""}).status_code == 400
    # "no beep" on file 1
    c.put("/api/files/1/label", json={"file": "a/s1.wav", "start_frame": None, "end_frame": None,
                                      "reviewed": True, "memo": "조용함"})

    # logout -> login again restores everything
    c.post("/api/auth/logout", json={"saved": True})
    assert c.get("/api/files/0/label").status_code == 401
    assert c.post("/api/auth/login", json={"user_id": "hong", "password": "wrong"}).status_code == 401
    c.post("/api/auth/login", json={"user_id": "hong", "password": "pw1234"})
    me = c.get("/api/me").json()
    assert me["user"]["last_file"] == "a/s1.wav" and me["settings"]["dataset_path"] == str(ds.resolve())
    lab = c.get("/api/files/0/label").json()["label"]
    assert (lab["start_frame"], lab["end_frame"], lab["reviewed"], lab["memo"]) == (2, 9, 1, "비프 2회")

    # second user labels the same file differently
    c.post("/api/auth/logout", json={})
    c.post("/api/auth/register", json={"user_id": "kim", "password": "abcd"})
    assert c.put("/api/settings", json={"dataset_path": "C:/"}).status_code == 403
    c.put("/api/files/0/label", json={"file": "a/s0.wav", "start_frame": 3, "end_frame": 9, "reviewed": True, "memo": ""})
    assert c.get("/api/files/0/label").json()["label"]["start_frame"] == 3
    hist = c.get("/api/files/0/history").json()
    assert {l["user_id"] for l in hist["labels"]} == {"hong", "kim"}

    # Excel export: all sheets, who/what/how
    r = c.get("/api/export/xlsx")
    assert r.status_code == 200
    wb = load_workbook(io.BytesIO(r.content))
    assert wb.sheetnames == ["라벨 결과", "작업 이력", "작업자 요약", "파일 현황", "정보"]
    rows = list(wb["라벨 결과"].values)
    head = rows[0]
    recs = [dict(zip(head, r)) for r in rows[1:]]
    hong0 = next(r for r in recs if r["작업자 ID"] == "hong" and r["파일"] == "a/s0.wav")
    assert hong0["비프 구간"] == "0.2~0.9초" and hong0["메모"] == "비프 2회" and hong0["상태"] == "검수 완료"
    assert next(r for r in recs if r["파일"] == "a/s1.wav")["비프 여부"] == "없음"
    acts = [dict(zip(list(wb["작업 이력"].values)[0], r)) for r in list(wb["작업 이력"].values)[1:]]
    mod = next(a for a in acts if a["동작"] == "구간 수정")
    assert mod["작업자 ID"] == "hong" and mod["변경 전"] == "0.3~0.9초" and mod["변경 후"] == "0.2~0.9초"
    assert any(a["동작"] == "저장 후 로그아웃" for a in acts)
    files_sheet = [dict(zip(list(wb["파일 현황"].values)[0], r)) for r in list(wb["파일 현황"].values)[1:]]
    assert files_sheet[0]["작업자 간 일치"] == "불일치"

    # CSV: seconds range + memo, UTF-8 BOM for Excel
    raw = c.get("/api/export/labels.csv").content
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    r0 = next(r for r in rows if r["user_id"] == "hong" and r["file"] == "a/s0.wav")
    assert (r0["start_s"], r0["end_s"], r0["memo"], r0["end_frame_inclusive"]) == ("0.2", "0.9", "비프 2회", "8")
    frames = list(csv.DictReader(io.StringIO(c.get("/api/export/frames.csv").content.decode("utf-8-sig"))))
    hong_frames = [f for f in frames if f["user_id"] == "hong" and f["file"] == "a/s0.wav"]
    assert [f["label"] for f in hong_frames][:10] == ["0", "0", "1", "1", "1", "1", "1", "1", "1", "0"]

    # data persists across a server restart
    app2 = App(app.data_dir)
    assert app2.dataset == ds.resolve()
    assert len(app2.db.labels()) == 3
