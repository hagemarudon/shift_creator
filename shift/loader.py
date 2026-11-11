"""設定ファイルと、Googleフォームの回答（CSV・Excel・スプレッドシート）の読み込み。

どんな形のフォームでも読めるように、回答の表の列ごとに「使い方」（氏名・出勤できる時間・最大日数など）を決めて読む。
使い方は見出しと中身から推測し（guess_roles）、画面で直せる。

出勤できる時間の列は、次の3か所から日付と時刻を探して組み合わせる（先にあるほうを使う）。
  1. 選んだ選択肢（チェックボックスなら1つずつ）
  2. グリッドの行の見出し（列見出しが「質問 [行の見出し]」の形のとき）
  3. 質問の見出し（日付・時刻が1つだけ書いてあるとき）
例：「出勤できる時間 [10:00-10:30]」に「11/16(月), 11/17(火)」、「11/20(金)に入れる時間」に「10:00-12:00」、
「シフト希望 [11/20(金) 10:00-12:00]」に「○」。日付だけの質問（入れる日）と時刻だけの質問（入れる時間帯）が
両方あれば、その組み合わせを出勤できる時間にする。日付だけなら、その日は一日中出勤できるとみなす。
「午前のみ」のように読めない選択肢は、画面で「9:30-12:00」などに読み替えられる（aliases）。
"""
from __future__ import annotations

import datetime as dt
import io
import re
import tomllib
import unicodedata
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd

from .model import WEEKDAYS, Problem, Rule, Settings, Staff, date_label, to_hhmm, to_min

LOCAL_CONFIG_NAME = "config.local.toml"

GRID_COL = re.compile(r"^(.*?)\s*\[(.+)\]\s*$")
DATE = re.compile(r"(?:(\d{4})\s*[/\-年]\s*)?(\d{1,2})\s*[/\-月]\s*(\d{1,2})")
NUMBER = re.compile(r"\d+(?:\.\d+)?")
SPLIT = re.compile(r"\s*[,、，]\s*")
# 10:00 / 10時 / 10時半 / 10時15分 / 午後1時 / 1:00 PM。「12時間」は時刻にしない
TIME = re.compile(
    r"(午前|午後)?\s*(\d{1,2})\s*"
    r"(?:[:]\s*(\d{2})|時(?!間)(?:\s*(半)|\s*(\d{1,2})\s*分)?|(?=\s*[AaPp]\.?[Mm]\b))"
    r"(?:\s*([AaPp])\.?[Mm]\b\.?)?"
)
HAS_TIME = re.compile(r"\d\s*[:：]|\d\s*時(?!間)")

# 列の使い方
ROLE_NAME, ROLE_AVAIL, ROLE_MAX_DAYS, ROLE_TARGET, ROLE_MAX_HOURS, ROLE_NOTE, ROLE_TIMESTAMP, ROLE_SKIP = (
    "氏名", "出勤できる時間", "最大日数", "希望勤務時間", "上限時間", "備考", "回答日時", "使わない")
ROLES = [ROLE_NAME, ROLE_AVAIL, ROLE_MAX_DAYS, ROLE_TARGET, ROLE_MAX_HOURS, ROLE_NOTE, ROLE_TIMESTAMP, ROLE_SKIP]

TIMESTAMP_KEYS = ("タイムスタンプ", "Timestamp", "回答日時")
SKIP_KEYS = ("メール", "mail", "Mail", "電話", "学籍", "学年", "所属", "学部", "連絡先", "Score", "スコア")
NAME_KEYS = ("氏名", "名前", "Name", "name")
MAX_DAYS_KEYS = ("最大日数", "入れる日数", "出勤日数")
TARGET_KEYS = ("希望勤務時間", "希望時間", "勤務時間の希望", "希望の勤務時間", "入りたい時間")
MAX_HOURS_KEYS = ("上限時間", "最大時間", "時間の上限")
NOTE_KEYS = ("備考", "要望", "その他", "コメント", "連絡事項", "Note", "Comment")
KEYED_ROLES = [(TIMESTAMP_KEYS, ROLE_TIMESTAMP), (SKIP_KEYS, ROLE_SKIP), (NAME_KEYS, ROLE_NAME),
               (MAX_DAYS_KEYS, ROLE_MAX_DAYS), (TARGET_KEYS, ROLE_TARGET), (MAX_HOURS_KEYS, ROLE_MAX_HOURS),
               (NOTE_KEYS, ROLE_NOTE)]

