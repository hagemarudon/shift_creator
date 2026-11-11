"""設定ファイルと、Googleフォームの回答（CSV・Excel・スプレッドシート）の読み込み。

フォームの「チェックボックス（グリッド）」の回答は、列見出しが「質問 [行の見出し]」、
値が「選んだ列の見出し」をカンマで区切ったものになる。行と列のどちらか一方が時刻（10:00 や 10:00-10:30）、
もう一方が日付（11/16(月) など）であれば読める（向きはどちらでもよい）。
"""
from __future__ import annotations

import datetime as dt
import io
import re
import tomllib
import unicodedata
from pathlib import Path

import pandas as pd

from .model import WEEKDAYS, Problem, Rule, Settings, Staff, date_label, to_min

LOCAL_CONFIG_NAME = "config.local.toml"

GRID_COL = re.compile(r"^(.*?)\s*\[(.+)\]\s*$")
TIME = re.compile(r"(\d{1,2})[:：時](\d{2})?")
DATE = re.compile(r"(?:(\d{4})[/\-年])?(\d{1,2})[/\-月](\d{1,2})")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
SPLIT = re.compile(r"\s*[,、，]\s*")

NAME_KEYS = ("氏名", "名前")
MAX_DAYS_KEYS = ("最大日数",)
TARGET_KEYS = ("希望勤務時間", "希望時間")
NOTE_KEYS = ("備考", "要望")
TIMESTAMP_KEYS = ("タイムスタンプ", "Timestamp")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_settings(path: str | Path, overlay: str | Path | None = None) -> Settings:
    """設定ファイルを読む。overlay（画面で保存した config.local.toml）があれば、その値で上書きする。"""
    with open(path, "rb") as f:
        c = tomllib.load(f)
    if overlay is not None and Path(overlay).exists():
        with open(overlay, "rb") as f:
            c = _merge(c, tomllib.load(f))
    return Settings(
        slot_minutes=int(c["slot_minutes"]),
        min_shift_minutes=round(float(c["shift"]["min_hours"]) * 60),
        normal_max_minutes=round(float(c["shift"].get("normal_max_hours", c["shift"]["max_hours"])) * 60),
        max_shift_minutes=round(float(c["shift"]["max_hours"]) * 60),
        rules=[Rule(str(r["days"]), str(r["start"]), str(r["end"]), int(r["people"])) for r in c["rules"]],
        leader_required=bool(c.get("leader", {}).get("required", False)),
        time_limit_seconds=float(c["solver"]["time_limit_seconds"]),
        weights={k: int(v) for k, v in c["weights"].items()},
    )


# ===== 回答ファイルを読む =====
def read_file(name: str, data: bytes) -> dict[str, pd.DataFrame]:
    """CSV か Excel を読む。シート名 → 表。CSV は1シート扱い。"""
    if name.lower().endswith(".csv"):
        for enc in ("utf-8-sig", "cp932"):
            try:
                return {"フォームの回答": pd.read_csv(io.BytesIO(data), dtype=str, encoding=enc, keep_default_na=False)}
            except UnicodeDecodeError:
                continue
        raise ValueError("CSVの文字コードを読めませんでした。UTF-8で保存し直してください。")
    return pd.read_excel(io.BytesIO(data), sheet_name=None, dtype=str, keep_default_na=False)


def _find(columns, keys) -> str | None:
    for c in columns:
        if any(k in str(c) for k in keys):
            return c
    return None


