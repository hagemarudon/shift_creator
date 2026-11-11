"""NF用（nf/）のテスト。フォームの回答の読み方と、会計係を必ず入れること。"""
import datetime as dt
from pathlib import Path

import pandas as pd

from nf.loader import answers_table, find_span, load_nf_config, parse_answers, save_nf_config, to_staff
from nf.output import shortage_table, to_xlsx
from nf.solver import solve
from shift.loader import build_problem
from shift.model import Rule, Settings, Staff

DATES = [dt.date(2026, 11, 20) + dt.timedelta(n) for n in range(4)]
D20, D21, D22, D23 = DATES
W = {"shortage": 1000, "long_shift": 200, "over": 10, "balance": 20, "target_hours": 5, "work_day": 2}


def busy(a, d):
    return a.busy_label(d)


# ===== 回答を読む =====
def test_per_day_checkbox_and_accountant():
    df = pd.DataFrame({
        "タイムスタンプ": ["2026/11/10 9:00:00", "2026/11/10 9:05:00"],
        "メールアドレス": ["a@example.com", "b@example.com"],
        "氏名": ["佐藤", "鈴木"],
        "あなたは会計ですか？": ["はい", "いいえ"],
        "11/20(金) 参加できない時間帯": ["10:00-11:00, 11:00-12:00", ""],
        "11月21日（土）": ["", "終日"],
        "11/22(日)": ["午後", "10時〜11時、15時半-16時"],
        "11/23(月)": ["17:00〜", "13-14時"],
    })
    answers, notes = parse_answers(df, DATES)
    assert notes == []
    a, b = answers
    assert a.accountant and not b.accountant
    assert busy(a, D20) == "10:00-12:00" and busy(a, D21) == "" and busy(a, D22) == "12:00-24:00"
    assert busy(a, D23) == "17:00-24:00"
    assert busy(b, D20) == "" and busy(b, D21) == "終日"
    assert busy(b, D22) == "10:00-11:00、15:30-16:00" and busy(b, D23) == "13:00-14:00"


def test_grid_rows_are_times_values_are_dates():
    df = pd.DataFrame({
        "氏名": ["佐藤"], "あなたは会計ですか？": ["はい"],
        "参加できない時間帯 [10:00-11:00]": ["11/20(金), 11/22(日)"],
        "参加できない時間帯 [11:00-12:00]": ["11/20(金)"],
    })
    (a,), notes = parse_answers(df, DATES)
    assert notes == []
    assert busy(a, D20) == "10:00-12:00" and busy(a, D22) == "10:00-11:00" and busy(a, D21) == ""


def test_grid_rows_are_dates_values_are_times():
    df = pd.DataFrame({
        "氏名": ["佐藤"], "あなたは会計ですか？": ["はい"],
        "参加できない時間帯 [11/21(土)]": ["9:00~10:00, 14:00~15:00"],
    })
    (a,), _ = parse_answers(df, DATES)
    assert busy(a, D21) == "9:00-10:00、14:00-15:00"


def test_dateless_questions_follow_the_period_order():
    # 日ごとのセクションに同じ題名の質問を置くと、CSVでは同じ見出しが並ぶ（pandas は .1 .2 を付ける）
    csv = ("氏名,あなたは会計ですか？,参加できない時間帯,参加できない時間帯,参加できない時間帯,参加できない時間帯\n"
           "佐藤,はい,,10:00-11:00,,終日\n")
    path = Path(__file__).with_name("_tmp_nf.csv")
    path.write_text(csv, encoding="utf-8")
    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=False)
    finally:
        path.unlink()
    (a,), notes = parse_answers(df, DATES)
    assert notes == []
    assert busy(a, D20) == "" and busy(a, D21) == "10:00-11:00" and busy(a, D23) == "終日"


def test_same_email_last_answer_wins_and_unreadable_choice():
    df = pd.DataFrame({
        "メールアドレス": ["a@example.com", "A@example.com"],
        "氏名": ["佐藤", "佐藤 花子"],
        "あなたは会計ですか？": ["いいえ", "はい"],
        "11/20(金)": ["10:00-11:00", "準備, 12:00-13:00"],
    })
    answers, notes = parse_answers(df, DATES)
    assert len(answers) == 1 and answers[0].name == "佐藤 花子" and answers[0].accountant
    assert busy(answers[0], D20) == "12:00-13:00"
    assert any("2回以上" in n for n in notes) and any("準備" in n for n in notes)


def test_find_span():
    assert find_span("10:00", 60) == (600, 660)
    assert find_span("10時半〜11時", 60) == (630, 660)
    assert find_span("〜11:00", 60) == (0, 660)
    assert find_span("参加できない", 60) is None


