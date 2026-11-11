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


def test_unchecked_staff_are_left_out():
    from shift.loader import apply_staff_frame, staff_frame

    df = pd.DataFrame({
        "タイムスタンプ": ["2026/11/10 9:00:00", "2026/11/10 9:05:00"],
        "氏名": ["佐藤", "鈴木"],
        "出勤 [10:00]": ["11/16(月)", "11/16(月)"],
    })
    staff, _ = parse_responses(df, 30, REF)
    frame = staff_frame(staff)
    assert list(frame["使う"]) == [True, True]
    assert list(frame["回答日時"]) == ["2026/11/10 9:00:00", "2026/11/10 9:05:00"]
    frame.loc[frame["氏名"] == "鈴木", "使う"] = False
    frame.loc[frame["氏名"] == "佐藤", "最大日数"] = 2
    out = apply_staff_frame(staff, frame)
    assert [s.name for s in out] == ["佐藤"]
    assert out[0].max_days == 2 and out[0].available == staff[0].available


def test_two_name_columns_after_switching_to_dropdown():
    # 記述式の「氏名」を消してプルダウンを足すと、回答のシートに「氏名」が2列できる（CSVでは「氏名.1」）
    df = pd.DataFrame([["佐藤", "11/16(月)", ""], ["", "11/17(火)", "鈴木"], ["", "11/16(月)", "佐藤"]],
                      columns=["氏名", "出勤 [10:00]", "氏名.1"])
    staff, notes = parse_responses(df, 30, REF)
    assert [s.name for s in staff] == ["鈴木", "佐藤"]
    assert any("佐藤 さんは2回以上" in n for n in notes)
    dup = pd.DataFrame([["山田", "11/16(月)", ""], ["", "11/17(火)", "高橋"]], columns=["氏名", "出勤 [10:00]", "氏名"])
    assert [s.name for s in parse_responses(dup, 30, REF)[0]] == ["山田", "高橋"]


def test_same_person_with_full_width_space():
    df = pd.DataFrame({"氏名": ["山田　太郎", "山田 太郎", "ＡＢ"], "出勤 [10:00]": ["11/16(月)", "11/17(火)", "11/16(月)"]})
    staff, _ = parse_responses(df, 30, REF)
    assert [s.name for s in staff] == ["山田 太郎", "ＡＢ"]


def test_request_frame_round_trip():
    from shift.loader import (build_problem, load_settings, read_request_frame, request_frame, request_notes,
                              update_requests)
    from shift.model import Staff

    old = {("佐藤", D16): "入れたい", ("田中", D16): "入れない", ("佐藤", dt.date(2026, 11, 30)): "入れない"}
    frame = request_frame(["佐藤", "鈴木"], [D16, D17], old)
    assert list(frame.columns) == ["氏名", "11/16(月)", "11/17(火)"]
    assert frame.loc[0, "11/16(月)"] == "入れたい" and frame.loc[1, "11/17(火)"] == "－"
    frame.loc[0, "11/16(月)"] = "－"
    frame.loc[1, "11/17(火)"] = "入れない"
    assert read_request_frame(frame, [D16, D17]) == {("鈴木", D17): "入れない"}
    # 表に出ていない人（田中）・日付（11/30）の指定は残す
    new = update_requests(old, frame, ["佐藤", "鈴木"], [D16, D17])
    assert new == {("鈴木", D17): "入れない", ("田中", D16): "入れない", ("佐藤", dt.date(2026, 11, 30)): "入れない"}

    from pathlib import Path
    settings = load_settings(Path(__file__).parent.parent / "config.toml")
    staff = [Staff("佐藤", {(D16, 600)}), Staff("鈴木", {(D16, 600)})]
    problem = build_problem(staff, settings, [D16, D17], {("佐藤", D17): "入れたい", ("鈴木", D16): "入れたい"})
    assert problem.want == {("佐藤", D17), ("鈴木", D16)}
    notes = request_notes(problem)
    assert len(notes) == 1 and "佐藤" in notes[0] and "11/17(火)" in notes[0]
