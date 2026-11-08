"""Googleログイン（OAuth）でスプレッドシートを読み書きする。

ログインした先生本人の権限で読むので、スプレッドシートを「リンクを知っている全員」に公開する必要はない。
設定は .streamlit/secrets.toml の [google_oauth]（client_id, client_secret, redirect_uri）に置く。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

import pandas as pd

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
STATE_MAX_AGE = 600  # ログイン画面から戻るまでの猶予（秒）


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_mapping(cls, m) -> "OAuthConfig | None":
        """secrets の [google_oauth] から作る。足りない項目があれば None。"""
        try:
            return cls(str(m["client_id"]), str(m["client_secret"]), str(m["redirect_uri"]))
        except (KeyError, TypeError):
            return None


def _sign(cfg: OAuthConfig, payload: str) -> str:
    return hmac.new(cfg.client_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(cfg: OAuthConfig, now: float | None = None) -> str:
    """偽のログイン戻りを弾くための state。

    Googleの画面から戻ると Streamlit のセッションが新しくなるので、
    セッションに保存せず、時刻と乱数に署名を付けて検証できるようにする。
    """
    payload = f"{int(now if now is not None else time.time())}.{secrets.token_urlsafe(8)}"
    return f"{payload}.{_sign(cfg, payload)}"


def check_state(cfg: OAuthConfig, state: str, now: float | None = None) -> bool:
    payload, _, sig = state.rpartition(".")
    if not payload or not hmac.compare_digest(sig, _sign(cfg, payload)):
        return False
    issued = payload.split(".")[0]
    return issued.isdigit() and 0 <= (now if now is not None else time.time()) - int(issued) <= STATE_MAX_AGE


def login_url(cfg: OAuthConfig, state: str) -> str:
    q = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def exchange_code(cfg: OAuthConfig, code: str) -> dict:
    """ログインから戻ってきた code をアクセストークンに換える。"""
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "redirect_uri": cfg.redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def credentials(cfg: OAuthConfig, token: dict):
    from google.oauth2.credentials import Credentials

    return Credentials(token["access_token"], refresh_token=token.get("refresh_token"), token_uri=TOKEN_URL,
                       client_id=cfg.client_id, client_secret=cfg.client_secret, scopes=SCOPES)


def _open(creds, url: str):
    import gspread

    if not re.search(r"/spreadsheets/d/[a-zA-Z0-9_-]+", url):
        raise ValueError("GoogleスプレッドシートのURLではないようです。")
    return gspread.authorize(creds).open_by_url(url)


def values_to_frame(values: list[list[str]]) -> pd.DataFrame:
    """1行目を見出しにして表にする。行ごとに長さが違ってもよい。"""
    if not values:
        return pd.DataFrame()
    header, *rows = values
    width = max(len(header), *(len(r) for r in rows)) if rows else len(header)
    header = header + [""] * (width - len(header))
    return pd.DataFrame([r + [""] * (width - len(r)) for r in rows], columns=header, dtype=str)


def read_sheet(creds, url: str) -> dict[str, pd.DataFrame]:
    """ログインした人の権限でスプレッドシートの全シートを読む。"""
    sh = _open(creds, url)
    titles = [ws.title for ws in sh.worksheets()]
    ranges = sh.values_batch_get(["'" + t.replace("'", "''") + "'" for t in titles]).get("valueRanges", [])
    return {t: values_to_frame(r.get("values", [])) for t, r in zip(titles, ranges)}


def write_sheets(creds, url: str, sheets: dict[str, pd.DataFrame]) -> None:
    """ログインした人の権限で、スプレッドシートにシートを書き込む（同名のシートは上書き）。"""
    sh = _open(creds, url)
    existing = {ws.title: ws for ws in sh.worksheets()}
    for name, df in sheets.items():
        ws = existing.get(name) or sh.add_worksheet(name, rows=max(len(df) + 5, 20), cols=max(len(df.columns) + 2, 10))
        ws.clear()
        if isinstance(df.columns, pd.MultiIndex):
            # 2段の見出しは、曜日の行と講師の行に分けて書く
            header = [[df.index.name or ""] + [c[0] for c in df.columns],
                      [""] + [c[1] for c in df.columns]]
            body = [[i] + row for i, row in zip(df.index, df.astype(str).values.tolist())]
            ws.update(header + body)
        else:
            ws.update([df.columns.tolist()] + df.astype(str).values.tolist())
