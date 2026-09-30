"""Readable Excel workbook + machine-friendly CSV exports (who labeled what, when, and how)."""

from __future__ import annotations

import csv
import io
import unicodedata
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import __version__, regions
from .db import now, region_text
from .project import App

STATUS = {1: "검수 완료", 0: "작업 중"}


def _dur(r):
    return None if r.get("start_s") is None else round(r["end_s"] - r["start_s"], 3)


def collect(app: App, reviewed_only: bool = False, with_auto: bool = True) -> dict:
    ds = app.dataset_key
    users = {u["id"]: u for u in app.db.users()}
    labels = [l for l in app.db.labels(dataset=ds) if l["file"] in set(app.files)]
    if reviewed_only:
        labels = [l for l in labels if l["reviewed"]]
    auto = {}
    if with_auto:
        for f in sorted({l["file"] for l in labels}):
            try:
                auto[f] = app.auto_region(f)
            except Exception:  # unreadable file: leave the auto column empty
                auto[f] = None
    for l in labels:
        u = users.get(l["user_id"], {})
        l["display_name"] = u.get("display_name", l["user_id"])
        l["status"] = STATUS[l["reviewed"]]
        l["has_beep"] = l["start_frame"] is not None
        l["duration_s"] = _dur(l)
        l["region"] = region_text(l["start_s"], l["end_s"])
        a = auto.get(l["file"])
        l["auto_region"] = region_text(a["start_s"], a["end_s"]) if a else ""
        if a and a["start_s"] is not None and l["has_beep"]:
            l["auto_diff_start"] = round(l["start_s"] - a["start_s"], 3)
            l["auto_diff_end"] = round(l["end_s"] - a["end_s"], 3)
        else:
            l["auto_diff_start"] = l["auto_diff_end"] = None
        l["file_duration_s"] = round(l["num_samples"] / l["sample_rate"], 3) if l["sample_rate"] else None
    audit = list(reversed(app.db.audit(dataset=ds)))
    audit += [a for a in reversed(app.db.audit()) if a["dataset"] is None]  # logins etc.
    audit.sort(key=lambda a: a["id"])
    return {"users": users, "labels": labels, "audit": audit, "auto": auto}


# ---------------- CSV ----------------

LABEL_COLS = [
    ("file", "file"), ("user_id", "user_id"), ("user_name", "display_name"),
    ("status", "status"), ("has_beep", "has_beep"), ("start_s", "start_s"), ("end_s", "end_s"),
    ("duration_s", "duration_s"), ("start_frame", "start_frame"),
    ("end_frame_inclusive", None), ("n_frames", None), ("memo", "memo"),
    ("auto_start_s", None), ("auto_end_s", None), ("n_edits", "n_edits"),
    ("created_at", "created_at"), ("updated_at", "updated_at"),
    ("sample_rate", "sample_rate"), ("file_duration_s", "file_duration_s"), ("sha256", "sha256"),
]


def labels_csv(data: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c for c, _ in LABEL_COLS])
    for l in data["labels"]:
        a = data["auto"].get(l["file"]) or {}
        row = []
        for col, key in LABEL_COLS:
            if col == "end_frame_inclusive":
                v = l["end_frame"] - 1 if l["has_beep"] else ""
            elif col == "n_frames":
                v = l["end_frame"] - l["start_frame"] if l["has_beep"] else 0
            elif col == "has_beep":
                v = int(l["has_beep"])
            elif col == "auto_start_s":
                v = a.get("start_s")
            elif col == "auto_end_s":
                v = a.get("end_s")
            else:
                v = l.get(key)
            row.append("" if v is None else v)
        w.writerow(row)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows Korean correctly


def frames_csv(app: App, data: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["file", "user_id", "status", "frame_index", "start_s", "end_s", "label", "memo"])
    for l in data["labels"]:
        b = app.bounds(l["file"])
        sr = l["sample_rate"]
        vec = regions.frame_vector(len(b), l["start_frame"], l["end_frame"])
        for i, (s, e) in enumerate(b):
            w.writerow([l["file"], l["user_id"], l["status"], i, round(s / sr, 6),
                        round(e / sr, 6), int(vec[i]), l["memo"]])
    return buf.getvalue().encode("utf-8-sig")


def audit_csv(data: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "timestamp", "user_id", "file", "action", "before", "after", "detail"])
    for a in data["audit"]:
        w.writerow([a["id"], a["ts"], a["user_id"] or "", a["file"] or "", a["action"],
                    a["before"] or "", a["after"] or "", a["detail"] or ""])
    return buf.getvalue().encode("utf-8-sig")


# ---------------- Excel ----------------

