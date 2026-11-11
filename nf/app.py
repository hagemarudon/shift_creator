"""NF2026 シフト作成の画面。「NF2026 シフト希望調査」（Googleフォーム）の回答から作る。

起動: streamlit run nf/app.py
"""
import datetime as dt
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from nf.loader import (LOCAL_CONFIG_NAME, answers_table, apply_staff_frame, load_nf_config,  # noqa: E402
                       parse_answers, pick_response_sheet, save_nf_config, staff_frame, to_staff)
from nf.output import (all_sheets, day_html, person_table, shift_table, shortage_table,  # noqa: E402
                       summary_table, to_xlsx)
from nf.solver import solve  # noqa: E402
from shift import google_sheets as gs  # noqa: E402
from shift.loader import build_problem, check_rules, read_file  # noqa: E402
from shift.model import Rule, date_label  # noqa: E402

WEIGHT_LABELS = {
    "shortage": "人数不足をなくす",
    "long_shift": "原則の長さを超えない",
    "over": "人数を多くしすぎない",
    "balance": "勤務時間を偏らせない",
    "target_hours": "希望勤務時間に近づける",
    "work_day": "出勤日を少なくする",
}
SRC_FILE, SRC_GOOGLE, SRC_SAMPLE = "ファイル（CSV・Excel）", "Googleスプレッドシート", "サンプルデータ"

st.set_page_config(page_title="NF2026 シフト作成", layout="wide")
st.title("NF2026 シフト作成")

LOCAL_CONFIG = ROOT / LOCAL_CONFIG_NAME
cfg = load_nf_config(ROOT / "config.toml", overlay=LOCAL_CONFIG)
settings = cfg.settings


def secret(name: str):
    try:
        return st.secrets.get(name)
    except Exception:  # noqa: BLE001  secrets.toml が無いとき
        return None


# ===== 入力 =====
service_creds = gs.service_account_credentials(secret("gcp_service_account") or secret("gcp_service_account_json"))
default_url = str((secret("responses") or {}).get("url", ""))
sources = [SRC_GOOGLE, SRC_FILE, SRC_SAMPLE] if (service_creds or default_url) else [SRC_FILE, SRC_GOOGLE, SRC_SAMPLE]
source = st.radio("フォームの回答", sources, horizontal=True, key="source")
dfs = None
sheet_url = ""
try:
    if source == SRC_FILE:
        up = st.file_uploader("回答のファイル", type=["csv", "xlsx"], label_visibility="collapsed")
        if up:
            dfs = read_file(up.name, up.getvalue())
        st.caption("回答のスプレッドシートで「ファイル → ダウンロード → カンマ区切り形式（.csv）」で保存したものを選べます。")
    elif source == SRC_GOOGLE:
        c1, c2 = st.columns([4, 1])
        sheet_url = c1.text_input("回答のURL", default_url,
                                  type="password" if gs.is_script_url(default_url) else "default",
                                  help="Apps Script のウェブアプリのURL（?key=合言葉 付き）、ウェブに公開したCSVのURL、"
                                       "またはサービスアカウントに共有したスプレッドシートのURL")
        cached = st.session_state.get("google_dfs")
        if c2.button("再読み込み", width="stretch") or (sheet_url and (not cached or cached[0] != sheet_url)):
            if gs.is_script_url(sheet_url):
                cached = (sheet_url, gs.read_script(sheet_url))
            elif gs.is_published_csv(sheet_url):
                cached = (sheet_url, gs.read_published_csv(sheet_url))
            elif service_creds is not None:
                cached = (sheet_url, gs.read_sheet(service_creds, sheet_url))
            else:
                raise ValueError("このURLは読めません。README の「回答を自動で読み込む」の設定をするか、CSVで読み込んでください。")
            st.session_state["google_dfs"] = cached
            st.session_state.pop("result", None)
        if cached and cached[0] == sheet_url:
            dfs = cached[1]
    else:
        sample = ROOT / "sample" / "NF回答サンプル.csv"
        dfs = read_file(sample.name, sample.read_bytes())
        st.download_button("サンプルの回答をダウンロード", sample.read_bytes(), sample.name)
except Exception as e:  # noqa: BLE001
    st.error(f"読み込めませんでした：{e}")