def name_key(name: str) -> str:
    """同じ人かどうかを見るための氏名。空白と全角・半角の違いを無視する。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(name)))


def is_response_sheet(df: pd.DataFrame) -> bool:
    return _find(df.columns, NAME_KEYS) is not None and any(GRID_COL.match(str(c)) for c in df.columns)


def pick_response_sheet(dfs: dict[str, pd.DataFrame]) -> str | None:
    """フォームの回答のシートを選ぶ。「フォームの回答」で始まるシートを優先する。"""
    names = [n for n, df in dfs.items() if is_response_sheet(df)]
    names.sort(key=lambda n: not n.startswith("フォームの回答"))
    return names[0] if names else None


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none") else s


def _number(v) -> float | None:
    m = NUMBER.search(_cell(v).translate(str.maketrans("０１２３４５６７８９．", "0123456789.")))
    return float(m.group()) if m else None


HAS_TIME = re.compile(r"\d\s*[:：時]")


def parse_times(label: str) -> tuple[int, int | None] | None:
    """「10:00」→(600, None)、「10:00-11:00」「10時〜11時」→(600, 660)。時刻がなければ None。"""
    if not HAS_TIME.search(label):
        return None
    found = [int(h) * 60 + int(m or 0) for h, m in TIME.findall(label)]
    return found[0], (found[1] if len(found) > 1 else None)


def parse_date(label: str, ref: dt.date) -> dt.date | None:
    """「11/16(月)」などを日付にする。年が書いてなければ ref に一番近い年にする。時刻の見出しは None。"""
    m = DATE.search(label)
    if not m or HAS_TIME.search(label):
        return None
    y, mo, d = m.groups()
    try:
        if y:
            return dt.date(int(y), int(mo), int(d))
        cands = []
        for k in (-1, 0, 1):
            try:
                cands.append(dt.date(ref.year + k, int(mo), int(d)))
            except ValueError:  # 2/29 など
                pass
    except ValueError:
        return None
    return min(cands, key=lambda c: abs((c - ref).days)) if cands else None


def _split_time_date(a: str, b: str, ref: dt.date):
    """グリッドの行見出しと選んだ列見出しの、どちらが時刻でどちらが日付かを決める。"""
    for t_label, d_label in ((a, b), (b, a)):
        times, day = parse_times(t_label), parse_date(d_label, ref)
        if times is not None and day is not None:
            return times, day
    return None, None


def _ref_date(row, ts_col, default: dt.date) -> dt.date:
    if ts_col:
        m = DATE.search(_cell(row[ts_col]))
        if m and m.group(1):
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return default


def parse_responses(df: pd.DataFrame, slot_minutes: int, today: dt.date | None = None) -> tuple[list[Staff], list[str]]:
    """フォームの回答の表からスタッフと出勤できる時間を読む。同じ氏名は最後の回答を使う。"""
    today = today or dt.date.today()
    notes: list[str] = []
    # 氏名の質問を記述からプルダウンに作り直すと、氏名の列が2つになる（古い回答は前の列、新しい回答は後の列）
    name_pos = [i for i, c in enumerate(df.columns) if any(k in str(c) for k in NAME_KEYS)]
    if not name_pos:
        return [], ["「氏名」の列が見つかりません。Googleフォームの回答のシートを読み込んでください。"]
    grid_cols = [(c, GRID_COL.match(str(c)).group(2).strip()) for c in df.columns if GRID_COL.match(str(c))]
    if not grid_cols:
        return [], ["出勤できる時間（チェックボックスのグリッド）の列が見つかりません。"]
    ts_col = _find(df.columns, TIMESTAMP_KEYS)
    max_col, target_col, note_col = (_find(df.columns, k) for k in (MAX_DAYS_KEYS, TARGET_KEYS, NOTE_KEYS))

    by_name: dict[str, Staff] = {}
    unreadable: set[str] = set()
    for n, (_, row) in enumerate(df.iterrows()):
        name = next((v for v in (_cell(df.iat[n, i]) for i in name_pos) if v), "")
        if not name:
            continue
        key = name_key(name)
        if key in by_name:
            notes.append(f"{name} さんは2回以上答えています。最後の回答を使います。")
        ref = _ref_date(row, ts_col, today)
        staff = Staff(name=name, answered_at=_cell(row[ts_col]) if ts_col else "")
        for col, row_label in grid_cols:
            for chosen in SPLIT.split(_cell(row[col])):
                if not chosen:
                    continue
                times, day = _split_time_date(row_label, chosen, ref)
                if times is None or day is None:
                    unreadable.add(f"{row_label} / {chosen}")
                    continue
                start, end = times
                end = end if end is not None and end > start else start + slot_minutes
                for t in range(start, end, slot_minutes):
                    staff.available.add((day, t))
        if max_col:
            n = _number(row[max_col])
            staff.max_days = int(n) if n is not None else None
        if target_col:
            staff.target_hours = _number(row[target_col])
        if note_col:
            staff.note = _cell(row[note_col])
        by_name.pop(key, None)  # 答え直した人は並びも最後にする
        by_name[key] = staff
    if unreadable:
        sample = "、".join(sorted(unreadable)[:3])
        notes.append(f"日付か時刻として読めない選択肢がありました（例：{sample}）。グリッドの行・列は「10:00」と「11/16(月)」の形にしてください。")
    for s in by_name.values():
        if not s.available:
            notes.append(f"{s.name} さんは出勤できる時間が1つもありません。")
    dup = [n for n in notes if "2回以上" in n]
    notes = sorted(set(dup), key=dup.index) + [n for n in notes if n not in dup]
    return list(by_name.values()), notes


# ===== 必要人数 =====
WANT, AVOID, NO_REQUEST = "入れたい", "入れない", "－"

def rule_matches(days: str, d: dt.date, holidays: set[dt.date]) -> bool:
    s = days.strip()
    hol = d.weekday() == 6 or d in holidays
    if s in ("毎日", "全日", ""):
        return True
    if s == "平日":
        return d.weekday() < 5 and not hol
    if s in ("日祝", "日曜祝日"):
        return hol
    if s in ("土日祝", "土日"):
        return d.weekday() >= 5 or hol
    if s == "祝":
        return d in holidays
    if DATE.search(s):
        return any(parse_date(p, d) == d for p in SPLIT.split(s) if p)
    return WEEKDAYS[d.weekday()] in s


def check_rules(rules: list[Rule]) -> list[str]:
    errors = []
    for i, r in enumerate(rules, 1):
        try:
            s, e = to_min(r.start), to_min(r.end)
        except (ValueError, IndexError):
            errors.append(f"必要人数の{i}行目：時刻は「10:00」の形で入力してください。")
            continue
        if e <= s:
            errors.append(f"必要人数の{i}行目：終了が開始より前になっています。")
        if r.people < 0:
            errors.append(f"必要人数の{i}行目：人数は0以上にしてください。")
        d = r.days.strip()
        if d and d not in ("毎日", "全日", "平日", "日祝", "日曜祝日", "土日祝", "土日", "祝") and not DATE.search(d) \
                and not all(ch in WEEKDAYS for ch in d):
            errors.append(f"必要人数の{i}行目：曜日「{d}」が読めません（毎日・平日・土日祝・月水金・11/23 など）。")
    return errors


def required_staff(rules: list[Rule], dates: list[dt.date], slot: int, holidays: set[dt.date]):
    """(日付, 枠) → 必要人数 と、期間を通して使う枠の一覧を返す。"""
    starts = [to_min(r.start) for r in rules]
    ends = [to_min(r.end) for r in rules]
    if not rules:
        return {}, []
    first = min(starts) // slot * slot
    times = list(range(first, max(ends), slot))
    req = {}
    for d in dates:
        for t in times:
            n = 0
            for r, s, e in zip(rules, starts, ends):
                if s <= t < e and rule_matches(r.days, d, holidays):
                    n = r.people
            req[(d, t)] = n
    return req, times


def build_problem(staff: list[Staff], settings: Settings, dates: list[dt.date],
                  requests: dict[tuple[str, dt.date], str] | None = None) -> Problem:
    """requests は日ごとの指定（(氏名, 日付) → 「入れたい」「入れない」）。"""
    req, times = required_staff(settings.rules, dates, settings.slot_minutes, settings.holidays)
    requests = requests or {}
    return Problem(settings=settings, staff=staff, dates=dates, times=times, required=req,
                   want={k for k, v in requests.items() if v == WANT},
                   avoid={k for k, v in requests.items() if v == AVOID})


def response_dates(staff: list[Staff]) -> list[dt.date]:
    days = {d for s in staff for d, _ in s.available}
    if not days:
        return []
    lo, hi = min(days), max(days)
    return [lo + dt.timedelta(n) for n in range((hi - lo).days + 1)]


# ===== スタッフ表（画面で直す最大日数・上限時間・責任者） =====
STAFF_COLS = ["使う", "氏名", "回答日時", "責任者", "最大日数", "希望時間", "上限時間", "備考"]


def staff_frame(staff: list[Staff]) -> pd.DataFrame:
    return pd.DataFrame([{
        "使う": True, "氏名": s.name, "回答日時": s.answered_at, "責任者": s.leader, "最大日数": s.max_days,
        "希望時間": s.target_hours, "上限時間": s.max_hours, "備考": s.note,
    } for s in staff], columns=STAFF_COLS)


def apply_staff_frame(staff: list[Staff], df: pd.DataFrame) -> list[Staff]:
    """画面で直したスタッフ表を反映した新しいリストを返す（氏名で対応させる）。

    「使う」を外した人は、シフトに入れないので返すリストから除く。
    """
    rows = {str(r["氏名"]): r for _, r in df.iterrows()}
    out = []
    for s in staff:
        r = rows.get(s.name)
        if r is None:
            out.append(s)
            continue
        if "使う" in r and not (bool(r["使う"]) if not pd.isna(r["使う"]) else False):
            continue
        out.append(Staff(
            name=s.name, available=s.available, note=s.note, answered_at=s.answered_at,
            leader=bool(r["責任者"]) if not pd.isna(r["責任者"]) else False,
            max_days=int(r["最大日数"]) if _number(r["最大日数"]) is not None else None,
            target_hours=_number(r["希望時間"]),
            max_hours=_number(r["上限時間"]),
        ))
    return out


# ===== 日ごとの指定（シフト作成者が「この日はこの人を入れたい／入れない」を決める） =====
def request_frame(names: list[str], dates: list[dt.date],
                  requests: dict[tuple[str, dt.date], str]) -> pd.DataFrame:
    """スタッフ×日付の表。セルは「入れたい」「入れない」か「－」（指定なし）。氏名は回答の氏名をそのまま使う。"""
    return pd.DataFrame([{"氏名": n, **{date_label(d): requests.get((n, d), NO_REQUEST) for d in dates}}
                         for n in names],
                        columns=["氏名"] + [date_label(d) for d in dates])


def read_request_frame(df: pd.DataFrame, dates: list[dt.date]) -> dict[tuple[str, dt.date], str]:
    out = {}
    for _, r in df.iterrows():
        for d in dates:
            v = _cell(r.get(date_label(d)))
            if v in (WANT, AVOID):
                out[str(r["氏名"]), d] = v
    return out


def update_requests(old: dict[tuple[str, dt.date], str], df: pd.DataFrame, names: list[str],
                    dates: list[dt.date]) -> dict[tuple[str, dt.date], str]:
    """表に出ている人と日付の指定を表の内容で置きかえる。表にない人・日付の指定は残す（期間を戻したときのため）。"""
    shown_names, shown_dates = set(names), set(dates)
    out = {k: v for k, v in old.items() if k[0] not in shown_names or k[1] not in shown_dates}
    out.update(read_request_frame(df, dates))
    return out


def request_notes(problem: Problem) -> list[str]:
    """「入れたい」にしたのに、その日の営業時間に出勤できる時間がない人。"""
    notes = []
    by_name = {s.name: s for s in problem.staff}
    for name, d in sorted(problem.want, key=lambda k: (k[1], k[0])):
        st = by_name.get(name)
        if st is None or d not in problem.dates:
            continue
        if not any(problem.required.get((d, t), 0) > 0 and (d, t) in st.available for t in problem.times):
            notes.append(f"{name} さんは {date_label(d)} に出勤できる時間がない（フォームの回答）ので、入れられません。")
    return notes
