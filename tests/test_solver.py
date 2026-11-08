import datetime as dt

from shift.loader import build_problem
from shift.model import Rule, Settings, Staff
from shift.solver import solve

D = dt.date(2026, 11, 16)
W = {"shortage": 1000, "leader_missing": 300, "long_shift": 200, "over": 10, "balance": 20,
     "target_hours": 5, "work_day": 2}


def settings(rules, **kw):
    base = dict(slot_minutes=30, min_shift_minutes=60, normal_max_minutes=120, max_shift_minutes=480,
                rules=rules, time_limit_seconds=10, weights=dict(W))
    base.update(kw)
    return Settings(**base)


def person(name, start, end, dates=(D,), **kw):
    return Staff(name, {(d, t) for d in dates for t in range(start, end, 30)}, **kw)


def test_shifts_stay_within_two_hours_when_enough_people():
    s = settings([Rule("毎日", "10:00", "16:00", 1)])
    staff = [person(n, 600, 960) for n in "ABC"]
    r = solve(build_problem(staff, s, [D]))
    assert not r.shortage
    assert all(x.hours <= 2 for x in r.shifts)
    assert sum(x.hours for x in r.shifts) == 6


def test_extend_only_to_fill_shortage():
    s = settings([Rule("毎日", "10:00", "14:00", 1)])
    staff = [person("A", 600, 840), person("B", 600, 720)]
    r = solve(build_problem(staff, s, [D]))
    assert not r.shortage
    a = [x for x in r.shifts if x.name == "A"][0]
    assert a.hours == 2  # B が 10-12、A が 12-14 で足りるので延長しない
    staff = [person("A", 600, 840)]
    r = solve(build_problem(staff, s, [D]))
    assert not r.shortage and r.shifts[0].hours == 4  # 1人しかいないので延長する


def test_one_shift_per_day_and_max_days():
    s = settings([Rule("毎日", "10:00", "11:00", 1), Rule("毎日", "15:00", "16:00", 1)])
    d2 = D + dt.timedelta(1)
    staff = [person("A", 600, 960, dates=(D, d2), max_days=1)]
    r = solve(build_problem(staff, s, [D, d2]))
    assert len(r.shifts) == 1 and r.shortage


def test_leader_required():
    s = settings([Rule("毎日", "10:00", "12:00", 1)], leader_required=True)
    staff = [person("A", 600, 720), person("L", 600, 720, leader=True)]
    r = solve(build_problem(staff, s, [D]))
    assert {x.name for x in r.shifts} == {"L"} and not r.leader_missing
