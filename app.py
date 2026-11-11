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
from shift.loader import (AVOID, LOCAL_CONFIG_NAME, NO_REQUEST, ROLES, WANT, alias_frame, apply_staff_frame,
                          build_problem, check_rules, column_frame, guess_roles, load_settings, pick_response_sheet,
                          read_alias_frame, read_file, read_responses, request_frame, request_notes, response_dates,
                          staff_frame, update_requests)
from shift.model import Rule, date_label
from shift.output import (all_sheets, day_html, person_table, request_table, shift_table, shortage_table,
                          summary_table, to_xlsx)
from shift.settings_io import save_settings
from shift.solver import solve

ROOT = Path(__file__).parent
WEIGHT_LABELS = {
    "shortage": "人数不足をなくす",
    "want": "「入れたい」の人を入れる",
    "leader_missing": "責任者を入れる",
    "long_shift": "原則の長さを超えない",
    "over": "人数を多くしすぎない",
    "balance": "勤務時間を偏らせない",
    "target_hours": "希望勤務時間に近づける",
    "work_day": "出勤日を少なくする",
}
SRC_FILE, SRC_GOOGLE, SRC_SAMPLE = "ファイル（CSV・Excel）", "Googleスプレッドシート", "サンプルデータ"
SLOT_CHOICES = [15, 30, 60, 120]

st.set_page_config(page_title="シフト作成", layout="wide")

# 見た目：ふだんは白い背景に黒い文字（.streamlit/config.toml）。スイッチを入れた人の画面だけダークモードにする
# （テーマの設定はサーバー全体で共有されるので、ここでは色を反転させる CSS で切り替える）
DARK_CSS = """<style>
html { filter: invert(1) hue-rotate(180deg); background: #fff; }
img, video { filter: invert(1) hue-rotate(180deg); }
</style>"""
title_col, theme_col = st.columns([5, 1], vertical_alignment="bottom")
title_col.title("シフト作成")
if theme_col.toggle("ダークモード", key="dark_mode"):
    st.html(DARK_CSS)

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

with st.sidebar:
    st.header("条件")
    st.subheader("時間刻み")
    slot_opts = sorted(set(SLOT_CHOICES) | {settings.slot_minutes})
    settings.slot_minutes = st.selectbox("時間刻み", slot_opts, slot_opts.index(settings.slot_minutes),
                                         format_func=lambda m: f"{m}分", label_visibility="collapsed")
    st.caption("フォームの刻みより粗くすると、刻みの間ずっと出勤できる人だけを入れます。")


def replace_editor_state(name: str, value: dict) -> None:
    """列の使い方・読み替えが変わったら、読み直して表を作り直す。"""
    if st.session_state.get(name) != value:
        st.session_state[name] = value
        st.session_state["reading_edited"] = True  # 直したあとも「回答の読み方」を開いたままにする
        st.session_state.pop("result", None)
        st.rerun()


