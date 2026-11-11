"""NF2026 シフト希望調査（Googleフォーム）の回答と、NF用の設定を読む。

フォームでは日ごとに「参加できない時間帯」を選んでもらう（何も選ばない日は終日参加できる）。
回答は次のどの形でも読める。

- 日ごとのチェックボックス：列見出しに日付（「11/20(金) 参加できない時間帯」「11月20日」など）、値は選んだ時間帯
  （「10:00-11:00, 13:00-14:00」「10時〜11時」「午前」「終日」など）
- チェックボックス（グリッド）：「質問 [11/20(金)]」で値が時間帯、または「質問 [10:00-11:00]」で値が日付
- 日付のない同じ題名の質問が日数ぶん並ぶとき（日ごとのセクションに同じ質問を置いた形）：左から順に期間の日に当てはめる

「あなたは会計ですか？」で「はい」と答えた人を会計係とする。
"""
from __future__ import annotations

import datetime as dt
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from shift.loader import DATE, _cell, _find, _merge, load_settings, parse_date
from shift.model import Settings, Staff, date_label, to_hhmm

LOCAL_CONFIG_NAME = "config.local.toml"
DAY_MINUTES = 24 * 60

GRID_COL = re.compile(r"^(.*?)\s*\[(.+)\]\s*$")
# 10:00 / 10：00 / 10時 / 10時30分 / 10時半
TIME_TOKEN = re.compile(r"(\d{1,2})\s*(?:[:：]\s*(\d{2})|時\s*(?:(\d{1,2})\s*分|(半))?)")
DUP_SUFFIX = re.compile(r"\.\d+$")  # pandas が同じ見出しに付ける「.1」「.2」
SPLIT = re.compile(r"\s*[,、，]\s*")

NAME_KEYS = ("氏名", "名前")
EMAIL_KEYS = ("メールアドレス", "Email", "email", "E-mail", "e-mail", "メール")
ACCOUNTANT_KEYS = ("会計",)
NOTE_KEYS = ("備考", "要望", "連絡")
TIMESTAMP_KEYS = ("タイムスタンプ", "Timestamp")
MAX_DAYS_KEYS = ("最大日数",)
TARGET_KEYS = ("希望勤務時間", "希望時間")
YES_WORDS = ("はい", "yes", "Yes", "YES", "○", "◯", "会計です", "true", "TRUE")
ALL_DAY_WORDS = ("終日", "一日中", "1日中", "全日", "丸一日", "ずっと")
AM_WORDS = ("午前",)
PM_WORDS = ("午後",)


@dataclass
class NFConfig:
    """NF用の設定。settings は元のシフト作成と同じ形。"""
    settings: Settings
    dates: list[dt.date]
    single_time_minutes: int = 60
    accountant_min: int = 1


@dataclass
class Answer:
    """1人分の回答。"""
    name: str
    email: str = ""
    accountant: bool = False
    answered_at: str = ""
    note: str = ""
    max_days: int | None = None
    target_hours: float | None = None
    # 日付 → 参加できない時間帯 (開始分, 終了分) の一覧
    busy: dict[dt.date, list[tuple[int, int]]] = field(default_factory=dict)

    def add_busy(self, d: dt.date, start: int, end: int) -> None:
        self.busy.setdefault(d, []).append((max(0, start), min(DAY_MINUTES, end)))

    def busy_label(self, d: dt.date) -> str:
        """例：「10:00-12:00、15:00-16:00」「終日」"""
        spans = merge_spans(self.busy.get(d, []))
        if spans == [(0, DAY_MINUTES)]:
            return "終日"
        return "、".join(f"{to_hhmm(s)}-{to_hhmm(e)}" for s, e in spans)


def merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


# ===== 設定 =====
def _to_date(v) -> dt.date:
    if isinstance(v, dt.date):
        return v
    return dt.date.fromisoformat(str(v).strip())


