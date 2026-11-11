"""NF用の表と Excel。元のシフト作成（shift/output.py）の表に、会計係の印と「会計不在」を足す。"""
from __future__ import annotations

import datetime as dt
import io

import pandas as pd
from openpyxl.styles import Alignment, Font

from shift import output as base
from shift.model import Problem, Result, Shift, date_label, to_hhmm

ACC_MARK = "【会計】"
SHEET_ANSWERS = "回答の読み取り"


def accountant_names(problem: Problem) -> set[str]:
    return {s.name for s in problem.staff if s.leader}


def display_result(problem: Problem, result: Result) -> Result:
    """日別の表で会計係が分かるように、名前に【会計】を付けた結果（表示用）。"""
    acc = accountant_names(problem)
    return Result(status=result.status, shortage=result.shortage, leader_missing=result.leader_missing,
                  shifts=[Shift(s.name + ACC_MARK if s.name in acc else s.name, s.date, s.start, s.end)
                          for s in result.shifts])


def shift_table(problem: Problem, result: Result) -> pd.DataFrame:
    """スタッフ×日付の一覧。左に会計の印。"""
    df = base.shift_table(problem, result)
    acc = accountant_names(problem)
    df.insert(0, "会計", ["○" if n in acc else "" for n in df.index])
    return df


def person_table(problem: Problem, result: Result) -> pd.DataFrame:
    df = base.person_table(problem, result)
    acc = accountant_names(problem)
    df.insert(1, "会計", ["○" if n in acc else "" for n in df["氏名"]])
    return df


def summary_table(problem: Problem, result: Result) -> pd.DataFrame:
    df = base.summary_table(problem, result).rename(columns={"責任者": "会計"})
    return df


def shortage_table(problem: Problem, result: Result) -> pd.DataFrame:
    """人数が足りない時間帯と、会計係がいない時間帯。同じ状態が続く枠はまとめる。"""
    slot = problem.settings.slot_minutes
    cnt = base.assigned_count(problem, result)
    rows = []
    for d in problem.dates:
        run = None
        for t in problem.times + [None]:
            key = (d, t)
            short = result.shortage.get(key, 0) if t is not None else 0
            miss = key in result.leader_missing if t is not None else False
            sig = (short, miss) if (short or miss) else None
            if run and (sig != run["sig"] or t != run["end"]):
                s0 = run["start"]
                avail = [st for st in problem.staff if (d, s0) in st.available]
                rows.append({"日付": date_label(d), "時間帯": f"{to_hhmm(s0)}-{to_hhmm(run['end'])}",
                             "必要人数": run["req"], "配置人数": run["cnt"], "不足": run["sig"][0],
                             "会計": "不在" if run["sig"][1] else "",
                             "出勤できる人": len(avail), "出勤できる会計": sum(st.leader for st in avail)})
                run = None
            if sig and run is None:
                run = {"sig": sig, "start": t, "end": t + slot, "req": problem.required[key], "cnt": cnt.get(key, 0)}
            elif sig and run:
                run["end"] = t + slot
    return pd.DataFrame(rows, columns=["日付", "時間帯", "必要人数", "配置人数", "不足", "会計", "出勤できる人",
                                       "出勤できる会計"])


def accountant_count(problem: Problem, result: Result) -> dict[tuple[dt.date, int], int]:
    acc = accountant_names(problem)
    out: dict[tuple[dt.date, int], int] = {}
    for s in result.shifts:
        if s.name in acc:
            for t in range(s.start, s.end, problem.settings.slot_minutes):
                out[s.date, t] = out.get((s.date, t), 0) + 1
    return out


def all_sheets(problem: Problem, result: Result) -> dict[str, pd.DataFrame]:
    """Googleスプレッドシートに書き込む表。"""
    return {
        base.SHEET_TABLE: shift_table(problem, result).reset_index(),
        base.SHEET_PERSON: person_table(problem, result),
        base.SHEET_SUMMARY: summary_table(problem, result),
        base.SHEET_SHORT: shortage_table(problem, result),
    }


def _write_table(ws, problem: Problem, result: Result) -> None:
    df = shift_table(problem, result)
    base._write_frame(ws, df, index=True)
    ws.freeze_panes = "C2"
    ws.column_dimensions["B"].width = 6
    for j, d in enumerate(problem.dates, start=3):
        color = base.WEEKEND_FILL.get(6 if d in problem.settings.holidays else d.weekday())
        if color:
            ws.cell(1, j).fill = base._fill(color)
        ws.column_dimensions[base.get_column_letter(j)].width = 13
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row):
        for c in row:
            c.border = base.BOX
            c.alignment = Alignment(horizontal="center", vertical="center")


def _write_daily(ws, problem: Problem, result: Result) -> None:
    """元の「日別」に、会計の人数の行を足す（会計がいない枠は赤）。"""
    base._write_daily(ws, problem, display_result(problem, result))
    acc = accountant_count(problem, result)
    # 元の表は日ごとに「見出し・勤務の行・配置／必要・空行2つ」。配置／必要の行の下に会計の行を書く
    r = 1
    for d in problem.dates:
        n_rows = sum(1 for s in result.shifts if s.date == d)
        r_count = r + 1 + n_rows
        r_acc = r_count + 1
        ws.cell(r_acc, 1, "会計").font = Font(size=9)
        for j, t in enumerate(problem.times, start=2):
            if not problem.required.get((d, t), 0):
                continue
            c = ws.cell(r_acc, j, acc.get((d, t), 0))
            c.font = Font(size=8)
            c.alignment = Alignment(horizontal="center")
            if (d, t) in result.leader_missing:
                c.fill = base._fill(base.SHORT_FILL)
        r = r_count + 2


def to_xlsx(problem: Problem, result: Result, answers: pd.DataFrame | None = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = base.SHEET_TABLE
    _write_table(ws, problem, result)
    _write_daily(wb.create_sheet(base.SHEET_DAILY), problem, result)
    base._write_frame(wb.create_sheet(base.SHEET_PERSON), person_table(problem, result))
    base._write_frame(wb.create_sheet(base.SHEET_SUMMARY), summary_table(problem, result))
    base._write_frame(wb.create_sheet(base.SHEET_SHORT), shortage_table(problem, result))
    if answers is not None:
        base._write_frame(wb.create_sheet(SHEET_ANSWERS), answers)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def day_html(problem: Problem, result: Result, d: dt.date) -> str:
    """その日のシフトの横棒（会計係は名前に【会計】）と、下に会計の人数（いない枠は赤）。"""
    html = base.day_html(problem, display_result(problem, result), d)
    acc = accountant_count(problem, result)
    tds = []
    for t in problem.times:
        req = problem.required.get((d, t), 0)
        bg = base.SHORT_FILL if (d, t) in result.leader_missing else "transparent"
        tds.append(f"<td style='background:{bg};font-size:10px;text-align:center;color:#111'>"
                   f"{acc.get((d, t), 0) if req else ''}</td>")
    row = f"<tr><th style='text-align:left;font-weight:normal;font-size:11px'>会計</th>{''.join(tds)}</tr>"
    return html.replace("</table></div>", row + "</table></div>")