# ===== 回答の読み方（ほかの人が作ったフォームでも、列の使い方と選択肢の読み替えを決めれば読める） =====
staff, notes = [], []
if dfs is not None:
    sheet = pick_response_sheet(dfs)
    if len(dfs) > 1:
        sheet = st.selectbox("回答のシート", list(dfs), list(dfs).index(sheet))
    df = dfs[sheet]
    guessed = guess_roles(df)
    overrides = st.session_state.setdefault("column_roles", dict(settings.column_roles))
    roles = {h: overrides.get(h, g) for h, g in guessed.items()}
    aliases = st.session_state.setdefault("choice_aliases", dict(settings.choice_aliases))
    read = read_responses(df, settings.slot_minutes, roles=roles, aliases=aliases)
    staff, notes = read.staff, read.notes
    with st.expander("回答の読み方", expanded=not staff or bool(read.unreadable) or st.session_state.get("reading_edited", False)):
        base = column_frame(df, roles)
        edited_roles = st.data_editor(
            base, hide_index=True, width="stretch", disabled=["列", "回答の例"],
            height=min(36 * (len(base) + 1) + 3, 400),
            column_config={"使い方": st.column_config.SelectboxColumn(options=ROLES, required=True)},
            key=f"roles_{hash(tuple(base.itertuples(index=False)))}",
        )
        new_roles = dict(zip(edited_roles["列"], edited_roles["使い方"]))
        # 推測と同じものは覚えない。この表にない列（ほかのフォーム）の指定は残す
        replace_editor_state("column_roles", {**{h: r for h, r in overrides.items() if h not in guessed},
                                              **{h: r for h, r in new_roles.items() if r != guessed.get(h)}})
        al = alias_frame(read.unreadable, aliases, read.alias_used)
        if not al.empty:
            st.markdown("**選択肢の読み替え**")
            edited_alias = st.data_editor(
                al, hide_index=True, width="stretch", disabled=["選択肢", "回数"],
                column_config={"読み替え": st.column_config.TextColumn(help="例：9:30-12:00 ／ 終日 ／ ×")},
                key=f"alias_{hash(tuple(al.itertuples(index=False)))}",
            )
            replace_editor_state("choice_aliases", read_alias_frame(edited_alias))
            st.caption("読み替えには時刻（9:30-12:00）、日付（11/20）、「終日」、出勤できないなら「×」を書けます。")
found_dates = response_dates(staff)

# ===== 条件 =====
with st.sidebar:
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
    slot = settings.slot_minutes
    step = slot / 60
    c1, c2, c3 = st.columns(3)
    min_h = c1.number_input("最短", step, 12.0, max(step, settings.min_shift_minutes / 60), step=step)
    normal_h = c2.number_input("原則", step, 12.0, max(step, settings.normal_max_minutes / 60), step=step)
    max_h = c3.number_input("延長上限", step, 16.0, max(step, settings.max_shift_minutes / 60), step=step)
    st.caption("「原則」を超えるのは、人数が足りない時間帯をうめるときだけです。1日に入るのは1回までです。")
    settings.min_shift_minutes, settings.normal_max_minutes, settings.max_shift_minutes = (
        round(min_h * 60), round(normal_h * 60), round(max_h * 60))
    shift_errors = []
    if not min_h <= normal_h <= max_h:
        shift_errors.append("勤務の長さは「最短 ≦ 原則 ≦ 延長上限」にしてください。")
    if any(round(h * 60) % slot for h in (min_h, normal_h, max_h)):
        shift_errors.append(f"勤務の長さは時間刻み（{slot}分）の倍数にしてください。")

    st.subheader("必要人数")
    edited = st.data_editor(
        pd.DataFrame([{"曜日": r.days, "開始": r.start, "終了": r.end, "人数": r.people} for r in settings.rules]),
        num_rows="dynamic", hide_index=True, width="stretch",
        column_config={"人数": st.column_config.NumberColumn(min_value=0, step=1)},
    )
    st.caption("下の行ほど優先します。曜日は 毎日・平日・土日祝・日祝・月水金・11/23 などで書けます。")
    settings.rules = [Rule(str(r["曜日"] or "毎日"), str(r["開始"]), str(r["終了"]), int(r["人数"] or 0))
                      for _, r in edited.iterrows() if pd.notna(r["開始"]) and pd.notna(r["終了"])]
    rule_errors = check_rules(settings.rules, settings.slot_minutes) + shift_errors
    for err in rule_errors:
        st.error(err)

    settings.leader_required = st.checkbox("営業中はいつも責任者を1人以上入れる", settings.leader_required)

    with st.expander("優先条件の重み（大きいほど重視）"):
        w = settings.weights
        for key, label in WEIGHT_LABELS.items():
            w[key] = st.number_input(label, 0, 10000, w.get(key, 0), key=f"w_{key}")
    settings.time_limit_seconds = st.number_input("計算時間の上限（秒）", 5, 600, int(settings.time_limit_seconds))

    if st.button("この条件を既定にする", disabled=bool(rule_errors)):
        settings.column_roles = dict(st.session_state.get("column_roles", settings.column_roles))
        settings.choice_aliases = dict(st.session_state.get("choice_aliases", settings.choice_aliases))
        save_settings(settings, LOCAL_CONFIG)
        st.success("保存しました。次回からこの条件（回答の読み方も）が最初から入ります。")