def load_nf_config(path: str | Path, overlay: str | Path | None = None) -> NFConfig:
    """NF用の設定を読む。overlay（画面で保存した config.local.toml）があれば、その値で上書きする。"""
    settings = load_settings(path, overlay)
    with open(path, "rb") as f:
        c = tomllib.load(f)
    if overlay is not None and Path(overlay).exists():
        with open(overlay, "rb") as f:
            c = _merge(c, tomllib.load(f))
    period = c.get("period", {})
    settings.holidays = {_to_date(d) for d in period.get("holidays", [])}
    settings.leader_required = False  # NFでは責任者ではなく会計係を入れる
    return NFConfig(
        settings=settings,
        dates=sorted({_to_date(d) for d in period.get("dates", [])}),
        single_time_minutes=int(c.get("form", {}).get("single_time_minutes", 60)),
        accountant_min=int(c.get("accountant", {}).get("min_people", 1)),
    )


def save_nf_config(cfg: NFConfig, path: str | Path) -> None:
    """画面で入力した条件を config.local.toml に保存する（Gitで管理しない）。"""
    from shift.settings_io import save_settings

    save_settings(cfg.settings, path)
    text = Path(path).read_text(encoding="utf-8")
    # [leader] は NF では使わないので書かない
    text = re.sub(r"\[leader\]\nrequired = \w+\n\n", "", text)
    dates = ", ".join(f'"{d.isoformat()}"' for d in cfg.dates)
    hols = ", ".join(f'"{d.isoformat()}"' for d in sorted(cfg.settings.holidays))
    text += ("\n[period]\n"
             f"dates = [{dates}]\n"
             f"holidays = [{hols}]\n\n"
             "[form]\n"
             f"single_time_minutes = {cfg.single_time_minutes}\n\n"
             "[accountant]\n"
             f"min_people = {cfg.accountant_min}\n")
    Path(path).write_text(text, encoding="utf-8")


# ===== 回答の1つの選択肢を読む =====
RANGE_HOURS = re.compile(r"(\d{1,2})\s*([〜~～\-－ー])\s*(\d{1,2})\s*時")


def _normalize(text: str) -> str:
    """「10-11時」→「10時-11時」"""
    return RANGE_HOURS.sub(r"\1時\2\3時", text)


def find_times(text: str) -> list[int]:
    text = _normalize(text)
    out = []
    for h, m, m2, half in TIME_TOKEN.findall(text):
        out.append(int(h) * 60 + int(m or m2 or (30 if half else 0)))
    return out


def strip_times(text: str) -> str:
    return TIME_TOKEN.sub(" ", _normalize(text))


def find_date(text: str, ref: dt.date) -> dt.date | None:
    """時刻を取りのぞいた残りから日付を1つ読む（2つ以上あれば None）。"""
    rest = strip_times(text)
    found = {parse_date(m.group(0), ref) for m in DATE.finditer(rest)}
    found.discard(None)
    return found.pop() if len(found) == 1 else None


def find_span(text: str, single_minutes: int) -> tuple[int, int] | None:
    """選択肢の時間帯（開始分, 終了分）。「終日」は 0:00〜24:00。読めなければ None。"""
    if any(w in text for w in ALL_DAY_WORDS):
        return 0, DAY_MINUTES
    times = find_times(text)
    if len(times) >= 2:
        s, e = times[0], times[1]
        return (s, e) if e > s else (s, s + single_minutes)
    if len(times) == 1:
        if re.search(r"[〜~～\-－ー]\s*$", text.strip()) or "以降" in text or "から" in text:
            return times[0], DAY_MINUTES  # 「17:00〜」「17時以降」
        if re.match(r"^\s*[〜~～]", text) or "まで" in text:
            return 0, times[0]  # 「〜11:00」「11時まで」
        return times[0], times[0] + single_minutes
    if any(w in text for w in AM_WORDS):
        return 0, 12 * 60
    if any(w in text for w in PM_WORDS):
        return 12 * 60, DAY_MINUTES
    return None


# ===== 列を見分ける =====
def _is_meta(col: str) -> bool:
    keys = NAME_KEYS + EMAIL_KEYS + ACCOUNTANT_KEYS + NOTE_KEYS + TIMESTAMP_KEYS + MAX_DAYS_KEYS + TARGET_KEYS
    return any(k in col for k in keys)


@dataclass
class BusyColumn:
    col: str
    question: str
    label: str | None  # グリッドの行の見出し
    day: dt.date | None = None  # 見出しから決まった日付（値の中に日付があればそちらを使う）


