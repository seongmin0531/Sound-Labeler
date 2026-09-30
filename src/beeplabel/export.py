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
from .project import Dataset

STATUS = {1: "검수 완료", 0: "작업 중"}
AGREE_TOL_MS = 20.0  # annotators "agree" when both boundaries are within this many ms


def collect(app: Dataset, user_id: str | None = None, reviewed_only: bool = False,
            with_auto: bool = True) -> dict:
    """Labels + history of one WAV folder. With user_id (the web UI), only that id's own work."""
    ds = app.dataset_key
    users = {u["id"]: u for u in app.db.users() if user_id is None or u["id"] == user_id}
    files = set(app.files)
    labels = [l for l in app.db.labels(dataset=ds, user_id=user_id) if l["file"] in files]
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
        sr, n = l["sample_rate"], l["num_samples"]
        l["display_name"] = users.get(l["user_id"], {}).get("display_name", l["user_id"])
        l["status"] = STATUS[l["reviewed"]]
        l["has_beep"] = l["start_sample"] is not None
        l["duration_ms"] = round(1000 * (l["end_sample"] - l["start_sample"]) / sr, 1) if l["has_beep"] else None
        l["region"] = region_text(l["start_s"], l["end_s"])
        cs, ce = regions.clip_window(l["start_sample"], l["end_sample"], n, sr)
        l["clip_start_s"], l["clip_end_s"] = regions.seconds(sr, cs, ce)
        l["clip_region"] = region_text(l["clip_start_s"], l["clip_end_s"]) if l["has_beep"] else ""
        a = auto.get(l["file"])
        l["auto_region"] = region_text(a["start_s"], a["end_s"]) if a else ""
        if a and a["start_s"] is not None and l["has_beep"]:
            l["auto_diff_start_ms"] = round(1000 * (l["start_s"] - a["start_s"]), 1)
            l["auto_diff_end_ms"] = round(1000 * (l["end_s"] - a["end_s"]), 1)
        else:
            l["auto_diff_start_ms"] = l["auto_diff_end_ms"] = None
        l["file_duration_s"] = round(n / sr, 3) if sr else None
    audit = sorted(app.db.audit(dataset=ds, user_id=user_id)
                   + [a for a in app.db.audit(user_id=user_id) if a["dataset"] is None],
                   key=lambda a: a["id"])
    return {"users": users, "labels": labels, "audit": audit, "auto": auto}


# ---------------- CSV ----------------

