"""見本のフォーム回答（架空のスタッフ20人・2週間）を sample/フォーム回答サンプル.csv に作る。

Googleフォームの回答をCSVでダウンロードしたときと同じ形（グリッドの列見出しが「質問 [10:00-10:30]」）。
"""
import csv
import datetime as dt
import random
from pathlib import Path

from shift.model import date_label, to_hhmm

START = dt.date(2026, 11, 16)
DAYS = 14
OPEN, CLOSE, SLOT = 10 * 60, 22 * 60, 30
NAMES = ["佐藤 陽菜", "鈴木 蓮", "高橋 結衣", "田中 湊", "伊藤 芽依", "渡辺 大翔", "山本 さくら", "中村 悠真",
         "小林 美桜", "加藤 陸", "吉田 心春", "山田 颯太", "佐々木 凛", "山口 樹", "松本 葵", "井上 蒼",
         "木村 紬", "林 律", "斎藤 杏", "清水 朝陽"]


def week_title(first: dt.date) -> str:
    last = first + dt.timedelta(6)
    return f"出勤できる時間（{first.month}/{first.day}〜{last.month}/{last.day}）"


def main(path: Path = Path(__file__).with_name("sample") / "フォーム回答サンプル.csv") -> Path:
    rnd = random.Random(7)
    dates = [START + dt.timedelta(n) for n in range(DAYS)]
    times = list(range(OPEN, CLOSE, SLOT))
    weeks = [dates[i:i + 7] for i in range(0, DAYS, 7)]
    header = ["タイムスタンプ", "氏名", "期間中に入れる最大日数（任意）", "期間中の希望勤務時間（任意）"]
    for wk in weeks:
        header += [f"{week_title(wk[0])} [{to_hhmm(t)}-{to_hhmm(t + SLOT)}]" for t in times]
    header.append("備考（任意）")

    rows = []
    for n, name in enumerate(NAMES):
        # 学生は夕方中心、主婦・フリーターは昼中心、などをざっくり真似る
        kind = rnd.choice(["昼", "夕", "通し"])
        lo, hi = {"昼": (10, 16), "夕": (16, 22), "通し": (10, 22)}[kind]
        avail = set()
        for d in dates:
            if rnd.random() < 0.3:
                continue
            s = rnd.randint(lo, hi - 3) * 60 + rnd.choice([0, 30])
            e = min(CLOSE, s + rnd.randint(4, 9) * 60)
            avail |= {(d, t) for t in times if s <= t < e}
        row = [f"2026/11/0{1 + n % 7} {9 + n % 12}:{n:02d}:00", name,
               str(rnd.choice(["", "", "4", "6", "8"])), str(rnd.choice(["", "", "10", "15", "20"]))]
        for wk in weeks:
            for t in times:
                row.append(", ".join(date_label(d) for d in wk if (d, t) in avail))
        row.append(rnd.choice(["", "", "テスト期間は少なめで", "土日多めに入れます"]))
        rows.append(row)
    # 答え直し（同じ氏名の2回目。最後の回答が使われる）
    redo = list(rows[0])
    redo[0] = "2026/11/08 21:00:00"
    redo[2] = "5"
    rows.append(redo)

    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows([header] + rows)
    return path


if __name__ == "__main__":
    print(main(), "を作りました")