HEAD_FILL = PatternFill("solid", fgColor="1F3864")
HEAD_FONT = Font(bold=True, color="FFFFFF")
TITLE_FONT = Font(bold=True, size=14)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
FILL = {
    "검수 완료": PatternFill("solid", fgColor="E2EFDA"),
    "작업 중": PatternFill("solid", fgColor="FFF2CC"),
    "zebra": PatternFill("solid", fgColor="F7F9FC"),
    "일치": PatternFill("solid", fgColor="E2EFDA"),
    "불일치": PatternFill("solid", fgColor="FCE4D6"),
}
ACTION_FILL = {
    "구간 지정": "DDEBF7", "구간 수정": "FFF2CC", "구간 삭제(비프 없음)": "FCE4D6",
    "비프 없음 지정": "FCE4D6", "검수 완료": "E2EFDA", "검수 취소": "F8CBAD",
    "메모 작성": "EDE7F6", "메모 수정": "EDE7F6", "로그인": "F2F2F2", "로그아웃": "F2F2F2",
    "저장 후 로그아웃": "F2F2F2", "회원가입": "F2F2F2", "데이터 폴더 변경": "FFE699",
    "내보내기": "F2F2F2", "비밀번호 변경": "F2F2F2", "비밀번호 초기화": "F2F2F2",
}


def _width(v) -> int:
    s = "" if v is None else str(v)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _table(ws, headers: list[str], rows: list[list], start_row: int = 1, widths: dict | None = None,
           wrap_cols: set[int] = frozenset(), fills=None):
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=start_row, column=j, value=h)
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BORDER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[start_row].height = 30
    for i, row in enumerate(rows, start_row + 1):
        fill = fills(row) if fills else None
        for j, v in enumerate(row, 1):
            c = ws.cell(row=i, column=j, value=v)
            c.border = BORDER
            c.alignment = Alignment(vertical="top", wrap_text=j in wrap_cols)
            if isinstance(v, float):
                c.number_format = "0.000"
            if fill:
                c.fill = fill
            elif (i - start_row) % 2 == 0:
                c.fill = FILL["zebra"]
    for j, h in enumerate(headers, 1):
        w = max([_width(h)] + [_width(r[j - 1]) for r in rows[:2000]]) + 2
        if widths and h in widths:
            w = widths[h]
        ws.column_dimensions[get_column_letter(j)].width = min(max(w, 8), 60)
    last = get_column_letter(len(headers))
    ws.auto_filter.ref = f"A{start_row}:{last}{start_row + max(len(rows), 1)}"
    ws.freeze_panes = ws.cell(row=start_row + 1, column=2)