def _csv_safe(v):
    """Neutralise spreadsheet formula injection: Excel evaluates CSV cells starting with these."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _csv(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([_csv_safe(v) for v in r])
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel shows Korean correctly


def labels_csv(data: dict) -> bytes:
    header = ["file", "user_id", "user_name", "status", "has_beep", "start_s", "end_s",
              "duration_ms", "start_sample", "end_sample", "clip100_start_s", "clip100_end_s",
              "memo", "auto_start_s", "auto_end_s", "n_edits", "created_at", "updated_at",
              "sample_rate", "file_duration_s", "sha256"]
    rows = []
    for l in data["labels"]:
        a = data["auto"].get(l["file"]) or {}
        rows.append([l["file"], l["user_id"], l["display_name"], l["status"], int(l["has_beep"]),
                     l["start_s"], l["end_s"], l["duration_ms"], l["start_sample"], l["end_sample"],
                     l["clip_start_s"], l["clip_end_s"], l["memo"], a.get("start_s"), a.get("end_s"),
                     l["n_edits"], l["created_at"], l["updated_at"], l["sample_rate"],
                     l["file_duration_s"], l["sha256"]])
    return _csv(header, rows)


def frames_csv(app: Dataset, data: dict) -> bytes:
    """100 ms grid frames; label = 1 if the region overlaps the frame."""
    def rows():
        for l in data["labels"]:
            b = app.bounds(l["file"])
            sr = l["sample_rate"]
            ov = regions.frame_overlap(b, l["start_sample"], l["end_sample"])
            for i, (s, e) in enumerate(b):
                yield [l["file"], l["user_id"], l["status"], i, round(s / sr, 6), round(e / sr, 6),
                       round(1000 * ov[i] / sr, 1), int(ov[i] > 0), l["memo"]]
    return _csv(["file", "user_id", "status", "frame_index", "start_s", "end_s", "overlap_ms",
                 "label", "memo"], rows())


def audit_csv(data: dict) -> bytes:
    return _csv(["id", "timestamp", "user_id", "file", "action", "before", "after", "detail"],
                ([a["id"], a["ts"], a["user_id"], a["file"], a["action"], a["before"], a["after"],
                  a["detail"]] for a in data["audit"]))


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
    "저장 후 로그아웃": "F2F2F2", "아이디 생성": "F2F2F2", "데이터 폴더 변경": "FFE699",
    "내보내기": "F2F2F2",
}

# Column order of the "라벨 결과" sheet. merge.py reads these headers back.
LABEL_HEADERS = ["번호", "파일", "작업자 ID", "작업자 이름", "상태", "비프 여부", "비프 구간",
                 "시작(초)", "끝(초)", "길이(ms)", "100ms 클립 구간(중심 기준)", "자동탐지 구간",
                 "자동 대비 시작 차이(ms)", "자동 대비 끝 차이(ms)", "메모", "수정 횟수",
                 "최초 작업 일시", "최종 수정 일시", "샘플레이트(Hz)", "파일 길이(초)",
                 "시작 샘플", "끝 샘플", "SHA-256"]
LABEL_KEYS = [None, "file", "user_id", "display_name", "status", None, "region",
              "start_s", "end_s", "duration_ms", "clip_region", "auto_region",
              "auto_diff_start_ms", "auto_diff_end_ms", "memo", "n_edits",
              "created_at", "updated_at", "sample_rate", "file_duration_s",
              "start_sample", "end_sample", "sha256"]
AUDIT_HEADERS = ["번호", "일시", "작업자 ID", "작업자 이름", "파일", "동작", "변경 전", "변경 후", "비고"]
SOURCE_HEADER = "출처 파일"


def _width(v) -> int:
    s = "" if v is None else str(v)
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in s)


def _set(c, v):
    """Write a value; text starting with '=' stays text (never an Excel formula)."""
    c.value = v
    if isinstance(v, str) and v.startswith("="):
        c.data_type = "s"


def _table(ws, headers: list[str], rows: list[list], widths: dict | None = None,
           wrap_cols=frozenset(), fills=None, formats: dict | None = None):
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=j, value=h)
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BORDER
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 30
    fmt_by_col = {headers.index(h) + 1: f for h, f in (formats or {}).items() if h in headers}
    for i, row in enumerate(rows, 2):
        fill = fills(row) if fills else None
        for j, v in enumerate(row, 1):
            c = ws.cell(row=i, column=j)
            _set(c, v)
            c.border = BORDER
            c.alignment = Alignment(vertical="top", wrap_text=j in wrap_cols)
            if j in fmt_by_col and isinstance(v, (int, float)):
                c.number_format = fmt_by_col[j]
            if fill:
                c.fill = fill
            elif i % 2 == 1:
                c.fill = FILL["zebra"]
    for j, h in enumerate(headers, 1):
        w = max([_width(h)] + [_width(r[j - 1]) for r in rows[:2000]]) + 2
        if widths and h in widths:
            w = widths[h]
        ws.column_dimensions[get_column_letter(j)].width = min(max(w, 8), 60)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(len(rows), 1) + 1}"
    ws.freeze_panes = "B2"


def agreement(ls: list[dict]) -> tuple[str, float | None, float | None]:
    """(verdict, max start diff ms, max end diff ms) for one file's labels from several people."""
    beeps = [l for l in ls if l["has_beep"]]
    ds = round(1000 * (max(l["start_s"] for l in beeps) - min(l["start_s"] for l in beeps)), 1) if len(beeps) >= 2 else None
    de = round(1000 * (max(l["end_s"] for l in beeps) - min(l["end_s"] for l in beeps)), 1) if len(beeps) >= 2 else None
    if len(ls) < 2:
        return "—", ds, de
    if 0 < len(beeps) < len(ls):
        return "불일치", ds, de  # someone says beep, someone says no beep
    if not beeps:
        return "일치", ds, de
    return ("일치" if max(ds, de) <= AGREE_TOL_MS else "불일치"), ds, de


