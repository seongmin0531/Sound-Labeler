"""Command line interface: beeplabel <command> ..."""

from __future__ import annotations

import argparse
import getpass
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from . import __version__, regions
from .metrics import FrameStats, fmt
from .project import DEFAULT_DATA_DIR, App


def _app(a) -> App:
    app = App(a.data_dir)
    if getattr(a, "dataset", None):
        app.set_dataset(a.dataset)
    return app


def cmd_serve(a):
    import uvicorn

    from .server import create_app

    app = _app(a)
    url = f"http://{'127.0.0.1' if a.host in ('0.0.0.0', '::') else a.host}:{a.port}/"
    print(f"Beep Labeler {__version__}")
    print(f"  데이터 저장 위치 : {app.data_dir}")
    print(f"  WAV 폴더        : {app.dataset_key or '(미지정 - 웹 화면 설정에서 지정)'}  파일 {len(app.files)}개")
    print(f"  접속 주소       : {url}")
    if a.host in ("0.0.0.0", "::"):
        print("  같은 네트워크의 다른 PC에서는 http://<이 PC의 IP>:%d/ 로 접속하세요." % a.port)
    print("  종료: Ctrl+C")
    if not a.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(app), host=a.host, port=a.port, log_level="warning")


def cmd_set_dataset(a):
    app = App(a.data_dir)
    app.set_dataset(a.path)
    app.db.log(None, "데이터 폴더 변경", after=app.dataset_key, detail="CLI")
    print(f"WAV 폴더를 {app.dataset_key} 로 지정했습니다 ({len(app.files)}개 파일).")


def cmd_export(a):
    from . import export as ex

    app = _app(a)
    if not app.dataset:
        sys.exit("WAV 폴더가 지정되지 않았습니다 (--dataset 또는 set-dataset).")
    out = Path(a.out or f"beep_export_{datetime.now():%Y%m%d_%H%M}")
    out.mkdir(parents=True, exist_ok=True)
    data = ex.collect(app, reviewed_only=a.reviewed_only)
    (out / "beep_labels.xlsx").write_bytes(ex.excel(app, data, "CLI"))
    (out / "labels.csv").write_bytes(ex.labels_csv(data))
    (out / "frames.csv").write_bytes(ex.frames_csv(app, data))
    (out / "audit.csv").write_bytes(ex.audit_csv(data))
    print(f"{len(data['labels'])}개 라벨, {len(data['audit'])}개 작업 이력 -> {out.resolve()}")


def cmd_users(a):
    app = App(a.data_dir)
    for u in app.db.users():
        print(f"{u['id']:<20} {u['display_name']:<16} {'admin' if u['is_admin'] else '':<6} "
              f"가입 {u['created_at']}  최근 로그인 {u['last_login'] or '-'}")


def cmd_reset_password(a):
    app = App(a.data_dir)
    if not app.db.get_user(a.user_id):
        sys.exit(f"없는 아이디: {a.user_id}")
    pw = a.password or getpass.getpass("새 비밀번호: ")
    app.db.set_password(a.user_id, pw)
    app.db.log(None, "비밀번호 초기화", detail=f"대상: {a.user_id} (CLI)")
    print("비밀번호를 변경했습니다. 해당 아이디의 기존 로그인은 모두 해제됩니다.")


def cmd_make_admin(a):
    app = App(a.data_dir)
    with app.db.conn() as c:
        n = c.execute("UPDATE users SET is_admin=1 WHERE id=?", (a.user_id,)).rowcount
    print("관리자로 지정했습니다." if n else f"없는 아이디: {a.user_id}")