def excel(app: App, data: dict, exported_by: str) -> bytes:
    wb = Workbook()
    labels, users = data["labels"], data["users"]

    # 1) labels
    ws = wb.active
    ws.title = "라벨 결과"
    headers = ["번호", "파일", "작업자 ID", "작업자 이름", "상태", "비프 여부", "비프 구간",
               "시작(초)", "끝(초)", "길이(초)", "시작 프레임", "끝 프레임", "프레임 수",
               "자동탐지 구간", "자동 대비 시작 차이(초)", "자동 대비 끝 차이(초)", "메모",
               "수정 횟수", "최초 작업 일시", "최종 수정 일시", "샘플레이트(Hz)", "파일 길이(초)"]
    rows = []
    for n, l in enumerate(labels, 1):
        rows.append([n, l["file"], l["user_id"], l["display_name"], l["status"],
                     "있음" if l["has_beep"] else "없음", l["region"], l["start_s"], l["end_s"],
                     l["duration_s"], l["start_frame"],
                     l["end_frame"] - 1 if l["has_beep"] else None,
                     (l["end_frame"] - l["start_frame"]) if l["has_beep"] else 0,
                     l["auto_region"], l["auto_diff_start"], l["auto_diff_end"], l["memo"],
                     l["n_edits"], l["created_at"], l["updated_at"], l["sample_rate"],
                     l["file_duration_s"]])
    _table(ws, headers, rows, widths={"메모": 40}, wrap_cols={17},
           fills=lambda r: FILL.get(r[4]))

    # 2) audit trail
    ws = wb.create_sheet("작업 이력")
    headers = ["번호", "일시", "작업자 ID", "작업자 이름", "파일", "동작", "변경 전", "변경 후", "비고"]
    rows = [[a["id"], a["ts"], a["user_id"] or "",
             users.get(a["user_id"], {}).get("display_name", a["user_id"] or ""),
             a["file"] or "", a["action"], a["before"] or "", a["after"] or "", a["detail"] or ""]
            for a in data["audit"]]
    _table(ws, headers, rows, widths={"변경 전": 30, "변경 후": 30, "비고": 30},
           wrap_cols={7, 8, 9},
           fills=lambda r: PatternFill("solid", fgColor=ACTION_FILL[r[5]]) if r[5] in ACTION_FILL else None)

    # 3) per-user summary
    ws = wb.create_sheet("작업자 요약")
    n_files = len(app.files)
    by_user = defaultdict(list)
    for l in labels:
        by_user[l["user_id"]].append(l)
    acts = defaultdict(int)
    for a in data["audit"]:
        acts[a["user_id"]] += 1
    headers = ["작업자 ID", "작업자 이름", "관리자", "가입 일시", "마지막 로그인", "작업한 파일 수",
               "검수 완료", "작업 중", "비프 있음", "비프 없음", "평균 구간 길이(초)",
               "작업 이력 수", "진행률(검수/전체 파일)"]
    rows = []
    for uid, u in users.items():
        ls = by_user.get(uid, [])
        durs = [l["duration_s"] for l in ls if l["duration_s"] is not None]
        done = sum(l["reviewed"] for l in ls)
        rows.append([uid, u["display_name"], "예" if u["is_admin"] else "", u["created_at"],
                     u["last_login"] or "", len(ls), done, len(ls) - done,
                     sum(l["has_beep"] for l in ls), sum(not l["has_beep"] for l in ls),
                     round(sum(durs) / len(durs), 3) if durs else None, acts.get(uid, 0),
                     f"{done}/{n_files} ({100 * done / n_files:.1f}%)" if n_files else "-"])
    _table(ws, headers, rows)

    # 4) per-file overview (agreement between annotators)
    ws = wb.create_sheet("파일 현황")
    by_file = defaultdict(list)
    for l in labels:
        by_file[l["file"]].append(l)
    headers = ["파일", "파일 길이(초)", "작업자 수", "검수 완료 작업자", "작업자별 구간",
               "작업자 간 일치", "시작 최대 차이(초)", "끝 최대 차이(초)", "자동탐지 구간", "메모 모음"]
    rows = []
    for f in app.files:
        ls = by_file.get(f, [])
        info = app.info(f)
        regs = {(l["start_frame"], l["end_frame"]) for l in ls}
        agree = "—" if len(ls) < 2 else ("일치" if len(regs) == 1 else "불일치")
        beeps = [l for l in ls if l["has_beep"]]
        ds = round(max(l["start_s"] for l in beeps) - min(l["start_s"] for l in beeps), 3) if len(beeps) >= 2 else None
        de = round(max(l["end_s"] for l in beeps) - min(l["end_s"] for l in beeps), 3) if len(beeps) >= 2 else None
        a = data["auto"].get(f)
        rows.append([f, round(info.duration, 3), len(ls),
                     ", ".join(l["user_id"] for l in ls if l["reviewed"]),
                     "\n".join(f"{l['user_id']}: {l['region']}" for l in ls), agree, ds, de,
                     region_text(a["start_s"], a["end_s"]) if a else "",
                     "\n".join(f"{l['user_id']}: {l['memo']}" for l in ls if l["memo"])])
    _table(ws, headers, rows, widths={"작업자별 구간": 34, "메모 모음": 40}, wrap_cols={5, 10},
           fills=lambda r: FILL.get(r[5]))

    # 5) info
    ws = wb.create_sheet("정보")
    ws["A1"] = "Beep Labeler 내보내기 정보"
    ws["A1"].font = TITLE_FONT
    info_rows = [
        ("생성 일시", now()), ("내보낸 사람", exported_by), ("데이터 폴더", app.dataset_key),
        ("툴 버전", __version__), ("구간 단위", f"{app.config.frames.frame_ms:g} ms (100 ms 격자에 맞춰 표시)"),
        ("라벨 규칙", "샘플(파일)마다 비프 구간은 최대 1개입니다. 구간 경계는 100 ms 프레임 경계에 "
                     "맞춰지며, 오디오는 자르지 않고 표시만 합니다. '비프 없음'은 해당 샘플에 비프가 없다고 "
                     "작업자가 판단한 것입니다."),
        ("끝(초)", "구간 마지막 프레임의 끝 시각입니다 (예: 1.2~1.8초 = 1.2초부터 1.8초 직전까지 6개 프레임)."),
        ("자동탐지", "협대역 톤 검출기가 제안한 구간입니다. 사람이 검수한 결과가 정답입니다."),
        ("탐지 설정 해시", app.config.digest()),
        ("전체 파일 수", len(app.files)), ("라벨 수(작업자×파일)", len(labels)),
        ("작업자 수", len(users)), ("작업 이력 수", len(data["audit"])),
        ("시트 설명", "라벨 결과: 작업자별 최종 구간 / 작업 이력: 누가 언제 무엇을 어떻게 바꿨는지 전부 / "
                    "작업자 요약: 작업자별 진행 현황 / 파일 현황: 파일별 작업자 간 일치 여부"),
    ]
    for i, (k, v) in enumerate(info_rows, 3):
        ws.cell(row=i, column=1, value=k).font = Font(bold=True)
        c = ws.cell(row=i, column=2, value=v)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