def write_workbook(labels: list[dict], audit: list[dict], users: dict, files: list[tuple],
                   auto_by_file: dict, title: str, info_rows: list[tuple],
                   with_source: bool = False) -> bytes:
    """labels/audit: normalised dicts (see collect() / merge.read_export()).
    users: {id: {display_name, created_at, last_login}}; files: [(file, duration_s)]."""
    wb = Workbook()
    src = [SOURCE_HEADER] if with_source else []

    # 1) labels
    ws = wb.active
    ws.title = "라벨 결과"
    rows = []
    for n, l in enumerate(labels, 1):
        row = []
        for h, k in zip(LABEL_HEADERS, LABEL_KEYS):
            if h == "번호":
                row.append(n)
            elif h == "비프 여부":
                row.append("있음" if l["has_beep"] else "없음")
            else:
                row.append(l.get(k))
        rows.append(row + ([l.get("source", "")] if with_source else []))
    _table(ws, LABEL_HEADERS + src, rows, widths={"메모": 40, "SHA-256": 20}, wrap_cols={15},
           formats={"시작(초)": "0.000", "끝(초)": "0.000"}, fills=lambda r: FILL.get(r[4]))

    # 2) audit trail
    ws = wb.create_sheet("작업 이력")
    rows = [[n, a["ts"], a["user_id"] or "", a.get("display_name") or users.get(a["user_id"], {}).get("display_name", a["user_id"] or ""),
             a["file"] or "", a["action"], a["before"] or "", a["after"] or "", a["detail"] or ""]
            + ([a.get("source", "")] if with_source else []) for n, a in enumerate(audit, 1)]
    _table(ws, AUDIT_HEADERS + src, rows, widths={"변경 전": 30, "변경 후": 30, "비고": 30}, wrap_cols={7, 8, 9},
           fills=lambda r: PatternFill("solid", fgColor=ACTION_FILL[r[5]]) if r[5] in ACTION_FILL else None)

    # 3) per-user summary
    ws = wb.create_sheet("작업자 요약")
    n_files = len(files)
    by_user = defaultdict(list)
    for l in labels:
        by_user[l["user_id"]].append(l)
    acts = defaultdict(int)
    for a in audit:
        acts[a["user_id"]] += 1
    headers = ["작업자 ID", "작업자 이름", "생성 일시", "마지막 로그인", "작업한 파일 수", "검수 완료",
               "작업 중", "비프 있음", "비프 없음", "평균 구간 길이(ms)", "작업 이력 수",
               "진행률(검수/전체 파일)"]
    rows = []
    for uid, u in users.items():
        ls = by_user.get(uid, [])
        durs = [l["duration_ms"] for l in ls if l.get("duration_ms") is not None]
        done = sum(bool(l["reviewed"]) for l in ls)
        rows.append([uid, u.get("display_name") or uid, u.get("created_at") or "", u.get("last_login") or "",
                     len(ls), done, len(ls) - done, sum(l["has_beep"] for l in ls),
                     sum(not l["has_beep"] for l in ls),
                     round(sum(durs) / len(durs), 1) if durs else None, acts.get(uid, 0),
                     f"{done}/{n_files} ({100 * done / n_files:.1f}%)" if n_files else "-"])
    _table(ws, headers, rows)

    # 4) per-file overview (agreement between annotators)
    ws = wb.create_sheet("파일 현황")
    by_file = defaultdict(list)
    for l in labels:
        by_file[l["file"]].append(l)
    headers = ["파일", "파일 길이(초)", "작업자 수", "검수 완료 작업자", "작업자별 구간",
               f"작업자 간 일치(±{AGREE_TOL_MS:g}ms)", "시작 최대 차이(ms)", "끝 최대 차이(ms)",
               "자동탐지 구간", "메모 모음"]
    rows = []
    for f, dur in files:
        ls = by_file.get(f, [])
        verdict, ds, de = agreement(ls)
        rows.append([f, dur, len(ls), ", ".join(l["user_id"] for l in ls if l["reviewed"]),
                     "\n".join(f"{l['user_id']}: {l['region']}" for l in ls), verdict, ds, de,
                     auto_by_file.get(f, ""),
                     "\n".join(f"{l['user_id']}: {l['memo']}" for l in ls if l["memo"])])
    _table(ws, headers, rows, widths={"작업자별 구간": 38, "메모 모음": 40}, wrap_cols={5, 10},
           fills=lambda r: FILL.get(r[5]))

    # 5) info
    ws = wb.create_sheet("정보")
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    for i, (k, v) in enumerate(info_rows, 3):
        ws.cell(row=i, column=1, value=k).font = Font(bold=True)
        c = ws.cell(row=i, column=2)
        _set(c, v)
        c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 100

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


