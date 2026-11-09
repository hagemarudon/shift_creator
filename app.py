"""バイトのシフト作成（試作版）の画面。

起動: streamlit run app.py
"""
import datetime as dt
import html
import time
from pathlib import Path

import pandas as pd
import streamlit as st

from shift import google_sheets as gs
from shift.loader import (LOCAL_CONFIG_NAME, apply_staff_frame, build_problem, check_rules, load_settings,
                          parse_responses, pick_response_sheet, read_file, response_dates, staff_frame)
from shift.model import Rule, date_label
from shift.output import (all_sheets, day_html, person_table, shift_table, shortage_table, summary_table,
                          to_xlsx)
from shift.settings_io import save_settings
from shift.solver import solve

ROOT = Path(__file__).parent
WEIGHT_LABELS = {
    "shortage": "人数不足をなくす",
    "leader_missing": "責任者を入れる",
    "long_shift": "原則の長さを超えない",
    "over": "人数を多くしすぎない",
    "balance": "勤務時間を偏らせない",
    "target_hours": "希望勤務時間に近づける",
    "work_day": "出勤日を少なくする",
}
SRC_FILE, SRC_GOOGLE, SRC_SAMPLE = "ファイル（CSV・Excel）", "Googleスプレッドシート", "サンプルデータ"

st.set_page_config(page_title="シフト作成", layout="wide")
st.title("シフト作成")

LOCAL_CONFIG = ROOT / LOCAL_CONFIG_NAME
settings = load_settings(ROOT / "config.toml", overlay=LOCAL_CONFIG)


def google_oauth_config() -> gs.OAuthConfig | None:
    try:
        return gs.OAuthConfig.from_mapping(st.secrets.get("google_oauth"))
    except Exception:  # noqa: BLE001  secrets.toml が無いとき
        return None


def google_credentials(cfg: gs.OAuthConfig):
    token = st.session_state.get("google_token")
    return gs.credentials(cfg, token) if token else None


oauth = google_oauth_config()
if oauth and "code" in st.query_params:
    if gs.check_state(oauth, st.query_params.get("state", "")):
        try:
            st.session_state["google_token"] = gs.exchange_code(oauth, st.query_params["code"])
            st.session_state["source"] = SRC_GOOGLE
        except Exception as e:  # noqa: BLE001
            st.error(f"Googleにログインできませんでした：{e}")
    else:
        st.error("ログインの有効期限が切れました。もう一度「Googleでログイン」を押してください。")
    st.query_params.clear()

# ===== 入力 =====
def secret(name: str):
    try:
        return st.secrets.get(name)
    except Exception:  # noqa: BLE001  secrets.toml が無いとき
        return None