# 選択肢の言葉（上から順に見る。「不可」は「可」より先に見る）
NEG = re.compile(r"[×✕✖❌]|不可|できない|出来ない|無理|いいえ|休み|入れない|出られない|不参加|\b(?:no|ng|not)\b", re.I)
WHOLE_DAY = re.compile(r"終日|全日|一日中|1日中|いつでも|何時でも|フル|all\s*day", re.I)
POS = re.compile(r"[○◯〇⭕◎✓✔]|可|はい|できる|出来る|入れる|出られる|大丈夫|参加|\b(?:ok|yes|available)\b", re.I)
DAY_MINUTES = 24 * 60


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
        column_roles={str(k): str(v) for k, v in c.get("columns", {}).items() if v in ROLES},
        choice_aliases={str(k): str(v) for k, v in c.get("choices", {}).items()},
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
    roles = guess_roles(df)
    return ROLE_NAME in roles.values() and ROLE_AVAIL in roles.values()


def pick_response_sheet(dfs: dict[str, pd.DataFrame]) -> str | None:
    """フォームの回答のシートを選ぶ。「フォームの回答」「Form Responses」で始まるシートを優先する。

    氏名と出勤できる時間の列が見つからなくても、回答らしいシート（無ければ最初のシート）を返す（列の対応を画面で直せるように）。
    """
    if not dfs:
        return None
    names = list(dfs)
    is_form = [n for n in names if n.startswith(("フォームの回答", "Form Responses", "Form responses"))]
    good = [n for n in names if is_response_sheet(dfs[n])]
    for cands in ([n for n in is_form if n in good], good, is_form):
        if cands:
            return cands[0]
    return next((n for n in names if not dfs[n].empty), names[0])


