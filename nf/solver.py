"""NF用のシフトの自動作成（OR-Tools CP-SAT）。

元のシフト作成（shift/solver.py）と同じルールに、会計係の決まりを足したもの。

必須
- 出勤できる時間（参加できない時間帯の外）にだけ入れる
- 1日の勤務は1回（連続）まで。長さは最短〜延長の上限まで
- 最大日数・上限時間を超えない
- 営業中はいつも会計係を（設定の人数、ふつうは1人）以上入れる
  会計係が誰も入れない時間などどうしても入れられないときだけ、その時間を「会計不在」として出す。
  そのために2回計算する：1回目で会計不在の時間を一番少なくし、2回目はその数を超えない範囲で残りの優先条件を考える

優先（重みは config.toml の [weights]）
- 必要人数の不足をなくす（最優先）
- 原則の長さ（例：2時間）を超えない。超えるのは不足をうめるときだけ
- 人数を多くしすぎない / 勤務時間を偏らせない / 希望勤務時間に近づける / 出勤日を少なく
"""
from __future__ import annotations

from ortools.sat.python import cp_model

from shift.model import Problem, Result, Shift


def _build(problem: Problem, accountant_min: int):
    s = problem.settings
    w = s.weights
    slot = s.slot_minutes
    T = len(problem.times)
    min_len = max(1, -(-s.min_shift_minutes // slot))
    max_len = max(min_len, s.max_shift_minutes // slot)
    normal_len = max(min_len, min(max_len, s.normal_max_minutes // slot))

    m = cp_model.CpModel()
    x = {}
    for p, st in enumerate(problem.staff):
        for d in problem.dates:
            for i, t in enumerate(problem.times):
                if problem.required.get((d, t), 0) > 0 and (d, t) in st.available:
                    x[p, d, i] = m.NewBoolVar(f"x{p}_{d}_{i}")

    penalty = []
    totals = []
    for p, st in enumerate(problem.staff):
        days_worked = []
        total = []
        for d in problem.dates:
            row = [x.get((p, d, i)) for i in range(T)]
            here = [v for v in row if v is not None]
            if not here:
                continue
            starts = []
            for i, v in enumerate(row):
                if v is None:
                    continue
                y = m.NewBoolVar("")
                prev = row[i - 1] if i > 0 else None
                m.Add(y >= v - prev) if prev is not None else m.Add(y >= v)
                # 始まったら最短の長さぶん続ける
                for k in range(min_len):
                    nxt = row[i + k] if i + k < T else None
                    if nxt is None:
                        m.Add(y == 0)
                        break
                    m.AddImplication(y, nxt)
                starts.append(y)
            m.Add(sum(starts) <= 1)
            length = sum(here)
            m.Add(length <= max_len)
            long = m.NewIntVar(0, max_len, "")
            m.Add(long >= length - normal_len)
            penalty.append(w.get("long_shift", 0) * long)
            work = m.NewBoolVar("")
            for v in here:
                m.AddImplication(v, work)
            m.Add(work <= length)
            days_worked.append(work)
            total.extend(here)
        if st.max_days is not None and days_worked:
            m.Add(sum(days_worked) <= st.max_days)
        if st.max_hours is not None and total:
            m.Add(sum(total) <= int(st.max_hours * 60 // slot))
        penalty.append(w.get("work_day", 0) * sum(days_worked))
        tot = m.NewIntVar(0, len(total), "")
        m.Add(tot == sum(total))
        totals.append(tot)
        if st.target_hours is not None:
            target = round(st.target_hours * 60 / slot)
            dev = m.NewIntVar(0, max(target, len(total)), "")
            m.Add(dev >= tot - target)
            m.Add(dev >= target - tot)
            penalty.append(w.get("target_hours", 0) * dev)

    if totals:
        most = m.NewIntVar(0, T * len(problem.dates), "")
        m.AddMaxEquality(most, totals)
        penalty.append(w.get("balance", 0) * most)

    short_vars, miss_vars = {}, {}
    n_staff = len(problem.staff)
    for d in problem.dates:
        for i, t in enumerate(problem.times):
            req = problem.required.get((d, t), 0)
            if req <= 0:
                continue
            here = [x[p, d, i] for p in range(n_staff) if (p, d, i) in x]
            short = m.NewIntVar(0, req, "")
            over = m.NewIntVar(0, n_staff, "")
            m.Add(sum(here) + short >= req)
            m.Add(over >= sum(here) - req)
            short_vars[d, t] = short
            penalty.append(w.get("shortage", 0) * short + w.get("over", 0) * over)
            need = min(accountant_min, req)
            if need > 0:
                acc = [x[p, d, i] for p, st in enumerate(problem.staff) if st.leader and (p, d, i) in x]
                miss = m.NewIntVar(0, need, "")
                m.Add(sum(acc) + miss >= need)
                miss_vars[d, t] = miss
    return m, x, penalty, short_vars, miss_vars


def _solver(seconds: float) -> cp_model.CpSolver:
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(seconds)
    solver.parameters.num_workers = 8
    return solver


def solve(problem: Problem, accountant_min: int = 1, time_limit: float | None = None) -> Result:
    """シフトを作る。Result.leader_missing に会計係が足りない (日付, 枠) を入れる。"""
    s = problem.settings
    limit = float(time_limit or s.time_limit_seconds)
    slot = s.slot_minutes
    T = len(problem.times)

    # 1回目：会計係が足りない時間をできるだけ少なくする
    m, _, _, _, miss_vars = _build(problem, accountant_min)
    least_missing = 0
    if miss_vars:
        m.Minimize(sum(miss_vars.values()))
        solver = _solver(max(3.0, limit / 3))
        code = solver.Solve(m)
        if code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return Result(status="シフトを作れませんでした", shifts=[], shortage={})
        least_missing = round(solver.ObjectiveValue())

    # 2回目：会計不在をそれ以上ふやさずに、人数不足などの優先条件を考える
    m, x, penalty, short_vars, miss_vars = _build(problem, accountant_min)
    if miss_vars:
        m.Add(sum(miss_vars.values()) <= least_missing)
    m.Minimize(sum(penalty))
    solver = _solver(limit)
    code = solver.Solve(m)
    if code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Result(status="シフトを作れませんでした", shifts=[],
                      shortage={k: problem.required[k] for k in short_vars})
    status = "最適なシフトができました" if code == cp_model.OPTIMAL else "計算時間内で一番よいシフトです"

    shifts = []
    for p, st in enumerate(problem.staff):
        for d in problem.dates:
            run = None
            for i in range(T + 1):
                on = i < T and (p, d, i) in x and solver.Value(x[p, d, i])
                if on and run is None:
                    run = i
                elif not on and run is not None:
                    shifts.append(Shift(st.name, d, problem.times[run], problem.times[i - 1] + slot))
                    run = None
    shortage = {k: solver.Value(v) for k, v in short_vars.items() if solver.Value(v)}
    missing = {k for k, v in miss_vars.items() if solver.Value(v)}
    return Result(status=status, shifts=shifts, shortage=shortage, leader_missing=missing)