# ===== 条件 =====
with st.sidebar:
    st.header("条件")
    st.subheader("シフトを組む日")
    default_period = (cfg.dates[0], cfg.dates[-1]) if cfg.dates else ()
    period = st.date_input("期間", default_period, label_visibility="collapsed")
    period_dates = []
    if isinstance(period, (tuple, list)) and len(period) == 2:
        period_dates = [period[0] + dt.timedelta(n) for n in range((period[1] - period[0]).days + 1)]
    cfg.dates = period_dates
    settings.holidays = set(st.multiselect("祝日として扱う日", period_dates, [d for d in period_dates
                                                                     if d in settings.holidays],
                                           format_func=date_label, placeholder="なし"))
    st.caption("フォームで何も選ばなかった日は、終日参加できるものとします。")

    st.subheader("1回の勤務（時間）")
    c1, c2, c3 = st.columns(3)
    min_h = c1.number_input("最短", 0.5, 12.0, settings.min_shift_minutes / 60, step=0.5)
    normal_h = c2.number_input("原則", 0.5, 12.0, settings.normal_max_minutes / 60, step=0.5)
    max_h = c3.number_input("延長上限", 0.5, 16.0, settings.max_shift_minutes / 60, step=0.5)
    st.caption("「原則」を超えるのは、人数が足りない時間帯をうめるときだけです。1日に入るのは1回までです。")
    settings.min_shift_minutes, settings.normal_max_minutes, settings.max_shift_minutes = (
        round(min_h * 60), round(normal_h * 60), round(max_h * 60))
    shift_errors = []
    if not min_h <= normal_h <= max_h:
        shift_errors.append("勤務の長さは「最短 ≦ 原則 ≦ 延長上限」にしてください。")

    st.subheader("必要人数（会計係をふくむ）")
    edited = st.data_editor(
        pd.DataFrame([{"曜日": r.days, "開始": r.start, "終了": r.end, "人数": r.people} for r in settings.rules]),
        num_rows="dynamic", hide_index=True, width="stretch",
        column_config={"人数": st.column_config.NumberColumn(min_value=0, step=1)},
    )
    st.caption("下の行ほど優先します。曜日は 毎日・土日祝・金・11/23 などで書けます。人数が0の時間は営業時間外です。")
    settings.rules = [Rule(str(r["曜日"] or "毎日"), str(r["開始"]), str(r["終了"]), int(r["人数"] or 0))
                      for _, r in edited.iterrows() if pd.notna(r["開始"]) and pd.notna(r["終了"])]
    rule_errors = check_rules(settings.rules) + shift_errors
    for err in rule_errors:
        st.error(err)

    cfg.accountant_min = int(st.number_input("営業中いつも入れる会計係の人数", 1, 10, max(1, cfg.accountant_min),
                                             step=1))

    with st.expander("優先条件の重み（大きいほど重視）"):
        w = settings.weights
        for key, label in WEIGHT_LABELS.items():
            w[key] = st.number_input(label, 0, 10000, w.get(key, 0), key=f"w_{key}")
        st.caption("会計係を入れることは、どの重みよりも先に守ります。")
    cfg.single_time_minutes = int(st.number_input("「10:00」のように時刻が1つの選択肢の長さ（分）", 15, 240,
                                                  cfg.single_time_minutes, step=15))
    settings.time_limit_seconds = st.number_input("計算時間の上限（秒）", 5, 600, int(settings.time_limit_seconds))

    if st.button("この条件を既定にする", disabled=bool(rule_errors) or not period_dates):
        save_nf_config(cfg, LOCAL_CONFIG)
        st.success("保存しました。次回からこの条件が最初から入ります。")

answers, notes = [], []
if dfs is not None:
    sheet = pick_response_sheet(dfs)
    if sheet is None:
        notes = ["フォームの回答のシートが見つかりません（「氏名」と「あなたは会計ですか？」の列が必要です）。"]
    else:
        answers, notes = parse_answers(dfs[sheet], period_dates, cfg.single_time_minutes)

if notes:
    with st.expander(f"入力の確認（{len(notes)}件）", expanded=not answers):
        for n in notes:
            st.warning(n)
if not answers or not period_dates or rule_errors:
    st.stop()

with st.expander("回答の読み取り結果（参加できない時間帯）"):
    st.dataframe(answers_table(answers, period_dates), width="stretch", hide_index=True)

# ===== スタッフ =====
staff = to_staff(answers, period_dates, settings.slot_minutes)
n_answers = len(staff)
with st.expander(f"スタッフ（回答 {n_answers}人・会計 {sum(s.leader for s in staff)}人）"):
    edited_staff = st.data_editor(
        staff_frame(staff), hide_index=True, width="stretch", disabled=["氏名", "回答日時", "備考"],
        column_config={
            "使う": st.column_config.CheckboxColumn(help="外した人はシフトに入れません"),
            "会計": st.column_config.CheckboxColumn(help="フォームで「はい」と答えた人に付いています"),
            "最大日数": st.column_config.NumberColumn("最大日数", min_value=0, step=1),
            "希望時間": st.column_config.NumberColumn("希望時間（期間中）", min_value=0.0, step=0.5),
            "上限時間": st.column_config.NumberColumn("上限時間（期間中）", min_value=0.0, step=0.5),
        },
        key="staff_editor",
    )
    staff = apply_staff_frame(staff, edited_staff)