def _cell(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none") else s


def _number(v) -> float | None:
    m = NUMBER.search(_cell(v).translate(str.maketrans("０１２３４５６７８９．", "0123456789.")))
    return float(m.group()) if m else None


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", str(s)).strip()


def _times(s: str) -> list[int]:
    out = []
    for jp, h, mm, half, fun, ap in TIME.findall(s):
        h, m = int(h), (30 if half else int(mm or fun or 0))
        if jp == "午後" or ap in ("p", "P"):
            h += 12 if h < 12 else 0
        elif (jp == "午前" or ap in ("a", "A")) and h == 12:
            h = 0
        if h * 60 + m <= DAY_MINUTES and m < 60:
            out.append(h * 60 + m)
    return out


def _resolve_date(y, mo, d, ref: dt.date) -> dt.date | None:
    """年が書いてなければ ref に一番近い年にする。"""
    try:
        if y:
            return dt.date(int(y), int(mo), int(d))
    except ValueError:
        return None
    cands = []
    for k in (-1, 0, 1):
        try:
            cands.append(dt.date(ref.year + k, int(mo), int(d)))
        except ValueError:  # 2/29 や 13月など
            pass
    return min(cands, key=lambda c: abs((c - ref).days)) if cands else None


def _dates(s: str, ref: dt.date) -> list[dt.date]:
    masked = TIME.sub(" ", s)  # 「9:05-10:00」の「05-10」を日付と読まないように、時刻を先に消す
    out = []
    for y, mo, d in DATE.findall(masked):
        day = _resolve_date(y, mo, d, ref)
        if day and day not in out:
            out.append(day)
    return out


def _ranges(times: list[int]) -> list[tuple[int, int | None]] | None:
    """時刻を2つずつ時間帯にする（「10:00-12:00 14:00-16:00」は2つ）。余った1つは開始だけ。終わりが前なら None。"""
    out: list[tuple[int, int | None]] = []
    for i in range(0, len(times) - 1, 2):
        a, b = times[i], times[i + 1]
        if b <= a < b + 720 and b < 720:  # 「午後1時〜3時」の終わりは午後
            b += 720
        if b <= a:
            return None
        out.append((a, b))
    if len(times) % 2:
        out.append((times[-1], None))
    return out


def parse_times(label: str) -> tuple[int, int | None] | None:
    """「10:00」→(600, None)、「10:00-11:00」「10時〜11時」→(600, 660)。時刻がなければ None。"""
    found = _ranges(_times(_norm(label)))
    return found[0] if found else None


def parse_date(label: str, ref: dt.date) -> dt.date | None:
    """「11/16(月)」などを日付にする。年が書いてなければ ref に一番近い年にする。時刻の見出しは None。"""
    s = _norm(label)
    if HAS_TIME.search(s):
        return None
    found = _dates(s, ref)
    return found[0] if found else None


@dataclass
class _Piece:
    """選択肢・行の見出し・質問の見出しのどれか1つから読み取ったもの。"""
    neg: bool = False
    pos: bool = False
    whole_day: bool = False
    dates: list[dt.date] = field(default_factory=list)
    ranges: list[tuple[int, int | None]] = field(default_factory=list)  # 終わりが None は開始だけ（長さは刻みから）

    @property
    def understood(self) -> bool:
        return self.neg or self.pos or self.whole_day or bool(self.dates or self.ranges)


def _read_piece(text: str, ref: dt.date) -> _Piece | None:
    """読めなければ None。"""
    s = _norm(text)
    if not s:
        return None
    if NEG.search(s):
        return _Piece(neg=True)
    ranges = _ranges(_times(s))
    if ranges is None:
        return None
    p = _Piece(whole_day=bool(WHOLE_DAY.search(s)), dates=_dates(s, ref), ranges=ranges)
    p.pos = not p.understood and bool(POS.search(s))
    return p if p.understood else None


def _ref_date(row, ts_pos: int | None, default: dt.date) -> dt.date:
    if ts_pos is not None:
        m = DATE.search(_cell(row.iat[ts_pos]))
        if m and m.group(1):
            try:
                return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
    return default


# ===== 列の使い方 =====
def _looks_available(header: str, values: list[str], ref: dt.date) -> bool:
    """見出しか中身に日付・時刻が入っている列。"""
    q, _, row = _split_header(header)
    head = [_read_piece(x, ref) for x in (q, row) if x]
    if any(p and (p.dates or p.ranges) for p in head):
        return True
    sample = [v for v in values if v][:20]
    if not sample:
        return False
    ok = 0
    for v in sample:
        pieces = [_read_piece(c, ref) for c in SPLIT.split(v) if c]
        ok += any(p and (p.dates or p.ranges) for p in pieces)
    return ok * 2 >= len(sample)


def _split_header(header: str) -> tuple[str, bool, str]:
    """「質問 [行]」→ (質問, True, 行)。グリッドでなければ (見出し, False, "")。"""
    m = GRID_COL.match(str(header))
    return (m.group(1).strip(), True, m.group(2).strip()) if m else (str(header).strip(), False, "")


def guess_roles(df: pd.DataFrame, today: dt.date | None = None) -> dict[str, str]:
    """列見出し → 使い方 の推測。同じ見出しの列は同じ使い方にする。"""
    today = today or dt.date.today()
    out: dict[str, str] = {}
    for i, c in enumerate(df.columns):
        h = str(c)
        if h in out:
            continue
        if not h.strip() or h.startswith("Unnamed"):
            out[h] = ROLE_SKIP
            continue
        if GRID_COL.match(h):
            out[h] = ROLE_AVAIL
            continue
        role = next((r for keys, r in KEYED_ROLES if any(k in h for k in keys)), None)
        if role in (None, ROLE_MAX_DAYS, ROLE_TARGET, ROLE_MAX_HOURS):  # 「出勤希望時間」に時間帯を答える列もある
            values = [_cell(v) for v in df.iloc[:, i]]
            if _looks_available(h, values, today):
                role = ROLE_AVAIL
        if role == ROLE_AVAIL and NEG.search(_norm(h)):  # 「休み希望」「出勤できない日」は出勤できる時間ではない
            role = ROLE_SKIP
        out[h] = role or ROLE_SKIP
    return out


def column_frame(df: pd.DataFrame, roles: dict[str, str]) -> pd.DataFrame:
    """画面で使い方を直すための表（列・回答の例・使い方）。"""
    rows, seen = [], set()
    for i, c in enumerate(df.columns):
        h = str(c)
        if h in seen:
            continue
        seen.add(h)
        ex = next((v for v in (_cell(x) for x in df.iloc[:, i]) if v), "")
        rows.append({"列": h, "回答の例": ex if len(ex) <= 40 else ex[:40] + "…", "使い方": roles.get(h, ROLE_SKIP)})
    return pd.DataFrame(rows, columns=["列", "回答の例", "使い方"])


# ===== 回答を読む =====
@dataclass
class Responses:
    staff: list[Staff]
    notes: list[str]
    # 日付・時刻として読めなかった選択肢や行の見出し → 出てきた回数（多い順）。画面で読み替えを入れてもらう
    unreadable: dict[str, int] = field(default_factory=dict)
    alias_used: dict[str, int] = field(default_factory=dict)  # 読み替えを使った選択肢 → 回数


def _merge_intervals(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(iv):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def covered(intervals: list[tuple[int, int]], start: int, end: int) -> bool:
    """[start, end) がまるごと出勤できる時間に入っているか（intervals はまとめたもの）。"""
    return any(a <= start and end <= b for a, b in intervals)


def slots_from_free(free: dict[dt.date, list[tuple[int, int]]], dates, times, slot: int) -> set[tuple[dt.date, int]]:
    """出勤できる時間（分の区間）を、刻みの枠の集まりにする。枠の間ずっと出勤できるときだけ入れる。"""
    return {(d, t) for d in dates if free.get(d) for t in times if covered(free[d], t, t + slot)}


def read_responses(df: pd.DataFrame, slot_minutes: int, today: dt.date | None = None,
                   roles: dict[str, str] | None = None, aliases: dict[str, str] | None = None) -> Responses:
    """フォームの回答の表からスタッフと出勤できる時間を読む。同じ氏名は最後の回答を使う。

    roles は 列見出し → 使い方（無い列は推測する）。aliases は 読めない選択肢 → 読み替え（「9:30-12:00」「×」など）。
    """
    today = today or dt.date.today()
    guessed = guess_roles(df, today)
    roles = {h: (roles or {}).get(h, g) for h, g in guessed.items()}
    aliases = {k.strip(): v for k, v in (aliases or {}).items() if str(v).strip()}
    by_role: dict[str, list[int]] = {r: [] for r in ROLES}
    for i, c in enumerate(df.columns):
        by_role[roles.get(str(c), ROLE_SKIP)].append(i)
    notes: list[str] = []
    if not by_role[ROLE_NAME]:
        return Responses([], ["氏名の列がありません。「回答の読み方」で、氏名の列の使い方を「氏名」にしてください。"])
    if not by_role[ROLE_AVAIL]:
        return Responses([], ["出勤できる時間の列がありません。「回答の読み方」で、出勤できる日や時間を聞いた列の使い方を"
                              "「出勤できる時間」にしてください。"])
    ts_pos = by_role[ROLE_TIMESTAMP][0] if by_role[ROLE_TIMESTAMP] else None
    avail_cols = [(i, *_split_header(str(df.columns[i]))) for i in by_role[ROLE_AVAIL]]

    for h in dict.fromkeys(str(df.columns[i]) for i, q, _, _ in avail_cols if NEG.search(_norm(q))):
        notes.append(f"「{h}」は出勤できない日・時間を聞く質問のようなので読んでいません。")
    avail_cols = [c for c in avail_cols if not NEG.search(_norm(c[1]))]
    unreadable: Counter[str] = Counter()
    alias_used: Counter[str] = Counter()
    no_date_cols: set[str] = set()
    day_only_cols: set[str] = set()

    def piece(text: str, ref: dt.date) -> _Piece | None:
        if text.strip() in aliases:
            alias_used[text.strip()] += 1
            return _read_piece(aliases[text.strip()], ref)
        return _read_piece(text, ref)

    # 1回目：人ごとに (日付, 開始, 終了 or None) を集める
    parsed = []
    for n in range(len(df)):
        row = df.iloc[n]
        name = " ".join(v for v in (_cell(row.iat[i]) for i in by_role[ROLE_NAME]) if v)
        if not name:
            continue
        ref = _ref_date(row, ts_pos, today)
        pairs: list[tuple[dt.date, int, int | None]] = []
        day_only: dict[dt.date, str] = {}
        time_only: list[tuple[tuple[int, int | None], str]] = []
        for i, question, is_grid, row_label in avail_cols:
            value = _cell(row.iat[i])
            if not value:
                continue
            q_piece = _read_piece(question, ref)
            row_piece = piece(row_label, ref) if is_grid else None
            if is_grid and row_piece is None:
                unreadable[row_label] += 1
                continue
            if row_piece and row_piece.neg:
                continue
            header = str(df.columns[i])
            for chosen in SPLIT.split(value):
                if not chosen:
                    continue
                p = piece(chosen, ref)
                if p is None:
                    unreadable[chosen] += 1
                    continue
                if p.neg:
                    continue
                parts = [x for x in (p, row_piece) if x]
                dates = next((x.dates for x in parts if x.dates), [])
                if not dates and q_piece and len(q_piece.dates) == 1:
                    dates = q_piece.dates
                ranges = next((x.ranges for x in parts if x.ranges), [])
                whole = any(x.whole_day for x in parts)
                if not ranges and not whole and q_piece and q_piece.ranges and not q_piece.dates[1:]:
                    ranges = q_piece.ranges
                if whole:
                    ranges = [(0, DAY_MINUTES)]
                if dates and ranges:
                    pairs += [(d, a, b) for d in dates for a, b in ranges]
                elif dates:
                    for d in dates:
                        day_only[d] = header
                elif ranges:
                    time_only += [(r, header) for r in ranges]
                else:
                    no_date_cols.add(header)
        if day_only:
            if time_only:
                pairs += [(d, a, b) for d in day_only for (a, b), _ in time_only]
            else:
                pairs += [(d, 0, DAY_MINUTES) for d in day_only]
                day_only_cols |= set(day_only.values())
        elif time_only:
            no_date_cols |= {h for _, h in time_only}
        parsed.append((n, row, name, pairs))

    # 終わりのない時刻（「10:00」だけ）の長さ：フォームの時刻の間隔（1時間おきに聞いていれば1時間）
    starts = sorted({a for *_, pairs in parsed for _, a, b in pairs if b is None})
    gaps = [b - a for a, b in zip(starts, starts[1:]) if b > a]
    step = min(gaps) if gaps else slot_minutes

    by_name: dict[str, Staff] = {}
    for n, row, name, pairs in parsed:
        key = name_key(name)
        if key in by_name:
            notes.append(f"{name} さんは2回以上答えています。最後の回答を使います。")
        free: dict[dt.date, list[tuple[int, int]]] = {}
        for d, a, b in pairs:
            free.setdefault(d, []).append((a, b if b is not None else min(a + step, DAY_MINUTES)))
        free = {d: _merge_intervals(iv) for d, iv in sorted(free.items())}
        staff = Staff(name=name, free=free, answered_at=_cell(row.iat[ts_pos]) if ts_pos is not None else "",
                      available=slots_from_free(free, free, range(0, DAY_MINUTES, slot_minutes), slot_minutes))

        def first(role):
            return next((v for v in (_cell(row.iat[i]) for i in by_role[role]) if v), "")

        num = _number(first(ROLE_MAX_DAYS))
        staff.max_days = int(num) if num is not None else None
        staff.target_hours = _number(first(ROLE_TARGET))
        staff.max_hours = _number(first(ROLE_MAX_HOURS))
        staff.note = " / ".join(v for v in (_cell(row.iat[i]) for i in by_role[ROLE_NOTE]) if v)
        by_name.pop(key, None)  # 答え直した人は並びも最後にする
        by_name[key] = staff
    if unreadable:
        sample = "、".join(f"「{k}」" for k, _ in unreadable.most_common(3))
        notes.append(f"日付・時刻として読めない選択肢が {len(unreadable)}種類 ありました（{sample} など）。"
                     "「回答の読み方」で読み替えを入れると読めます。")
    for h in sorted(no_date_cols):
        notes.append(f"「{h}」は日付がわからない回答があったので、その分は読んでいません。日付を聞く列も「出勤できる時間」にしてください。")
    for h in sorted(day_only_cols):
        notes.append(f"「{h}」は日付だけの回答なので、その日は一日中出勤できるとして読みました。")
    for s in by_name.values():
        if not s.free:
            notes.append(f"{s.name} さんは出勤できる時間が1つもありません。")
    dup = [n for n in notes if "2回以上" in n]
    notes = sorted(set(dup), key=dup.index) + [n for n in notes if n not in dup]
    return Responses(list(by_name.values()), notes, dict(unreadable.most_common()), dict(alias_used))


def parse_responses(df: pd.DataFrame, slot_minutes: int, today: dt.date | None = None,
                    roles: dict[str, str] | None = None, aliases: dict[str, str] | None = None
                    ) -> tuple[list[Staff], list[str]]:
    r = read_responses(df, slot_minutes, today, roles, aliases)
    return r.staff, r.notes


def alias_frame(unreadable: dict[str, int], aliases: dict[str, str],
                alias_used: dict[str, int] | None = None) -> pd.DataFrame:
    """読めない選択肢と、いま入っている読み替えの表（選択肢・回数・読み替え）。回数はこの回答に出てきた回数。"""
    used = alias_used or {}
    keys = list(unreadable) + [k for k in aliases if k not in unreadable]
    return pd.DataFrame([{"選択肢": k, "回数": unreadable.get(k, 0) or used.get(k, 0), "読み替え": aliases.get(k, "")}
                         for k in keys],
                        columns=["選択肢", "回数", "読み替え"])


def read_alias_frame(df: pd.DataFrame) -> dict[str, str]:
    return {str(r["選択肢"]): _cell(r["読み替え"]) for _, r in df.iterrows() if _cell(r["読み替え"])}


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


def check_rules(rules: list[Rule], slot: int | None = None) -> list[str]:
    """slot（時間刻み）を渡すと、必要人数の時刻が刻みに合っているかも確かめる。"""
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
    if slot and not errors and rules:
        first = min(to_min(r.start) for r in rules)
        off = [f"{i}行目（{r.start}-{r.end}）" for i, r in enumerate(rules, 1)
               if (to_min(r.start) - first) % slot or (to_min(r.end) - first) % slot]
        if off:
            errors.append(f"必要人数の{'・'.join(off)}：時刻が時間刻み（{slot}分）に合っていません。"
                          f"{to_hhmm(first)}から{slot}分ごとの時刻にするか、時間刻みを細かくしてください。")
    return errors


def required_staff(rules: list[Rule], dates: list[dt.date], slot: int, holidays: set[dt.date]):
    """(日付, 枠) → 必要人数 と、期間を通して使う枠の一覧を返す。"""
    starts = [to_min(r.start) for r in rules]
    ends = [to_min(r.end) for r in rules]
    if not rules:
        return {}, []
    # 枠は一番早い開始から刻みごと（刻みが60分で9:30開始なら 9:30, 10:30, …）
    first = min(starts)
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
    # フォームの回答から読んだ人は、出勤できる時間（分）を今の刻みの枠に直す
    staff = [replace(s, available=slots_from_free(s.free, dates, times, settings.slot_minutes))
             if s.free is not None else s for s in staff]
    requests = requests or {}
    return Problem(settings=settings, staff=staff, dates=dates, times=times, required=req,
                   want={k for k, v in requests.items() if v == WANT},
                   avoid={k for k, v in requests.items() if v == AVOID})


def response_dates(staff: list[Staff]) -> list[dt.date]:
    days = {d for s in staff for d in (s.free if s.free is not None else (d for d, _ in s.available))}
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
        out.append(replace(
            s,
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