# Google Cloud でダウンロードしたJSONを、そのまま gcp_service_account_json に貼ってもよい
service_creds = gs.service_account_credentials(secret("gcp_service_account") or secret("gcp_service_account_json"))
default_url = str((secret("responses") or {}).get("url", ""))
sources = [SRC_GOOGLE, SRC_FILE, SRC_SAMPLE] if (service_creds or default_url) else [SRC_FILE, SRC_GOOGLE, SRC_SAMPLE]
source = st.radio("フォームの回答", sources, horizontal=True, key="source")
dfs = None
sheet_url = ""
write_creds = None
write_script_url = ""
try:
    if source == SRC_FILE:
        up = st.file_uploader("回答のファイル", type=["csv", "xlsx"], label_visibility="collapsed")
        if up:
            dfs = read_file(up.name, up.getvalue())
        st.caption("回答のスプレッドシートで「ファイル → ダウンロード → カンマ区切り形式（.csv）」で保存したものを選べます。")
    elif source == SRC_GOOGLE and (service_creds or default_url):
        # ログインなし：サービスアカウントに共有したシート、またはウェブに公開したCSVを読む
        c1, c2 = st.columns([4, 1])
        sheet_url = c1.text_input("回答のURL", default_url, type="password" if gs.is_script_url(default_url) else "default")
        if gs.is_script_url(sheet_url):
            write_script_url = sheet_url
        elif service_creds is not None and not gs.is_published_csv(sheet_url):
            write_creds = service_creds
            st.caption(f"スプレッドシートを {service_creds.service_account_email} に共有（編集者）しておくと読めます。")
        cached = st.session_state.get("google_dfs")
        if c2.button("再読み込み", width="stretch") or (sheet_url and (not cached or cached[0] != sheet_url)):
            if gs.is_script_url(sheet_url):
                cached = (sheet_url, gs.read_script(sheet_url))
            elif gs.is_published_csv(sheet_url):
                cached = (sheet_url, gs.read_published_csv(sheet_url))
            elif service_creds is None:
                raise ValueError("このURLは読めません。READMEの「回答を自動で読み込む」の設定をしてください。")
            else:
                cached = (sheet_url, gs.read_sheet(service_creds, sheet_url))
            st.session_state["google_dfs"] = cached
            st.session_state.pop("result", None)
        if cached and cached[0] == sheet_url:
            dfs = cached[1]
    elif source == SRC_GOOGLE:
        if oauth is None:
            st.info("Googleスプレッドシートから読む設定がまだです。設定方法は README の「回答を自動で読み込む」にあります。"
                    "それまではCSVかExcelで読み込めます。")
        elif google_credentials(oauth) is None:
            url = html.escape(gs.login_url(oauth, gs.make_state(oauth)))
            st.markdown(f'<a href="{url}" target="_self" style="display:inline-block;padding:6px 14px;border-radius:6px;'
                        f'border:1px solid #aaa;text-decoration:none">Googleでログイン</a>', unsafe_allow_html=True)
        else:
            c1, c2 = st.columns([4, 1])
            sheet_url = c1.text_input("スプレッドシートのURL")
            if c2.button("ログアウト"):
                for k in ("google_token", "google_dfs"):
                    st.session_state.pop(k, None)
                st.rerun()
            cached = st.session_state.get("google_dfs")
            if sheet_url and st.button("読み込む"):
                cached = st.session_state["google_dfs"] = (sheet_url, gs.read_sheet(google_credentials(oauth), sheet_url))
            if cached and cached[0] == sheet_url:
                dfs = cached[1]
                write_creds = google_credentials(oauth)
    else:
        sample = ROOT / "sample" / "フォーム回答サンプル.csv"
        dfs = read_file(sample.name, sample.read_bytes())
        st.download_button("サンプルの回答をダウンロード", sample.read_bytes(), sample.name)
except Exception as e:  # noqa: BLE001
    name = type(e).__name__
    if name in ("PermissionError", "SpreadsheetNotFound", "APIError") and service_creds is not None:
        st.error("スプレッドシートを読めませんでした。上のメールアドレスに共有されているか、URLが合っているか確かめてください。")
    else:
        st.error(f"読み込めませんでした：{e}")

staff, notes = [], []
if dfs is not None:
    sheet = pick_response_sheet(dfs)
    if sheet is None:
        notes = ["フォームの回答のシートが見つかりません（「氏名」の列と、出勤できる時間のグリッドの列が必要です）。"]
    else:
        staff, notes = parse_responses(dfs[sheet], settings.slot_minutes)
found_dates = response_dates(staff)

# ===== 条件 =====
with st.sidebar:
    st.header("条件")
    st.subheader("期間")
    if found_dates:
        period = st.date_input("期間", (found_dates[0], found_dates[-1]), label_visibility="collapsed")
    else:
        period = ()
    period_dates = []
    if isinstance(period, (tuple, list)) and len(period) == 2:
        period_dates = [period[0] + dt.timedelta(n) for n in range((period[1] - period[0]).days + 1)]
    settings.holidays = set(st.multiselect("祝日として扱う日", period_dates, format_func=date_label, placeholder="なし"))

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

    st.subheader("必要人数")
    edited = st.data_editor(
        pd.DataFrame([{"曜日": r.days, "開始": r.start, "終了": r.end, "人数": r.people} for r in settings.rules]),
        num_rows="dynamic", hide_index=True, width="stretch",
        column_config={"人数": st.column_config.NumberColumn(min_value=0, step=1)},
    )
    st.caption("下の行ほど優先します。曜日は 毎日・平日・土日祝・日祝・月水金・11/23 などで書けます。")
    settings.rules = [Rule(str(r["曜日"] or "毎日"), str(r["開始"]), str(r["終了"]), int(r["人数"] or 0))
                      for _, r in edited.iterrows() if pd.notna(r["開始"]) and pd.notna(r["終了"])]
    rule_errors = check_rules(settings.rules) + shift_errors
    for err in rule_errors:
        st.error(err)

    settings.leader_required = st.checkbox("営業中はいつも責任者を1人以上入れる", settings.leader_required)

    with st.expander("優先条件の重み（大きいほど重視）"):
        w = settings.weights
        for key, label in WEIGHT_LABELS.items():
            w[key] = st.number_input(label, 0, 10000, w.get(key, 0), key=f"w_{key}")
    settings.time_limit_seconds = st.number_input("計算時間の上限（秒）", 5, 600, int(settings.time_limit_seconds))

    if st.button("この条件を既定にする", disabled=bool(rule_errors)):
        save_settings(settings, LOCAL_CONFIG)
        st.success("保存しました。次回からこの条件が最初から入ります。")

