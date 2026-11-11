"""画面なしで NF のシフトを作る。

使い方: python nf/cli.py 回答.csv 出力.xlsx
（回答.csv は「NF2026 シフト希望調査」の回答のスプレッドシートを「カンマ区切り形式（.csv）」でダウンロードしたもの。Excelでもよい）
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from nf.loader import (LOCAL_CONFIG_NAME, answers_table, load_nf_config, parse_answers,  # noqa: E402
                       pick_response_sheet, to_staff)
from nf.output import shortage_table, to_xlsx  # noqa: E402
from nf.solver import solve  # noqa: E402
from shift.loader import build_problem, read_file  # noqa: E402


def main(src: str, dst: str) -> int:
    cfg = load_nf_config(ROOT / "config.toml", overlay=ROOT / LOCAL_CONFIG_NAME)
    dfs = read_file(src, Path(src).read_bytes())
    sheet = pick_response_sheet(dfs)
    if sheet is None:
        print("フォームの回答のシートが見つかりません（「氏名」と「あなたは会計ですか？」の列が必要です）")
        return 1
    answers, notes = parse_answers(dfs[sheet], cfg.dates, cfg.single_time_minutes)
    for n in notes:
        print("注意:", n)
    if not answers:
        return 1
    staff = to_staff(answers, cfg.dates, cfg.settings.slot_minutes)
    problem = build_problem(staff, cfg.settings, cfg.dates)
    print(f"{len(staff)}人（会計 {sum(s.leader for s in staff)}人）・{len(cfg.dates)}日")
    t0 = time.time()
    result = solve(problem, cfg.accountant_min)
    print(f"{result.status}（{time.time() - t0:.1f}秒）: {len(result.shifts)}件")
    short = shortage_table(problem, result)
    if not short.empty:
        print("人数不足・会計不在:")
        print(short.to_string(index=False))
    Path(dst).write_bytes(to_xlsx(problem, result, answers_table(answers, cfg.dates)))
    print(f"{dst} に書き出しました")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
