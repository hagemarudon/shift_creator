import datetime as dt

import pandas as pd

from shift.loader import parse_responses, required_staff, rule_matches
from shift.model import Rule

REF = dt.date(2026, 11, 8)
D16, D17 = dt.date(2026, 11, 16), dt.date(2026, 11, 17)


def test_grid_rows_are_times():
    df = pd.DataFrame({
        "タイムスタンプ": ["2026/11/08 10:00:00"],
        "氏名": ["佐藤"],
        "出勤できる時間（11/16〜11/22） [10:00-10:30]": ["11/16(月), 11/17(火)"],
        "出勤できる時間（11/16〜11/22） [10:30-11:00]": ["11/16(月)"],
    })
    staff, notes = parse_responses(df, 30, REF)
    assert notes == []
    assert staff[0].available == {(D16, 600), (D17, 600), (D16, 630)}


def test_grid_rows_are_dates_and_hourly_labels():
    df = pd.DataFrame({"氏名": ["鈴木"], "希望 [11/16(月)]": ["10時〜11時、13:00-14:00"]})
    staff, _ = parse_responses(df, 30, REF)
    assert staff[0].available == {(D16, t) for t in (600, 630, 780, 810)}


def test_last_answer_wins_and_numbers():
    df = pd.DataFrame({
        "氏名": ["佐藤", "佐藤 "],
        "期間中に入れる最大日数（任意）": ["3", "５日"],
        "期間中の希望勤務時間（任意）": ["", "12.5"],
        "出勤 [10:00]": ["11/16(月)", "11/17(火)"],
    })
    staff, notes = parse_responses(df, 30, REF)
    assert len(staff) == 1 and staff[0].max_days == 5 and staff[0].target_hours == 12.5
    assert staff[0].available == {(D17, 600)}
    assert any("2回以上" in n for n in notes)


def test_year_rolls_over():
    df = pd.DataFrame({"タイムスタンプ": ["2026/12/20 9:00:00"], "氏名": ["A"], "出勤 [10:00]": ["1/3(日)"]})
    staff, _ = parse_responses(df, 30)
    assert (dt.date(2027, 1, 3), 600) in staff[0].available


def test_rules():
    sat, sun, mon = dt.date(2026, 11, 21), dt.date(2026, 11, 22), dt.date(2026, 11, 23)
    hol = {mon}
    assert rule_matches("平日", D16, hol) and not rule_matches("平日", mon, hol)
    assert rule_matches("土日祝", sat, hol) and rule_matches("日祝", mon, hol)
    assert rule_matches("月水金", D16, set()) and not rule_matches("月水金", D17, set())
    assert rule_matches("11/17", D17, set())
    req, times = required_staff([Rule("毎日", "10:00", "12:00", 2), Rule("火", "11:00", "12:00", 3)],
                                [D16, D17], 30, set())
    assert times == [600, 630, 660, 690]
    assert req[D16, 660] == 2 and req[D17, 660] == 3