if notes:
    with st.expander(f"入力の確認（{len(notes)}件）", expanded=not staff):
        for n in notes:
            st.warning(n)
if not staff or not period_dates or rule_errors:
    st.stop()

# ===== スタッフ =====
n_answers = len(staff)
with st.expander(f"スタッフ（回答 {n_answers}人）"):
    base = staff_frame(staff)
    edited_staff = st.data_editor(
        base, hide_index=True, width="stretch", disabled=["氏名", "回答日時", "備考"],
        column_config={
            "使う": st.column_config.CheckboxColumn(help="外した人はシフトに入れません"),
            "責任者": st.column_config.CheckboxColumn(),
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

# ===== 日ごとの指定 =====
# 表の人・期間が変わったら作り直す。指定は session_state に残すので、作り直しても消えない
names = [s.name for s in staff]
requests = st.session_state.setdefault("day_requests", {})
sig = (tuple(names), tuple(period_dates))
if st.session_state.get("request_sig") != sig:
    st.session_state.pop(f"request_editor_{st.session_state.get('request_ver', 0)}", None)
    st.session_state["request_sig"] = sig
    st.session_state["request_base"] = request_frame(names, period_dates, requests)
    st.session_state["request_ver"] = st.session_state.get("request_ver", 0) + 1
shown = {k: v for k, v in requests.items() if k[0] in set(names) and k[1] in set(period_dates)}
with st.expander("日ごとの指定（入れたい人・入れない人）", expanded=bool(shown)):
    edited_requests = st.data_editor(
        st.session_state["request_base"], hide_index=True, width="stretch", disabled=["氏名"],
        height=min(36 * (len(names) + 1) + 3, 800),
        column_config={"氏名": st.column_config.TextColumn(pinned=True),
                       **{date_label(d): st.column_config.SelectboxColumn(options=[NO_REQUEST, WANT, AVOID], required=True)
                          for d in period_dates}},
        key=f"request_editor_{st.session_state['request_ver']}",
    )
    requests = st.session_state["day_requests"] = update_requests(requests, edited_requests, names, period_dates)
    st.caption(f"「{AVOID}」の人はその日に入れません。「{WANT}」の人は、フォームで出勤できると答えた時間の中で"
               "その日に入れます（人数不足をうめるほうを優先します）。")

problem = build_problem(staff, settings, period_dates, requests)
for n in request_notes(problem):
    st.warning(n)
total_req_h = sum(problem.required.values()) * settings.slot_minutes / 60
c1, c2, c3, c4 = st.columns(4)
c1.metric("スタッフ", f"{len(staff)}人" if len(staff) == n_answers else f"{len(staff)}人／{n_answers}人")
c2.metric("期間", f"{len(period_dates)}日")
c3.metric("必要な人数×時間", f"{total_req_h:g}時間")
c4.metric("日ごとの指定", f"{WANT} {len(problem.want)}・{AVOID} {len(problem.avoid)}")

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
if result.want_missed:
    missed = "、".join(f"{date_label(d)} {n}" for n, d in result.want_missed)
    st.warning(f"「{WANT}」にしたのに入れられなかった人がいます：{missed}（「日ごとの指定」タブ）")

short_df = shortage_table(problem, result)
req_df = request_table(problem, result)
tabs = st.tabs(["シフト表", "日別", "個人別", "集計", f"人数不足（{len(short_df)}）", f"日ごとの指定（{len(req_df)}）"])
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
with tabs[5]:
    if req_df.empty:
        st.info("指定はありません。")
    else:
        st.dataframe(req_df, width="stretch", hide_index=True)

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