def busy_columns(df: pd.DataFrame, dates: list[dt.date], ref: dt.date) -> tuple[list[BusyColumn], list[str]]:
    """参加できない時間帯を答える列を探し、見出しから日付を決める。"""
    notes: list[str] = []
    cols: list[BusyColumn] = []
    dateless: dict[str, list[BusyColumn]] = {}
    for c in df.columns:
        col = str(c)
        if _is_meta(col):
            continue
        m = GRID_COL.match(col)
        q, label = (m.group(1), m.group(2).strip()) if m else (DUP_SUFFIX.sub("", col), None)
        values = [v for v in (_cell(x) for x in df[c]) if v]
        day = (find_date(label, ref) if label else None) or find_date(q, ref)
        has_time = (label is not None and find_span(label, 60) is not None) or any(
            find_span(p, 60) is not None or find_date(p, ref) for v in values for p in SPLIT.split(v))
        looks_busy = "参加" in q or "時間" in q or "シフト" in q or "都合" in q
        if day is None and not has_time and not looks_busy:
            continue
        bc = BusyColumn(col, q, label, day)
        cols.append(bc)
        if day is None and not (label and find_date(label, ref)):
            dateless.setdefault(q.strip(), []).append(bc)
    # 日付のない同じ題名の列が日数ぶんあれば、左から期間の日に当てはめる
    for q, group in dateless.items():
        has_date_values = any(find_date(p, ref) for bc in group for v in df[bc.col] for p in SPLIT.split(_cell(v)) if p)
        if has_date_values:
            continue  # 値に日付が入っている（グリッドで列が日付など）
        if len(group) == len(dates):
            for bc, d in zip(group, dates):
                bc.day = d
        else:
            notes.append(f"「{q}」の列がどの日のものか分かりません（{len(group)}列・期間は{len(dates)}日）。"
                         "質問の題名に「11/20(金)」のように日付を入れてください。")
    return cols, notes


def _ref_date(row, ts_col, default: dt.date) -> dt.date:
    if ts_col:
        m = DATE.search(_cell(row[ts_col]))
        if m and m.group(1):
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return default


def _number(v) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", _cell(v).translate(str.maketrans("０１２３４５６７８９．", "0123456789.")))
    return float(m.group()) if m else None


def is_response_sheet(df: pd.DataFrame) -> bool:
    return _find(df.columns, NAME_KEYS) is not None and _find(df.columns, ACCOUNTANT_KEYS) is not None


def pick_response_sheet(dfs: dict[str, pd.DataFrame]) -> str | None:
    """フォームの回答のシートを選ぶ。「フォームの回答」で始まるシートを優先する。"""
    names = [n for n, df in dfs.items() if is_response_sheet(df)]
    names.sort(key=lambda n: not n.startswith("フォームの回答"))
    return names[0] if names else None


