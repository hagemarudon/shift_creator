"""ほかの人が作った、質問の形や順番が違うフォームの回答を読む。"""
import datetime as dt
from pathlib import Path

import pandas as pd

from shift.loader import (ROLE_AVAIL, ROLE_NAME, ROLE_SKIP, ROLE_TARGET, alias_frame, build_problem, check_rules,
                          guess_roles, load_settings, parse_times, pick_response_sheet, read_alias_frame,
                          read_responses)
from shift.model import Rule, Settings, Staff
from shift.settings_io import save_settings
from shift.solver import solve

REF = dt.date(2026, 11, 11)
D20, D21 = dt.date(2026, 11, 20), dt.date(2026, 11, 21)


def free(staff, name):
    return next(s for s in staff if s.name == name).free


def test_question_per_date_with_time_choices_in_any_order():
    df = pd.DataFrame({
        "11/20(金)に入れる時間": ["10:00-12:00, 13:00-15:00", ""],
        "お名前": ["佐藤", "鈴木"],
        "11/21(土)に入れる時間": ["", "9時半〜12時"],
        "メールアドレス": ["a@example.com", "b@example.com"],
    })
    roles = guess_roles(df, REF)
    assert roles["お名前"] == ROLE_NAME and roles["メールアドレス"] == ROLE_SKIP
    assert roles["11/20(金)に入れる時間"] == ROLE_AVAIL
    r = read_responses(df, 30, REF)
    assert free(r.staff, "佐藤") == {D20: [(600, 720), (780, 900)]}
    assert free(r.staff, "鈴木") == {D21: [(570, 720)]}


def test_days_question_and_hours_question_are_combined():
    df = pd.DataFrame({"名前": ["A", "B"], "入れる日": ["11/20(金), 11/21(土)", "11/21(土)"],
                       "入れる時間帯": ["10:00-14:00", ""]})
    r = read_responses(df, 30, REF)
    assert free(r.staff, "A") == {D20: [(600, 840)], D21: [(600, 840)]}
    assert free(r.staff, "B") == {D21: [(0, 1440)]}  # 時間を答えていなければ一日中
    assert any("一日中" in n for n in r.notes)


def test_yes_no_grid_with_date_and_time_in_row():
    df = pd.DataFrame({
        "氏名": ["A"],
        "シフト希望 [11/20(金) 10:00-12:00]": ["○"],
        "シフト希望 [11/20(金) 12:00-14:00]": ["×"],
        "シフト希望 [11/21(土) 10:00-12:00]": ["可"],
        "シフト希望 [11/21(土) 12:00-14:00]": ["不可"],
    })
    r = read_responses(df, 30, REF)
    assert r.staff[0].free == {D20: [(600, 720)], D21: [(600, 720)]}
    assert not r.unreadable


def test_unreadable_choices_can_be_replaced():
    df = pd.DataFrame({"氏名": ["A", "B", "C"], "11/20(金)": ["終日OK", "午前のみ", "不可"],
                       "11/21(土)": ["午後のみ", "午前のみ", "終日OK"]})
    r = read_responses(df, 30, REF)
    assert r.unreadable == {"午前のみ": 2, "午後のみ": 1}
    assert free(r.staff, "A") == {D20: [(0, 1440)]}
    assert free(r.staff, "C") == {D21: [(0, 1440)]}
    frame = alias_frame(r.unreadable, {})
    frame.loc[frame["選択肢"] == "午前のみ", "読み替え"] = "9:30-12:00"
    frame.loc[frame["選択肢"] == "午後のみ", "読み替え"] = "13:00-18:00"
    aliases = read_alias_frame(frame)
    r = read_responses(df, 30, REF, aliases=aliases)
    assert not r.unreadable and r.alias_used == {"午前のみ": 2, "午後のみ": 1}
    assert list(alias_frame(r.unreadable, aliases, r.alias_used)["回数"]) == [2, 1]
    assert free(r.staff, "A") == {D20: [(0, 1440)], D21: [(780, 1080)]}
    assert free(r.staff, "B") == {D20: [(570, 720)], D21: [(570, 720)]}


def test_grid_rows_that_are_not_times_can_be_replaced():
    df = pd.DataFrame({"氏名": ["A"], "出勤 [1限]": ["11/20(金), 11/21(土)"], "出勤 [2限]": ["11/20(金)"]})
    r = read_responses(df, 30, REF)
    assert r.unreadable == {"1限": 1, "2限": 1} and not r.staff[0].free
    r = read_responses(df, 30, REF, aliases={"1限": "9:00-10:30", "2限": "10:40-12:10"})
    assert r.staff[0].free == {D20: [(540, 630), (640, 730)], D21: [(540, 630)]}


def test_roles_set_by_hand_and_two_name_parts():
    df = pd.DataFrame({"姓": ["山田"], "名": ["太郎"], "ニックネーム": ["たろ"], "Q3": ["11/20(金) 10:00-12:00"]})
    roles = {"姓": ROLE_NAME, "名": ROLE_NAME, "ニックネーム": ROLE_SKIP}
    r = read_responses(df, 30, REF, roles=roles)
    assert r.staff[0].name == "山田 太郎"
    assert r.staff[0].free == {D20: [(600, 720)]}


def test_hourly_labels_last_one_hour():
    df = pd.DataFrame({"氏名": ["A"], "出勤 [10:00]": ["11/20(金)"], "出勤 [11:00]": ["11/20(金)"],
                       "出勤 [13:00]": ["11/20(金)"]})
    assert read_responses(df, 30, REF).staff[0].free == {D20: [(600, 720), (780, 840)]}