if notes:
    with st.expander(f"入力の確認（{len(notes)}件）", expanded=not staff):
        for n in notes:
            st.warning(n)
if not staff or not period_dates or rule_errors:
    st.stop()

# ===== スタッフ =====
with st.expander(f"スタッフ（{len(staff)}人）"):
    base = staff_frame(staff)
    edited_staff = st.data_editor(
        base, hide_index=True, width="stretch", disabled=["氏名", "備考"],
        column_config={
            "責任者": st.column_config.CheckboxColumn(),
            "最大日数": st.column_config.NumberColumn("最大日数", min_value=0, step=1),
            "希望時間": st.column_config.NumberColumn("希望時間（期間中）", min_value=0.0, step=0.5),
            "上限時間": st.column_config.NumberColumn("上限時間（期間中）", min_value=0.0, step=0.5),
        },
        key="staff_editor",
    )
    staff = apply_staff_frame(staff, edited_staff)

problem = build_problem(staff, settings, period_dates)
total_req_h = sum(problem.required.values()) * settings.slot_minutes / 60
c1, c2, c3 = st.columns(3)
c1.metric("スタッフ", f"{len(staff)}人")
c2.metric("期間", f"{len(period_dates)}日")
c3.metric("必要な人数×時間", f"{total_req_h:g}時間")

if st.button("シフトを作る", type="primary"):
    with st.spinner("シフトを計算中…"):
        t0 = time.time()
        st.session_state["result"] = (problem, solve(problem), time.time() - t0)

if "result" not in st.session_state:
    st.stop()

problem, result, elapsed = st.session_state["result"]
short_h = sum(result.shortage.values()) * problem.settings.slot_minutes / 60
assigned_h = sum(s.hours for s in result.shifts)
if not result.shifts and result.shortage:
    st.error(f"{result.status}（{elapsed:.1f}秒）")
elif short_h:
    st.warning(f"{result.status}（{elapsed:.1f}秒）：{len(result.shifts)}件・{assigned_h:g}時間を配置。"
               f"人数が足りない時間が延べ {short_h:g} 時間あります（「人数不足」タブ）。")
else:
    st.success(f"{result.status}（{elapsed:.1f}秒）：{len(result.shifts)}件・{assigned_h:g}時間を配置。人数はすべて足りています。")

short_df = shortage_table(problem, result)
tabs = st.tabs(["シフト表", "日別", "個人別", "集計", f"人数不足（{len(short_df)}）"])
with tabs[0]:
    st.dataframe(shift_table(problem, result), width="stretch")
with tabs[1]:
    d = st.selectbox("日付", problem.dates, format_func=date_label)
    st.markdown(day_html(problem, result, d), unsafe_allow_html=True)
    st.caption("オレンジは「原則」の長さを超えて延長した勤務、赤は人数が足りない時間です。")
with tabs[2]:
    name = st.selectbox("スタッフ", [s.name for s in problem.staff])
    st.dataframe(person_table(problem, result).query("氏名 == @name").drop(columns="氏名"),
                 width="stretch", hide_index=True)
with tabs[3]:
    st.dataframe(summary_table(problem, result), width="stretch", hide_index=True)
with tabs[4]:
    if short_df.empty:
        st.info("人数はすべて足りています。")
    else:
        st.dataframe(short_df, width="stretch", hide_index=True)

st.subheader("書き出し")
first, last = problem.dates[0], problem.dates[-1]
st.download_button("Excelでダウンロード", to_xlsx(problem, result),
                   f"シフト_{first:%m%d}-{last:%m%d}.xlsx", type="primary")
if source == SRC_GOOGLE and sheet_url and (write_creds is not None or write_script_url):
    if st.button("元のGoogleスプレッドシートに書き込む"):
        try:
            if write_script_url:
                gs.write_script(write_script_url, all_sheets(problem, result))
            else:
                gs.write_sheets(write_creds, sheet_url, all_sheets(problem, result))
            st.success("「シフト表」などのシートを書き込みました。")
        except Exception as e:  # noqa: BLE001
            st.error(f"書き込めませんでした：{e}")
