"""見本のフォーム回答（架空の24人・11/20〜11/23）を nf/sample/NF回答サンプル.csv に作る。

「NF2026 シフト希望調査」の回答をCSVでダウンロードしたときの形をまねたもの（日ごとに「参加できない時間帯」を選ぶ）。
使い方: python nf/make_sample.py
"""
import csv
import datetime as dt
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shift.model import date_label  # noqa: E402

DATES = [dt.date(2026, 11, 20) + dt.timedelta(n) for n in range(4)]
HOURS = list(range(9, 19))  # 9:00-10:00 … 18:00-19:00 の選択肢
NAMES = ["佐藤 陽菜", "鈴木 蓮", "高橋 結衣", "田中 湊", "伊藤 芽依", "渡辺 大翔", "山本 さくら", "中村 悠真",
         "小林 美桜", "加藤 陸", "吉田 心春", "山田 颯太", "佐々木 凛", "山口 樹", "松本 葵", "井上 蒼",
         "木村 紬", "林 律", "斎藤 杏", "清水 朝陽", "森 柚子", "池田 奏", "橋本 楓", "石川 陽向"]
ACCOUNTANTS = {"高橋 結衣", "中村 悠真", "山田 颯太", "木村 紬", "池田 奏"}


def main(path: Path = Path(__file__).with_name("sample") / "NF回答サンプル.csv") -> Path:
    rnd = random.Random(11)
    header = ["タイムスタンプ", "メールアドレス", "氏名", "あなたは会計ですか？"]
    header += [f"{date_label(d)} 参加できない時間帯" for d in DATES]
    rows = []
    for n, name in enumerate(NAMES):
        row = [f"2026/11/{10 + n % 2:02d} {9 + n % 12}:{n:02d}:00", f"user{n + 1:02d}@example.com", name,
               "はい" if name in ACCOUNTANTS else "いいえ"]
        for _ in DATES:
            r = rnd.random()
            if r < 0.45:
                busy = []  # 終日参加できる
            elif r < 0.55:
                busy = HOURS  # 終日参加できない
            else:
                s = rnd.choice(HOURS[:-1])
                busy = HOURS[HOURS.index(s):HOURS.index(s) + rnd.randint(1, 4)]
            row.append(", ".join(f"{h}:00-{h + 1}:00" for h in busy))
        rows.append(row)
    # 答え直し（同じメールアドレスの2回目。最後の回答が使われる）
    redo = list(rows[0])
    redo[0] = "2026/11/11 21:00:00"
    redo[4] = ""
    rows.append(redo)

    path.parent.mkdir(exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows([header] + rows)
    return path


if __name__ == "__main__":
    print(main(), "を作りました")