def test_days_off_question_is_not_read_as_available():
    df = pd.DataFrame({"氏名": ["A"], "休み希望": ["11/20(金)"], "出勤できる日": ["11/21(土)"]})
    assert guess_roles(df, REF)["休み希望"] == ROLE_SKIP
    r = read_responses(df, 30, REF, roles={"休み希望": ROLE_AVAIL})
    assert set(r.staff[0].free) == {D21}
    assert any("休み希望" in n for n in r.notes)


def test_time_words():
    assert parse_times("午後1時〜3時半") == (780, 930)
    assert parse_times("10:00 AM - 1:00 PM") == (600, 780)
    assert parse_times("１０：００～１２：００") == (600, 720)
    assert parse_times("12時間") is None


def test_hours_column_with_ranges_is_availability():
    df = pd.DataFrame({"氏名": ["A", "B"], "出勤希望時間": ["10:00-14:00", "12:00-16:00"], "入れる日": ["11/20(金)", "11/20(金)"],
                       "希望時間": ["10", "15時間"]})
    roles = guess_roles(df, REF)
    assert roles["出勤希望時間"] == ROLE_AVAIL and roles["希望時間"] == ROLE_TARGET
    r = read_responses(df, 30, REF)
    assert [s.target_hours for s in r.staff] == [10, 15]
    assert free(r.staff, "B") == {D20: [(720, 960)]}


def test_english_form():
    dfs = {"Sheet1": pd.DataFrame({"x": ["1"]}),
           "Form Responses 1": pd.DataFrame({"Timestamp": ["11/10/2026 9:00:00"], "Name": ["Alex"],
                                             "Availability [10:00 AM - 11:00 AM]": ["11/20 (Fri), 11/21 (Sat)"]})}
    assert pick_response_sheet(dfs) == "Form Responses 1"
    r = read_responses(dfs["Form Responses 1"], 30, REF)
    assert r.staff[0].free == {D20: [(600, 660)], D21: [(600, 660)]}


def test_no_name_column_explains_what_to_do():
    r = read_responses(pd.DataFrame({"ニックネーム": ["たろ"], "入れる日": ["11/20(金)"]}), 30, REF)
    assert not r.staff and "回答の読み方" in r.notes[0]


# ===== 時間刻み =====
W = {"shortage": 1000, "leader_missing": 300, "long_shift": 200, "over": 10, "balance": 20,
     "target_hours": 5, "work_day": 2, "want": 500}


def settings(slot, rules, **kw):
    base = dict(slot_minutes=slot, min_shift_minutes=60, normal_max_minutes=120, max_shift_minutes=480,
                rules=rules, time_limit_seconds=10, weights=dict(W))
    base.update(kw)
    return Settings(**base)


def test_coarser_slot_needs_the_whole_slot():
    # 30分ごとに答えた人を60分刻みで使う：10:00-11:30 に入れる人は 10:00-11:00 の枠だけ
    a = Staff("A", free={D20: [(600, 690)]})
    p = build_problem([a], settings(60, [Rule("毎日", "10:00", "12:00", 1)]), [D20])
    assert p.times == [600, 660]
    assert p.staff[0].available == {(D20, 600)}
    p = build_problem([a], settings(30, [Rule("毎日", "10:00", "12:00", 1)]), [D20])
    assert p.staff[0].available == {(D20, 600), (D20, 630), (D20, 660)}


def test_slot_starts_at_opening_time():
    p = build_problem([], settings(60, [Rule("毎日", "9:30", "12:30", 1)]), [D20])
    assert p.times == [570, 630, 690]
    assert check_rules([Rule("毎日", "9:30", "12:30", 1)], 60) == []
    errs = check_rules([Rule("毎日", "9:30", "12:00", 1)], 60)
    assert len(errs) == 1 and "時間刻み（60分）" in errs[0]


def test_solve_with_60_and_15_minute_slots():
    for slot in (60, 15):
        staff = [Staff(n, free={D20: [(600, 960)]}) for n in "ABC"]
        s = settings(slot, [Rule("毎日", "10:00", "16:00", 1)])
        r = solve(build_problem(staff, s, [D20]))
        assert not r.shortage
        assert all(x.hours <= 2 for x in r.shifts)
        assert sum(x.hours for x in r.shifts) == 6
        assert all((x.start - 600) % slot == 0 and (x.end - 600) % slot == 0 for x in r.shifts)


def test_read_with_slot_and_output(tmp_path):
    from shift.output import day_html, to_xlsx

    df = pd.DataFrame({"氏名": ["A", "B"], "入れる日": ["11/20(金)", "11/20(金)"], "時間": ["10:00-13:00", "12:00-16:00"]})
    staff = read_responses(df, 30, REF).staff
    s = settings(60, [Rule("毎日", "10:00", "16:00", 1)])
    p = build_problem(staff, s, [D20])
    r = solve(p)
    assert not r.shortage
    assert "10:00" in day_html(p, r, D20) and to_xlsx(p, r)


def test_save_and_load_reading_rules(tmp_path):
    base = Path(__file__).parent.parent / "config.toml"
    s = load_settings(base)
    s.slot_minutes = 60
    s.column_roles = {"お名前（フルネーム）": "氏名", 'へんな "見出し" [10:00]': "使わない"}
    s.choice_aliases = {"午前のみ": "9:30-12:00"}
    out = tmp_path / "config.local.toml"
    save_settings(s, out)
    t = load_settings(base, overlay=out)
    assert t.slot_minutes == 60 and t.column_roles == s.column_roles and t.choice_aliases == s.choice_aliases