if not staff:
    st.warning("「使う」にチェックの入った人がいません。")
    st.stop()
if not any(s.leader for s in staff):
    st.warning("会計係が1人もいません。スタッフの表で「会計」に印を付けてください。")

problem = build_problem(staff, settings, period_dates)
total_req_h = sum(problem.required.values()) * settings.slot_minutes / 60
c1, c2, c3, c4 = st.columns(4)
c1.metric("スタッフ", f"{len(staff)}人" if len(staff) == n_answers else f"{len(staff)}人／{n_answers}人")
c2.metric("会計係", f"{sum(s.leader for s in staff)}人")
c3.metric("期間", f"{len(period_dates)}日")
c4.metric("必要な人数×時間", f"{total_req_h:g}時間")

if st.button("シフトを作る", type="primary"):
    with st.spinner("シフトを計算中…"):
        t0 = time.time()
        st.session_state["result"] = (problem, solve(problem, cfg.accountant_min), time.time() - t0)

if "result" not in st.session_state:
    st.stop()

problem, result, elapsed = st.session_state["result"]
slot_h = problem.settings.slot_minutes / 60
short_h = sum(result.shortage.values()) * slot_h
miss_h = len(result.leader_missing) * slot_h
assigned_h = sum(s.hours for s in result.shifts)
head = f"{result.status}（{elapsed:.1f}秒）：{len(result.shifts)}件・{assigned_h:g}時間を配置。"
if not result.shifts and result.shortage:
    st.error(f"{result.status}（{elapsed:.1f}秒）")
elif miss_h:
    st.error(head + f"会計係がいない時間が延べ {miss_h:g} 時間あります（「人数不足・会計不在」タブ）。"
             "その時間に入れる会計係がいないか、勤務の長さ・最大日数の条件で入れられません。")
elif short_h:
    st.warning(head + f"会計係はいつもいます。人数が足りない時間が延べ {short_h:g} 時間あります（「人数不足・会計不在」タブ）。")
else:
    st.success(head + "会計係はいつもいて、人数もすべて足りています。")

short_df = shortage_table(problem, result)
tabs = st.tabs(["シフト表", "日別", "個人別", "集計", f"人数不足・会計不在（{len(short_df)}）"])
with tabs[0]:
    st.dataframe(shift_table(problem, result), width="stretch")
with tabs[1]:
    d = st.selectbox("日付", problem.dates, format_func=date_label)
    st.markdown(day_html(problem, result, d), unsafe_allow_html=True)
    st.caption("【会計】は会計係です。オレンジは「原則」の長さを超えて延長した勤務、赤は人数が足りない時間・会計係がいない時間です。")
with tabs[2]:
    name = st.selectbox("スタッフ", [s.name for s in problem.staff])
    st.dataframe(person_table(problem, result).query("氏名 == @name").drop(columns=["氏名", "会計"]),
                 width="stretch", hide_index=True)
with tabs[3]:
    st.dataframe(summary_table(problem, result), width="stretch", hide_index=True)
with tabs[4]:
    if short_df.empty:
        st.info("会計係はいつもいて、人数もすべて足りています。")
    else:
        st.dataframe(short_df, width="stretch", hide_index=True)

st.subheader("書き出し")
first, last = problem.dates[0], problem.dates[-1]
st.download_button("Excelでダウンロード", to_xlsx(problem, result, answers_table(answers, period_dates)),
                   f"NFシフト_{first:%m%d}-{last:%m%d}.xlsx", type="primary")
if source == SRC_GOOGLE and sheet_url and (gs.is_script_url(sheet_url) or
                                           (service_creds is not None and not gs.is_published_csv(sheet_url))):
    if st.button("元のGoogleスプレッドシートに書き込む"):
        try:
            if gs.is_script_url(sheet_url):
                gs.write_script(sheet_url, all_sheets(problem, result))
            else:
                gs.write_sheets(service_creds, sheet_url, all_sheets(problem, result))
            st.success("「シフト表」などのシートを書き込みました。")
        except Exception as e:  # noqa: BLE001
            st.error(f"書き込めませんでした：{e}")
