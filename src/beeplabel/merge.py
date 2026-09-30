"""Merge Excel exports made on different PCs into one workbook.

Everyone labels on their own PC and exports an .xlsx; this reads the "라벨 결과" and
"작업 이력" sheets of each file back and writes one combined workbook with the same sheets
(plus a source-file column), so agreement between annotators can be compared in one place.
Files are matched by their path inside the dataset folder; SHA-256 mismatches are reported.
Merged workbooks can themselves be merged again.
"""

from __future__ import annotations

import io
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

from . import __version__
from .db import now
from .export import (AUDIT_HEADERS, LABEL_HEADERS, LABEL_KEYS, RULE_ROWS, SHEET_HELP,
                     SOURCE_HEADER, write_workbook)


class MergeError(ValueError):
    pass


def _rows(ws) -> tuple[list[str], list[dict]]:
    it = ws.iter_rows(values_only=True)
    try:
        head = [str(h) if h is not None else "" for h in next(it)]
    except StopIteration:
        return [], []
    out = []
    for r in it:
        if r is None or all(v is None for v in r):
            continue
        out.append({h: v for h, v in zip(head, r)})
    return head, out


def _text(v) -> str:
    return "" if v is None else str(v)


def read_export(data: bytes, name: str) -> dict:
    """Parse one Beep Labeler .xlsx export into normalised label/audit dicts."""
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True)
    except Exception as e:
        raise MergeError(f"{name}: 엑셀 파일을 열 수 없습니다 ({e})")
    if "라벨 결과" not in wb.sheetnames or "작업 이력" not in wb.sheetnames:
        raise MergeError(f"{name}: Beep Labeler에서 내보낸 엑셀이 아닙니다 ('라벨 결과'/'작업 이력' 시트 없음)")
    head, rows = _rows(wb["라벨 결과"])
    for need in ("파일", "작업자 ID", "상태", "비프 여부", "시작(초)", "끝(초)"):
        if need not in head:
            raise MergeError(f"{name}: '라벨 결과' 시트에 '{need}' 열이 없습니다")
    labels = []
    for r in rows:
        l = {k: r.get(h) for h, k in zip(LABEL_HEADERS, LABEL_KEYS) if k}
        l["file"], l["user_id"] = _text(l["file"]), _text(l["user_id"])
        if not l["file"] or not l["user_id"]:
            continue
        l["display_name"] = _text(l.get("display_name")) or l["user_id"]
        l["status"] = _text(l.get("status"))
        l["reviewed"] = l["status"] == "검수 완료"
        l["has_beep"] = _text(r.get("비프 여부")) == "있음"
        l["memo"] = _text(l.get("memo"))
        for k in ("region", "clip_region", "auto_region", "created_at", "updated_at", "sha256"):
            l[k] = _text(l.get(k))
        if l["has_beep"] and (l.get("start_s") is None or l.get("end_s") is None):
            raise MergeError(f"{name}: {l['file']} / {l['user_id']} 의 시작·끝 초가 비어 있습니다")
        l["source"] = _text(r.get(SOURCE_HEADER)) or name
        labels.append(l)
    _, arows = _rows(wb["작업 이력"])
    audit = []
    for r in arows:
        audit.append({"ts": _text(r.get("일시")), "user_id": _text(r.get("작업자 ID")),
                      "display_name": _text(r.get("작업자 이름")), "file": _text(r.get("파일")),
                      "action": _text(r.get("동작")), "before": _text(r.get("변경 전")),
                      "after": _text(r.get("변경 후")), "detail": _text(r.get("비고")),
                      "source": _text(r.get(SOURCE_HEADER)) or name})
    info = {}
    if "정보" in wb.sheetnames:
        for r in wb["정보"].iter_rows(values_only=True):
            if r and r[0] is not None and len(r) > 1:
                info[str(r[0])] = r[1]
    wb.close()
    return {"name": name, "labels": labels, "audit": audit, "info": info}


