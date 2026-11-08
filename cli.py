"""画面なしでシフトを作る。

使い方: python cli.py 回答.csv 出力.xlsx
（回答.csv はGoogleフォームの回答のスプレッドシートを「カンマ区切り形式（.csv）」でダウンロードしたもの。Excelでもよい）
"""
import sys
import time
from pathlib import Path

from shift.loader import (LOCAL_CONFIG_NAME, build_problem, load_settings, parse_responses, pick_response_sheet,
                          read_file, response_dates)
from shift.output import shortage_table, to_xlsx
from shift.solver import solve


def main(src: str, dst: str) -> int:
    here = Path(__file__)
    settings = load_settings(here.with_name("config.toml"), overlay=here.with_name(LOCAL_CONFIG_NAME))
    dfs = read_file(src, Path(src).read_bytes())
    sheet = pick_response_sheet(dfs)
    if sheet is None:
        print("フォームの回答のシートが見つかりません")
        return 1
    staff, notes = parse_responses(dfs[sheet], settings.slot_minutes)
    for n in notes:
        print("注意:", n)
    if not staff:
        return 1
    problem = build_problem(staff, settings, response_dates(staff))
    t0 = time.time()
    result = solve(problem)
    print(f"{result.status}（{time.time() - t0:.1f}秒）: {len(result.shifts)}件")
    short = shortage_table(problem, result)
    if not short.empty:
        print("人数不足:")
        print(short.to_string(index=False))
    Path(dst).write_bytes(to_xlsx(problem, result))
    print(f"{dst} に書き出しました")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
