"""シフトを表にして、Excel に書き出す（Googleスプレッドシートへは google_sheets.write_sheets）。"""
from __future__ import annotations

import datetime as dt
import io
from collections import defaultdict

import pandas as pd
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .model import Problem, Result, date_label, to_hhmm

SHEET_TABLE = "シフト表"
SHEET_DAILY = "日別"
SHEET_PERSON = "個人別"
SHEET_SUMMARY = "集計"
SHEET_SHORT = "人数不足"
SHEET_REQUEST = "日ごとの指定"

WORK_FILL = "#93c5fd"
LONG_FILL = "#fdba74"   # 原則の長さを超えた勤務
SHORT_FILL = "#fca5a5"
WEEKEND_FILL = {5: "#dbeafe", 6: "#fee2e2"}


def _by_person_day(result: Result):
    out = defaultdict(list)
    for s in result.shifts:
        out[s.name, s.date].append(s)
    return out


def _is_long(problem: Problem, minutes: int) -> bool:
    return minutes > problem.settings.normal_max_minutes


def assigned_count(problem: Problem, result: Result) -> dict[tuple[dt.date, int], int]:
    cnt = defaultdict(int)
    for s in result.shifts:
        for t in range(s.start, s.end, problem.settings.slot_minutes):
            cnt[s.date, t] += 1
    return cnt


def shift_table(problem: Problem, result: Result) -> pd.DataFrame:
    """スタッフ×日付の一覧（セルは 10:00-12:00）。右端に日数と合計時間。"""
    pd_map = _by_person_day(result)
    rows = []
    for st in problem.staff:
        row = {"氏名": st.name}
        hours = 0.0
        days = 0
        for d in problem.dates:
            items = pd_map.get((st.name, d), [])
            row[date_label(d)] = "、".join(s.label for s in items)
            hours += sum(s.hours for s in items)
            days += bool(items)
        row["日数"] = days
        row["合計時間"] = hours
        rows.append(row)
    return pd.DataFrame(rows).set_index("氏名")


def day_grid(problem: Problem, result: Result, d: dt.date) -> pd.DataFrame:
    """その日の 人×枠（時間刻み） の表。働く枠は ■。下に配置人数・必要人数。"""
    slot = problem.settings.slot_minutes
    shifts = sorted((s for s in result.shifts if s.date == d), key=lambda s: (s.start, s.end))
    cols = [to_hhmm(t) for t in problem.times]
    rows = {}
    for s in shifts:
        rows[f"{s.name}（{s.label}）"] = ["■" if s.start <= t < s.end else "" for t in problem.times]
    cnt = assigned_count(problem, result)
    rows["配置人数"] = [cnt.get((d, t), 0) for t in problem.times]
    rows["必要人数"] = [problem.required.get((d, t), 0) for t in problem.times]
    df = pd.DataFrame.from_dict(rows, orient="index", columns=cols)
    df.index.name = date_label(d)
    return df


def person_table(problem: Problem, result: Result) -> pd.DataFrame:
    rows = [{"氏名": s.name, "日付": date_label(s.date), "開始": to_hhmm(s.start), "終了": to_hhmm(s.end),
             "時間": s.hours, "延長": "延長" if _is_long(problem, s.end - s.start) else ""}
            for s in sorted(result.shifts, key=lambda s: ([x.name for x in problem.staff].index(s.name), s.date, s.start))]
    return pd.DataFrame(rows, columns=["氏名", "日付", "開始", "終了", "時間", "延長"])


def summary_table(problem: Problem, result: Result) -> pd.DataFrame:
    slot_h = problem.settings.slot_minutes / 60
    rows = []
    for st in problem.staff:
        mine = [s for s in result.shifts if s.name == st.name]
        open_avail = sum(1 for k in st.available if problem.required.get(k, 0) > 0)
        rows.append({
            "氏名": st.name,
            "出勤日数": len({s.date for s in mine}),
            "合計時間": sum(s.hours for s in mine),
            "延長した日": sum(_is_long(problem, s.end - s.start) for s in mine),
            "出勤できる時間": open_avail * slot_h,
            "最大日数": st.max_days,
            "希望時間": st.target_hours,
            "上限時間": st.max_hours,
            "責任者": "○" if st.leader else "",
        })
    return pd.DataFrame(rows)