RULE_ROWS = [
    ("라벨 규칙", f"샘플(파일)마다 비프 구간은 최대 1개이며 길이는 0~{regions.MAX_MS:g} ms 입니다. "
                 "구간은 오디오를 자르지 않고 표시만 합니다. '비프 없음'은 해당 샘플에 비프가 없다고 "
                 "작업자가 판단한 것입니다."),
    ("100ms 클립 구간", "표시한 구간의 중심을 기준으로 양옆 50 ms씩 잡은 100 ms 창입니다 "
                      "(파일 끝에서는 안쪽으로 밀어서 100 ms 유지). 나중에 100 ms 샘플을 자를 때 사용합니다."),
    ("작업자 간 일치", f"두 작업자 이상이 라벨링한 파일에서 시작·끝 경계 차이가 모두 {AGREE_TOL_MS:g} ms 이하면 '일치'."),
    ("자동탐지", "협대역 톤 검출기가 찾은 첫 비프의 시작점부터 최대 100 ms. 사람이 검수한 결과가 정답입니다."),
    ("로그인 방식", "아이디만으로 로그인합니다. 작업자 ID는 본인 확인을 거친 값이 아닙니다."),
]
SHEET_HELP = ("시트 설명", "라벨 결과: 작업자별 최종 구간 / 작업 이력: 누가 언제 무엇을 어떻게 바꿨는지 / "
                          "작업자 요약: 작업자별 진행 현황 / 파일 현황: 파일별 작업자 간 일치 여부")


def excel(app: Dataset, data: dict, exported_by: str) -> bytes:
    labels = data["labels"]
    for l in labels:
        l.setdefault("source", "")
    audit = [{**a, "display_name": data["users"].get(a["user_id"], {}).get("display_name", a["user_id"])}
             for a in data["audit"]]
    auto_by_file = {f: region_text(a["start_s"], a["end_s"]) for f, a in data["auto"].items() if a}
    files = [(f, round(app.info(f).duration, 3)) for f in app.files]
    info = [("생성 일시", now()), ("내보낸 사람", exported_by), ("데이터 폴더", app.dataset_key),
            ("툴 버전", __version__), *RULE_ROWS, ("탐지 설정 해시", app.config.digest()),
            ("전체 파일 수", len(app.files)), ("라벨 수(작업자×파일)", len(labels)),
            ("작업자 수", len(data["users"])), ("작업 이력 수", len(audit)), SHEET_HELP]
    return write_workbook(labels, audit, data["users"], files, auto_by_file,
                          "Beep Labeler 내보내기 정보", info)