def test_busy_slots_are_not_available():
    df = pd.DataFrame({"氏名": ["佐藤"], "あなたは会計ですか？": ["はい"], "11/20(金)": ["10:15-11:00"]})
    answers, _ = parse_answers(df, DATES)
    (s,) = to_staff(answers, DATES, 30)
    assert s.leader
    assert (D20, 570) in s.available and (D20, 600) not in s.available and (D20, 630) not in s.available
    assert (D20, 660) in s.available and all((D21, t) in s.available for t in range(0, 1440, 30))


# ===== 会計係を必ず入れる =====
def settings(rules, **kw):
    base = dict(slot_minutes=30, min_shift_minutes=60, normal_max_minutes=120, max_shift_minutes=240,
                rules=rules, time_limit_seconds=10, weights=dict(W))
    base.update(kw)
    return Settings(**base)


def person(name, start, end, dates=(D20,), **kw):
    return Staff(name, {(d, t) for d in dates for t in range(start, end, 30)}, **kw)


def accountant_on_duty(problem, result):
    acc = {s.name for s in problem.staff if s.leader}
    on = set()
    for s in result.shifts:
        if s.name in acc:
            on |= {(s.date, t) for t in range(s.start, s.end, 30)}
    return on


def test_accountant_is_always_on_duty():
    s = settings([Rule("毎日", "10:00", "14:00", 2)])
    staff = [person("A", 600, 840), person("B", 600, 840), person("C", 600, 840),
             person("会計1", 600, 720, leader=True), person("会計2", 720, 840, leader=True)]
    p = build_problem(staff, s, [D20])
    r = solve(p)
    assert not r.shortage and not r.leader_missing
    open_slots = {k for k, v in p.required.items() if v}
    assert open_slots <= accountant_on_duty(p, r)


def test_accountant_wins_over_people_count():
    # 必要1人。会計係を入れると延長が要るが、それでも会計係を入れる
    s = settings([Rule("毎日", "10:00", "14:00", 1)])
    staff = [person("A", 600, 840), person("B", 600, 840), person("会計", 600, 840, leader=True)]
    p = build_problem(staff, s, [D20])
    r = solve(p)
    assert not r.leader_missing and {x.name for x in r.shifts} == {"会計"}


def test_missing_accountant_is_reported_only_where_impossible():
    s = settings([Rule("毎日", "10:00", "14:00", 1)])
    staff = [person("A", 600, 840, dates=(D20, D21)), person("会計", 600, 840, dates=(D20, D21), leader=True,
                                                            max_days=1)]
    p = build_problem(staff, s, [D20, D21])
    r = solve(p)
    # 会計係は1日しか入れないので、もう1日の4時間（8枠）だけ会計不在
    assert len(r.leader_missing) == 8 and len({d for d, _ in r.leader_missing}) == 1
    table = shortage_table(p, r)
    assert list(table["会計"]) == ["不在"] and table["出勤できる会計"].iloc[0] == 1


def test_two_accountants_when_asked():
    s = settings([Rule("毎日", "10:00", "12:00", 3)])
    staff = [person("A", 600, 720), person("会計1", 600, 720, leader=True), person("会計2", 600, 720, leader=True)]
    r = solve(build_problem(staff, s, [D20]), accountant_min=2)
    assert {x.name for x in r.shifts} == {"A", "会計1", "会計2"} and not r.leader_missing


# ===== 設定・サンプル・書き出し =====
def test_config_round_trip(tmp_path):
    root = Path(__file__).resolve().parent.parent / "nf"
    cfg = load_nf_config(root / "config.toml")
    assert cfg.dates == DATES and cfg.settings.holidays == {D23} and cfg.accountant_min == 1
    cfg.accountant_min = 2
    cfg.dates = DATES[:2]
    local = tmp_path / "config.local.toml"
    save_nf_config(cfg, local)
    again = load_nf_config(root / "config.toml", overlay=local)
    assert again.accountant_min == 2 and again.dates == DATES[:2]
    assert again.settings.rules == cfg.settings.rules


def test_sample_end_to_end():
    root = Path(__file__).resolve().parent.parent / "nf"
    cfg = load_nf_config(root / "config.toml")
    df = pd.read_csv(root / "sample" / "NF回答サンプル.csv", dtype=str, keep_default_na=False)
    answers, notes = parse_answers(df, cfg.dates)
    assert len(answers) == 24 and sum(a.accountant for a in answers) == 5
    p = build_problem(to_staff(answers, cfg.dates, 30), cfg.settings, cfg.dates)
    r = solve(p, time_limit=10)
    assert not r.leader_missing
    assert {k for k, v in p.required.items() if v} <= accountant_on_duty(p, r)
    assert to_xlsx(p, r, answers_table(answers, cfg.dates))[:2] == b"PK"