def shortage_table(problem: Problem, result: Result) -> pd.DataFrame:
    """人数が足りない時間帯。同じ不足人数が続く枠はまとめる。"""
    slot = problem.settings.slot_minutes
    cnt = assigned_count(problem, result)
    rows = []
    for d in problem.dates:
        run = None
        for t in problem.times + [None]:
            key = (d, t)
            short = result.shortage.get(key, 0) if t is not None else 0
            lead = key in result.leader_missing if t is not None else False
            sig = (short, lead) if (short or lead) else None
            if run and (sig != run["sig"] or t != run["end"]):
                avail = sum(1 for st in problem.staff if (d, run["start"]) in st.available)
                rows.append({"日付": date_label(d), "時間帯": f"{to_hhmm(run['start'])}-{to_hhmm(run['end'])}",
                             "必要人数": run["req"], "配置人数": run["cnt"], "不足": run["sig"][0],
                             "責任者": "不在" if run["sig"][1] else "", "出勤できる人": avail})
                run = None
            if sig and run is None:
                run = {"sig": sig, "start": t, "end": t + slot, "req": problem.required[key], "cnt": cnt.get(key, 0)}
            elif sig and run:
                run["end"] = t + slot
    return pd.DataFrame(rows, columns=["日付", "時間帯", "必要人数", "配置人数", "不足", "責任者", "出勤できる人"])


def request_table(problem: Problem, result: Result) -> pd.DataFrame:
    """日ごとの指定と、そのとおりになったか。"""
    worked = _by_person_day(result)
    order = {st.name: i for i, st in enumerate(problem.staff)}
    rows = []
    for kind, keys in (("入れたい", problem.want), ("入れない", problem.avoid)):
        for name, d in keys:
            if name not in order or d not in problem.dates:
                continue
            items = worked.get((name, d), [])
            rows.append({"日付": d, "氏名": name, "指定": kind, "結果": "、".join(s.label for s in items) or "入っていない",
                         "_ok": bool(items) == (kind == "入れたい")})
    rows.sort(key=lambda r: (r["日付"], order[r["氏名"]]))
    df = pd.DataFrame(rows, columns=["日付", "氏名", "指定", "結果", "_ok"])
    df["日付"] = [date_label(d) for d in df["日付"]]
    df.insert(4, "指定どおり", ["○" if ok else "×" for ok in df.pop("_ok")])
    return df


def all_sheets(problem: Problem, result: Result) -> dict[str, pd.DataFrame]:
    """Googleスプレッドシートに書き込む表。"""
    sheets = {
        f"{SHEET_TABLE}": shift_table(problem, result).reset_index(),
        f"{SHEET_PERSON}": person_table(problem, result),
        f"{SHEET_SUMMARY}": summary_table(problem, result),
        f"{SHEET_SHORT}": shortage_table(problem, result),
    }
    if problem.want or problem.avoid:
        sheets[SHEET_REQUEST] = request_table(problem, result)
    return sheets


# ===== Excel =====
THIN = Side(style="thin", color="999999")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _fill(color: str) -> PatternFill:
    return PatternFill("solid", fgColor=color.lstrip("#"))


def _write_frame(ws, df: pd.DataFrame, index: bool = False) -> None:
    data = df.reset_index() if index else df
    ws.append([str(c) for c in data.columns])
    for row in data.itertuples(index=False):
        ws.append([None if (isinstance(v, float) and pd.isna(v)) else v for v in row])
    for c in ws[1]:
        c.font = Font(bold=True)
        c.fill = _fill("#e5e7eb")
    for col in range(1, ws.max_column + 1):
        width = max(len(str(ws.cell(r, col).value or "")) for r in range(1, ws.max_row + 1))
        ws.column_dimensions[get_column_letter(col)].width = min(max(8, width * 1.6 + 2), 40)


def _write_table(ws, problem: Problem, result: Result) -> None:
    df = shift_table(problem, result)
    _write_frame(ws, df, index=True)
    ws.freeze_panes = "B2"
    for j, d in enumerate(problem.dates, start=2):
        color = WEEKEND_FILL.get(6 if d in problem.settings.holidays else d.weekday())
        if color:
            ws.cell(1, j).fill = _fill(color)
        ws.column_dimensions[get_column_letter(j)].width = 13
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for c in row:
            c.border = BOX
            c.alignment = Alignment(horizontal="center", vertical="center")


