"""シフト作成で使うデータの形。時刻は 0:00 からの分（例：10:30 → 630）で持つ。"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]


def to_min(hhmm: str) -> int:
    h, m = str(hhmm).strip().split(":")[:2]
    return int(h) * 60 + int(m)


def to_hhmm(minutes: int) -> str:
    return f"{minutes // 60}:{minutes % 60:02d}"


def date_label(d: dt.date) -> str:
    """例：11/16(月)"""
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


@dataclass(frozen=True)
class Rule:
    """必要人数の決まり。後の行ほど優先（同じ時間帯は後の行で上書き）。

    days は「毎日」「平日」「土」「日祝」「土日祝」、曜日（「月」「月水金」）、日付（「11/23」）のどれか。
    """
    days: str
    start: str
    end: str
    people: int


@dataclass
class Settings:
    slot_minutes: int
    min_shift_minutes: int
    normal_max_minutes: int  # 原則の長さ。超えるのは人数不足をうめるときだけ（重み long_shift）
    max_shift_minutes: int
    rules: list[Rule]
    holidays: set[dt.date] = field(default_factory=set)
    # 時間帯ごとに責任者（スタッフ表の「責任者」に○）を1人以上入れる
    leader_required: bool = False
    time_limit_seconds: float = 30
    weights: dict[str, int] = field(default_factory=dict)


@dataclass
class Staff:
    name: str
    # 出勤できる (日付, 枠の開始分) の集まり
    available: set[tuple[dt.date, int]] = field(default_factory=set)
    max_days: int | None = None      # 期間中に入れる最大日数
    target_hours: float | None = None  # 期間中の希望勤務時間（なるべく近づける）
    max_hours: float | None = None   # 期間中の上限時間（必ず守る）
    leader: bool = False
    note: str = ""
    answered_at: str = ""  # フォームのタイムスタンプ（画面の表示用）


@dataclass
class Problem:
    settings: Settings
    staff: list[Staff]
    dates: list[dt.date]
    times: list[int]                       # 枠の開始分（期間を通して共通）
    required: dict[tuple[dt.date, int], int]  # (日付, 枠) → 必要人数。0 は営業時間外
    # シフト作成者が日ごとに指定する人：(氏名, 日付)。want はなるべく入れる、avoid は必ず入れない
    want: set[tuple[str, dt.date]] = field(default_factory=set)
    avoid: set[tuple[str, dt.date]] = field(default_factory=set)


@dataclass(frozen=True)
class Shift:
    name: str
    date: dt.date
    start: int
    end: int

    @property
    def hours(self) -> float:
        return (self.end - self.start) / 60

    @property
    def label(self) -> str:
        return f"{to_hhmm(self.start)}-{to_hhmm(self.end)}"


@dataclass
class Result:
    status: str
    shifts: list[Shift]
    # (日付, 枠) → 足りない人数
    shortage: dict[tuple[dt.date, int], int] = field(default_factory=dict)
    leader_missing: set[tuple[dt.date, int]] = field(default_factory=set)
    # 「入れたい」と指定したのに入れられなかった (氏名, 日付)
    want_missed: list[tuple[str, dt.date]] = field(default_factory=list)
