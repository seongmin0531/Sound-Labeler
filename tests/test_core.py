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


def test_regions_max_100ms():
    n = SR * 3
    ev = [Event(int(0.43 * SR), int(0.61 * SR)), Event(int(1.05 * SR), int(1.18 * SR))]
    s, e = regions.suggest(ev, n, SR)
    assert (s, e) == (int(0.43 * SR), int(0.43 * SR) + 4410)   # first onset, capped at 100 ms
    assert regions.suggest([Event(100, 1000)], n, SR) == (100, 1000)  # short beep kept as is
    assert regions.suggest([], n, SR) == (None, None)
    assert regions.validate(0, 4410, n, SR) == (0, 4410)
    with pytest.raises(ValueError):
        regions.validate(0, 4411, n, SR)                      # 100 ms + 1 sample
    with pytest.raises(ValueError):
        regions.validate(5, 5, n, SR)
    assert regions.centered(SR, n, SR) == (SR - 2205, SR + 2205)   # click +-50 ms
    assert regions.centered(100, n, SR) == (0, 4410)               # pushed inside at the start
    assert regions.centered(n - 1, n, SR) == (n - 4410, n)
    b = frame_bounds(n, SR, 100)
    ov = regions.frame_overlap(b, int(0.45 * SR), int(0.53 * SR))  # straddles frames 4 and 5
    assert ov[4] == int(0.5 * SR) - int(0.45 * SR) and ov[5] == int(0.53 * SR) - int(0.5 * SR)
    assert ov.sum() == int(0.53 * SR) - int(0.45 * SR)


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
    assert c.get("/api/public").json()["users"] == []

    # id-only login: unknown id -> 404 until the user confirms creating it
    assert c.post("/api/auth/login", json={"user_id": "hong"}).status_code == 404
    r = c.post("/api/auth/login", json={"user_id": "hong", "create": True, "display_name": "홍길동"})
    assert r.status_code == 200 and r.json()["display_name"] == "홍길동"
    assert c.put("/api/settings", json={"dataset_path": str(ds)}).json()["n_files"] == 3
    files = c.get("/api/files").json()
    assert [f["file"] for f in files] == ["a/s0.wav", "a/s1.wav", "a/s2.wav"]

    v = c.get("/api/files/0/view").json()
    assert len(v["frame_starts"]) == 20 and v["max_region_ms"] == 100
    a = v["auto"]
    assert a["start_sample"] is not None and 0 < a["end_sample"] - a["start_sample"] <= 4410
    assert c.get("/api/files/0/audio").headers["content-type"] == "audio/wav"

    put = lambda body: c.put("/api/files/0/label", json={"file": "a/s0.wav", **body})
    s1, e1 = int(0.31 * SR), int(0.39 * SR)            # 80 ms
    r = put({"start_sample": s1, "end_sample": e1, "reviewed": False, "memo": ""}).json()
    assert r["label"]["start_s"] == round(s1 / SR, 6) and r["actions"] == ["구간 지정"]
    s2, e2 = 13230, 17640                                # 0.3~0.4 s, exactly 100 ms
    r = put({"start_sample": s2, "end_sample": e2, "reviewed": True, "memo": "비프 2회"}).json()
    assert set(r["actions"]) == {"구간 수정", "메모 작성", "검수 완료"} or set(r["actions"]) == {"구간 수정", "메모 수정", "검수 완료"}
    assert put({"start_sample": s2, "end_sample": s2 + 4411, "reviewed": True, "memo": ""}).status_code == 400
    c.put("/api/files/1/label", json={"file": "a/s1.wav", "start_sample": None, "end_sample": None,
                                      "reviewed": True, "memo": "조용함"})

    # logout -> login again (id only) restores everything
    c.post("/api/auth/logout", json={"saved": True})
    assert c.get("/api/files/0/label").status_code == 401
    assert c.get("/api/public").json()["users"] == ["hong"]
    c.post("/api/auth/login", json={"user_id": "hong"})
    me = c.get("/api/me").json()
    assert me["user"]["last_file"] == "a/s1.wav" and me["settings"]["dataset_path"] == str(ds.resolve())
    lab = c.get("/api/files/0/label").json()["label"]
    assert (lab["start_sample"], lab["end_sample"], lab["reviewed"], lab["memo"]) == (s2, e2, 1, "비프 2회")

    # second user labels the same file 50 ms later; everyone may change settings
    c.post("/api/auth/logout", json={})
    c.post("/api/auth/login", json={"user_id": "kim", "create": True})
    assert c.put("/api/settings", json={"dataset_path": str(ds)}).status_code == 200
    s3 = int(0.35 * SR); e3 = s3 + 4410
    c.put("/api/files/0/label", json={"file": "a/s0.wav", "start_sample": s3, "end_sample": e3, "reviewed": True, "memo": ""})
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
    assert hong0["비프 구간"] == "0.300~0.400초" and hong0["메모"] == "비프 2회" and hong0["상태"] == "검수 완료"
    assert hong0["길이(ms)"] == 100.0 and hong0["100ms 클립 구간(중심 기준)"] == "0.300~0.400초"
    assert next(r for r in recs if r["파일"] == "a/s1.wav")["비프 여부"] == "없음"
    acts = [dict(zip(list(wb["작업 이력"].values)[0], r)) for r in list(wb["작업 이력"].values)[1:]]
    mod = next(a for a in acts if a["동작"] == "구간 수정")
    assert mod["작업자 ID"] == "hong" and mod["변경 전"] == "0.310~0.390초" and mod["변경 후"] == "0.300~0.400초"
    assert any(a["동작"] == "저장 후 로그아웃" for a in acts)
    files_sheet = [dict(zip(list(wb["파일 현황"].values)[0], r)) for r in list(wb["파일 현황"].values)[1:]]
    assert files_sheet[0]["작업자 간 일치(±20ms)"] == "불일치" and files_sheet[0]["시작 최대 차이(ms)"] == 50.0

    # CSV: seconds range + memo, UTF-8 BOM for Excel
    raw = c.get("/api/export/labels.csv").content
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    r0 = next(r for r in rows if r["user_id"] == "hong" and r["file"] == "a/s0.wav")
    assert (r0["start_s"], r0["end_s"], r0["memo"], r0["duration_ms"]) == ("0.3", "0.4", "비프 2회", "100.0")
    r1 = next(r for r in rows if r["user_id"] == "kim")
    assert (float(r1["clip100_start_s"]), float(r1["clip100_end_s"])) == pytest.approx((0.35, 0.45), abs=1e-4)
    frames = list(csv.DictReader(io.StringIO(c.get("/api/export/frames.csv").content.decode("utf-8-sig"))))
    hong_frames = [f for f in frames if f["user_id"] == "hong" and f["file"] == "a/s0.wav"]
    assert [f["label"] for f in hong_frames][:6] == ["0", "0", "0", "1", "0", "0"]
    kim_frames = [f for f in frames if f["user_id"] == "kim"]
    assert [f["overlap_ms"] for f in kim_frames][3:5] == ["50.0", "50.0"]

    # data persists across a server restart
    app2 = App(app.data_dir)
    assert app2.dataset == ds.resolve()
    assert len(app2.db.labels()) == 3