def _write_daily(ws, problem: Problem, result: Result) -> None:
    """日ごとに、人×枠（時間刻み）のマスを塗った表を縦に並べる。"""
    slot = problem.settings.slot_minutes
    cnt = assigned_count(problem, result)
    ws.column_dimensions["A"].width = 24
    for j in range(len(problem.times)):
        ws.column_dimensions[get_column_letter(j + 2)].width = 6
    labels = set(_label_slots(problem))
    r = 1
    for d in problem.dates:
        ws.cell(r, 1, date_label(d)).font = Font(bold=True, size=12)
        for j, t in enumerate(problem.times, start=2):
            c = ws.cell(r, j, to_hhmm(t) if j - 2 in labels else "")
            c.font = Font(size=9)
        r += 1
        for s in sorted((s for s in result.shifts if s.date == d), key=lambda s: (s.start, s.end)):
            ws.cell(r, 1, f"{s.name}  {s.label}").border = BOX
            color = LONG_FILL if _is_long(problem, s.end - s.start) else WORK_FILL
            for j, t in enumerate(problem.times, start=2):
                c = ws.cell(r, j)
                c.border = BOX
                if s.start <= t < s.end:
                    c.fill = _fill(color)
            r += 1
        if True:
            ws.cell(r, 1, "配置／必要").font = Font(size=9)
            for j, t in enumerate(problem.times, start=2):
                req = problem.required.get((d, t), 0)
                if not req:
                    continue
                c = ws.cell(r, j, f"{cnt.get((d, t), 0)}/{req}")
                c.font = Font(size=8)
                c.alignment = Alignment(horizontal="center")
                if result.shortage.get((d, t)):
                    c.fill = _fill(SHORT_FILL)
        r += 2


def to_xlsx(problem: Problem, result: Result) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_TABLE
    _write_table(ws, problem, result)
    _write_daily(wb.create_sheet(SHEET_DAILY), problem, result)
    _write_frame(wb.create_sheet(SHEET_PERSON), person_table(problem, result))
    _write_frame(wb.create_sheet(SHEET_SUMMARY), summary_table(problem, result))
    _write_frame(wb.create_sheet(SHEET_SHORT), shortage_table(problem, result))
    if problem.want or problem.avoid:
        _write_frame(wb.create_sheet(SHEET_REQUEST), request_table(problem, result))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _label_slots(problem: Problem) -> list[int]:
    """時刻の見出しを書く枠の番号。刻みが1時間より短ければ毎正時、そうでなければ全部の枠。"""
    slot = problem.settings.slot_minutes
    out = [i for i, t in enumerate(problem.times) if slot >= 60 or t % 60 == 0]
    return out or list(range(0, len(problem.times), max(1, 60 // slot)))


# ===== 画面用 HTML =====
def day_html(problem: Problem, result: Result, d: dt.date) -> str:
    """その日のシフトを横棒で描く。下の行は配置人数／必要人数（足りない枠は赤）。"""
    cnt = assigned_count(problem, result)
    times = problem.times
    marks = _label_slots(problem) + [len(times)]
    head = "".join(f"<th style='font-weight:normal;font-size:11px;text-align:left' colspan={b - a}>{to_hhmm(times[a])}</th>"
                   for a, b in zip(marks, marks[1:]))
    first_pad = f"<th colspan={marks[0]}></th>" if marks[0] else ""
    rows = []
    for s in sorted((s for s in result.shifts if s.date == d), key=lambda s: (s.start, s.end)):
        color = LONG_FILL if _is_long(problem, s.end - s.start) else WORK_FILL
        tds = "".join(f"<td style='background:{color if s.start <= t < s.end else 'transparent'};"
                      f"border-left:1px solid {'rgba(128,128,128,.5)' if t % 60 == 0 else 'rgba(128,128,128,.2)'}'></td>" for t in times)
        rows.append(f"<tr><th style='text-align:left;white-space:nowrap;font-weight:normal;padding-right:8px'>"
                    f"{s.name} <small style='opacity:.7'>{s.label}</small></th>{tds}</tr>")
    tds = []
    for t in times:
        req = problem.required.get((d, t), 0)
        n = cnt.get((d, t), 0)
        bg = SHORT_FILL if result.shortage.get((d, t)) else "transparent"
        fg = '#111' if result.shortage.get((d, t)) else 'inherit'
        tds.append(f"<td style='background:{bg};font-size:10px;text-align:center;color:{fg}'>"
                   f"{f'{n}/{req}' if req else ''}</td>")
    rows.append(f"<tr><th style='text-align:left;font-weight:normal;font-size:11px'>配置／必要</th>{''.join(tds)}</tr>")
    return ("<div style='overflow-x:auto'><table style='border-collapse:collapse;width:100%;table-layout:fixed'>"
            f"<colgroup><col style='width:190px'>{'<col>' * len(times)}</colgroup>"
            f"<tr><th></th>{first_pad}{head}</tr>{''.join(rows)}</table></div>")