def parse_answers(df: pd.DataFrame, dates: list[dt.date], single_minutes: int = 60) -> tuple[list[Answer], list[str]]:
    """フォームの回答の表を読む。同じ人（メールアドレス、無ければ氏名）が2回答えたら最後の回答を使う。"""
    notes: list[str] = []
    name_col = _find(df.columns, NAME_KEYS)
    if name_col is None:
        return [], ["「氏名」の列が見つかりません。Googleフォームの回答のシートを読み込んでください。"]
    acc_col = _find(df.columns, ACCOUNTANT_KEYS)
    if acc_col is None:
        notes.append("「あなたは会計ですか？」の列が見つかりません。会計係はスタッフの表で選んでください。")
    email_col = _find(df.columns, EMAIL_KEYS)
    ts_col = _find(df.columns, TIMESTAMP_KEYS)
    note_col = _find(df.columns, NOTE_KEYS)
    max_col, target_col = _find(df.columns, MAX_DAYS_KEYS), _find(df.columns, TARGET_KEYS)
    ref0 = dates[0] if dates else dt.date.today()
    cols, col_notes = busy_columns(df, dates, ref0)
    notes += col_notes
    if not cols:
        notes.append("参加できない時間帯の列が見つかりません。全員が終日参加できるものとして作ります。")

    period = set(dates)
    by_key: dict[str, Answer] = {}
    unreadable: set[str] = set()
    outside: set[str] = set()
    for _, row in df.iterrows():
        name = _cell(row[name_col])
        if not name:
            continue
        email = _cell(row[email_col]) if email_col else ""
        key = email.lower() or re.sub(r"\s+", "", name)
        if key in by_key:
            notes.append(f"{name} さんは2回以上答えています。最後の回答を使います。")
        ref = _ref_date(row, ts_col, ref0) if not dates else ref0
        a = Answer(name=name, email=email, answered_at=_cell(row[ts_col]) if ts_col else "",
                   accountant=bool(acc_col) and any(w in _cell(row[acc_col]) for w in YES_WORDS)
                   and "いいえ" not in _cell(row[acc_col]),
                   note=_cell(row[note_col]) if note_col else "")
        if max_col:
            n = _number(row[max_col])
            a.max_days = int(n) if n is not None else None
        if target_col:
            a.target_hours = _number(row[target_col])
        for bc in cols:
            raw = _cell(row[bc.col])
            if not raw:
                continue
            for chosen in SPLIT.split(raw):
                if not chosen:
                    continue
                day = find_date(chosen, ref) or bc.day
                span = find_span(chosen, single_minutes)
                if span is None and bc.label is not None:
                    span = find_span(bc.label, single_minutes)
                if day is None or span is None:
                    unreadable.add(chosen if bc.label is None else f"{bc.label} / {chosen}")
                    continue
                if period and day not in period:
                    outside.add(date_label(day))
                    continue
                a.add_busy(day, *span)
        by_key.pop(key, None)  # 答え直した人は並びも最後にする
        by_key[key] = a
    if unreadable:
        sample = "、".join(sorted(unreadable)[:3])
        notes.append(f"時間帯として読めない選択肢がありました（例：{sample}）。この選択肢は使っていません。"
                     "選択肢は「10:00-11:00」「10時〜11時」「終日」の形にしてください。")
    if outside:
        notes.append(f"期間の外の日付の回答は使っていません（{'、'.join(sorted(outside))}）。")
    answers = list(by_key.values())
    if answers and acc_col and not any(a.accountant for a in answers):
        notes.append("会計係（「あなたは会計ですか？」で「はい」）の人が1人もいません。")
    dup = [n for n in notes if "2回以上" in n]
    notes = sorted(set(dup), key=dup.index) + [n for n in notes if n not in dup]
    return answers, notes


# ===== シフト作成の入力にする =====
def to_staff(answers: list[Answer], dates: list[dt.date], slot: int) -> list[Staff]:
    """参加できない時間帯を、30分ごとの出勤できる枠に置きかえる。少しでもかかる枠は入れない。

    会計係は Staff.leader に入れる（元のシフト作成の「責任者」の欄を会計に使う）。
    """
    out = []
    for a in answers:
        avail = set()
        for d in dates:
            spans = merge_spans(a.busy.get(d, []))
            for t in range(0, DAY_MINUTES, slot):
                if not any(s < t + slot and t < e for s, e in spans):
                    avail.add((d, t))
        out.append(Staff(name=a.name, available=avail, max_days=a.max_days, target_hours=a.target_hours,
                         leader=a.accountant, note=a.note, answered_at=a.answered_at))
    return out


def answers_table(answers: list[Answer], dates: list[dt.date]) -> pd.DataFrame:
    """読み取った回答の確認用：人ごと・日ごとの参加できない時間帯。"""
    rows = []
    for a in answers:
        row = {"氏名": a.name, "会計": "○" if a.accountant else ""}
        for d in dates:
            row[date_label(d)] = a.busy_label(d)
        rows.append(row)
    return pd.DataFrame(rows, columns=["氏名", "会計"] + [date_label(d) for d in dates])


# ===== スタッフ表（画面で直す 会計・最大日数・上限時間） =====
def staff_frame(staff: list[Staff]) -> pd.DataFrame:
    from shift.loader import staff_frame as base

    df = base(staff).rename(columns={"責任者": "会計"})
    return df[["使う", "氏名", "会計", "回答日時", "最大日数", "希望時間", "上限時間", "備考"]]


def apply_staff_frame(staff: list[Staff], df: pd.DataFrame) -> list[Staff]:
    from shift.loader import apply_staff_frame as base

    return base(staff, df.rename(columns={"会計": "責任者"}))