def merge(exports: list[dict]) -> tuple[bytes, dict]:
    """Returns (xlsx bytes, report)."""
    if not exports:
        raise MergeError("합칠 파일이 없습니다")
    report = {"sources": [], "duplicates": 0, "conflicts": [], "sha_mismatch": []}

    # labels: one per (file, user). Same pair in several files -> keep the latest edit.
    best: dict[tuple, dict] = {}
    for ex in exports:
        report["sources"].append({"name": ex["name"], "labels": len(ex["labels"]),
                                  "audit": len(ex["audit"]),
                                  "exported_by": ex["info"].get("내보낸 사람", ""),
                                  "exported_at": ex["info"].get("생성 일시", ""),
                                  "dataset": ex["info"].get("데이터 폴더", "")})
        for l in ex["labels"]:
            key = (l["file"], l["user_id"])
            old = best.get(key)
            if old is None:
                best[key] = l
                continue
            report["duplicates"] += 1
            same = (old["has_beep"], old.get("start_s"), old.get("end_s"), old["memo"], old["reviewed"]) == \
                   (l["has_beep"], l.get("start_s"), l.get("end_s"), l["memo"], l["reviewed"])
            newer = l["updated_at"] > old["updated_at"]
            if not same:
                keep, drop = (l, old) if newer else (old, l)
                report["conflicts"].append(
                    f"{l['file']} / {l['user_id']}: '{keep['source']}'({keep['updated_at']}, {keep['region']}) 사용, "
                    f"'{drop['source']}'({drop['updated_at']}, {drop['region']}) 제외")
            if newer:
                best[key] = l
    labels = sorted(best.values(), key=lambda l: (l["file"], l["user_id"]))

    # same file name but different audio content on different PCs?
    shas = defaultdict(set)
    for l in labels:
        if l["sha256"]:
            shas[l["file"]].add(l["sha256"])
    report["sha_mismatch"] = sorted(f for f, s in shas.items() if len(s) > 1)

    # audit: union without duplicates, in time order
    seen, audit = set(), []
    for ex in exports:
        for a in ex["audit"]:
            key = (a["ts"], a["user_id"], a["file"], a["action"], a["before"], a["after"], a["detail"])
            if key in seen:
                continue
            seen.add(key)
            audit.append(a)
    audit.sort(key=lambda a: (a["ts"], a["user_id"]))

    users: dict[str, dict] = {}
    for l in labels:
        users.setdefault(l["user_id"], {"display_name": l["display_name"], "created_at": "", "last_login": ""})
    for a in audit:
        if not a["user_id"]:
            continue
        u = users.setdefault(a["user_id"], {"display_name": a["display_name"] or a["user_id"],
                                            "created_at": "", "last_login": ""})
        if a["action"] == "아이디 생성" and (not u["created_at"] or a["ts"] < u["created_at"]):
            u["created_at"] = a["ts"]
        if a["action"] == "로그인" and a["ts"] > u["last_login"]:
            u["last_login"] = a["ts"]
    users = dict(sorted(users.items()))

    files, auto_by_file = {}, {}
    for l in labels:
        files.setdefault(l["file"], l.get("file_duration_s"))
        if l["auto_region"]:
            auto_by_file.setdefault(l["file"], l["auto_region"])
    file_list = sorted(files.items())

    src_text = "\n".join(f"{s['name']}: 라벨 {s['labels']}개, 이력 {s['audit']}개"
                         f" (내보낸 사람 {s['exported_by'] or '-'}, {s['exported_at'] or '-'})"
                         for s in report["sources"])
    info = [("합친 일시", now()), ("툴 버전", __version__), ("합친 파일", src_text),
            ("중복 라벨", f"{report['duplicates']}개 (같은 파일·같은 작업자 → 최종 수정 일시가 가장 늦은 것 사용)"),
            ("내용이 다른 중복", "\n".join(report["conflicts"]) or "없음"),
            ("오디오 불일치 경고", ("같은 파일 이름인데 PC마다 오디오 내용(SHA-256)이 다름: "
                                  + ", ".join(report["sha_mismatch"])) if report["sha_mismatch"] else "없음"),
            *RULE_ROWS,
            ("파일 수", len(file_list)), ("라벨 수(작업자×파일)", len(labels)),
            ("작업자 수", len(users)), ("작업 이력 수", len(audit)), SHEET_HELP]
    body = write_workbook(labels, audit, users, file_list, auto_by_file,
                          "Beep Labeler 합친 결과", info, with_source=True)
    report.update({"labels": len(labels), "audit": len(audit), "users": list(users), "files": len(file_list)})
    return body, report


def merge_paths(paths: list[str | Path]) -> tuple[bytes, dict]:
    return merge([read_export(Path(p).read_bytes(), Path(p).name) for p in paths])