def cmd_agree(a):
    """Agreement between two annotators (or an annotator and 'auto') over shared files."""
    app = _app(a)
    ua, ub = a.users

    def region_of(user, f):
        if user == "auto":
            r = app.auto_region(f)
            return r["start_frame"], r["end_frame"]
        l = app.db.get_label(app.dataset_key, f, user)
        if l is None or (a.reviewed_only and not l["reviewed"]):
            return None
        return l["start_frame"], l["end_frame"]

    fs = FrameStats()
    exact = n = 0
    ds, de = [], []
    for f in app.files:
        ra, rb = region_of(ua, f), region_of(ub, f)
        if ra is None or rb is None:
            continue
        nb = len(app.bounds(f))
        fs.add(regions.frame_vector(nb, *ra), regions.frame_vector(nb, *rb))
        n += 1
        exact += ra == rb
        if ra[0] is not None and rb[0] is not None:
            ds.append(abs(ra[0] - rb[0]))
            de.append(abs(ra[1] - rb[1]))
    if not n:
        print("두 작업자가 함께 라벨링한 파일이 없습니다.")
        return
    fm = app.config.frames.frame_ms
    print(f"비교 파일 {n}개 ({ua} vs {ub})")
    print(f"  구간 완전 일치: {exact}/{n} ({100 * exact / n:.1f}%)")
    if ds:
        print(f"  시작 경계 평균 차이: {fm * sum(ds) / len(ds):.1f} ms, 끝 경계 평균 차이: {fm * sum(de) / len(de):.1f} ms")
    print("  프레임 단위:", fmt(fs.summary()))


def cmd_demo(a):
    import numpy as np
    import soundfile as sf

    from .synth import make_clip

    out = Path(a.out)
    specs = [(2000, 10, 44100), (800, 5, 48000), (3500, 0, 44100), (1500, 15, 16000),
             (2500, 3, 44100), (1000, -3, 22050)][: a.n]
    for i, (f0, snr, sr) in enumerate(specs):
        y, _ = make_clip(sr=sr, snr_db=snr, seed=i, f0=f0, dur=5.0, n_beeps=2)
        rel = out / f"car_{'AB'[i % 2]}" / f"rec_{i:03d}_snr{snr:+d}dB.wav"
        rel.parent.mkdir(parents=True, exist_ok=True)
        sf.write(rel, y * (0.9 / np.abs(y).max()), sr, subtype="PCM_16")
    print(f"데모 WAV {len(specs)}개 생성: {out.resolve()}")
    print(f"  beeplabel serve --dataset {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="beeplabel", description="Beep Labeler " + __version__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--data-dir", default=None,
                    help=f"계정/라벨/이력 DB 저장 폴더 (기본: {DEFAULT_DATA_DIR}, 환경변수 BEEPLABEL_DATA)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("serve", help="웹 화면 실행")
    sp.add_argument("--dataset", "-d", help="WAV 폴더 (지정하면 저장되어 다음 실행에도 사용)")
    sp.add_argument("--host", default="127.0.0.1", help="다른 PC에서도 접속하려면 0.0.0.0")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--no-browser", action="store_true")
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("set-dataset", help="WAV 폴더 경로를 미리 지정")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_set_dataset)

    sp = sub.add_parser("export", help="엑셀 + CSV 내보내기")
    sp.add_argument("--dataset", "-d")
    sp.add_argument("--out", "-o")
    sp.add_argument("--reviewed-only", action="store_true")
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("users", help="계정 목록")
    sp.set_defaults(func=cmd_users)

    sp = sub.add_parser("reset-password", help="비밀번호 초기화 (관리자용)")
    sp.add_argument("user_id")
    sp.add_argument("--password")
    sp.set_defaults(func=cmd_reset_password)

    sp = sub.add_parser("make-admin", help="관리자 권한 부여")
    sp.add_argument("user_id")
    sp.set_defaults(func=cmd_make_admin)

    sp = sub.add_parser("agree", help="작업자 간 (또는 작업자-자동탐지) 일치도")
    sp.add_argument("--dataset", "-d")
    sp.add_argument("--users", nargs=2, required=True, metavar=("A", "B"), help="'auto' 사용 가능")
    sp.add_argument("--reviewed-only", action="store_true")
    sp.set_defaults(func=cmd_agree)

    sp = sub.add_parser("demo", help="합성 데모 WAV 생성")
    sp.add_argument("out")
    sp.add_argument("-n", type=int, default=6)
    sp.set_defaults(func=cmd_demo)

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
