import io

from shift import google_sheets as gs


def test_published_csv_url():
    assert gs.is_published_csv("https://docs.google.com/spreadsheets/d/e/2PACX-xxx/pub?gid=0&single=true&output=csv")
    assert not gs.is_published_csv("https://docs.google.com/spreadsheets/d/abc/edit#gid=0")


def test_read_published_csv(monkeypatch):
    body = "氏名,出勤 [10:00]\n佐藤,11/20(金)\n".encode("utf-8-sig")

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda url, timeout: Resp(body))
    dfs = gs.read_published_csv("https://example.com/pub?output=csv")
    assert list(dfs["フォームの回答"].columns) == ["氏名", "出勤 [10:00]"]


def test_service_account_credentials():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    info = {"type": "service_account", "project_id": "p", "private_key_id": "k", "private_key": key,
            "client_email": "shift@p.iam.gserviceaccount.com", "client_id": "1",
            "token_uri": "https://oauth2.googleapis.com/token"}
    creds = gs.service_account_credentials(info)
    assert creds.service_account_email == "shift@p.iam.gserviceaccount.com"
    assert gs.service_account_credentials(None) is None
    import json
    assert gs.service_account_credentials(json.dumps(info)).service_account_email == info["client_email"]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


SCRIPT = "https://script.google.com/macros/s/AKfy/exec?key=abc"


def test_read_script(monkeypatch):
    seen = {}

    def fake(url, timeout):
        seen["url"] = url
        return _Resp('タイムスタンプ,氏名,"出勤 [9:30-10:00]"\n2026/11/10 9:00:00,佐藤,"11/20(金), 11/21(土)"\n'.encode())

    monkeypatch.setattr(gs.urllib.request, "urlopen", fake)
    assert gs.is_script_url(SCRIPT)
    df = gs.read_script(SCRIPT)["フォームの回答"]
    assert seen["url"] == SCRIPT
    assert df.loc[0, "出勤 [9:30-10:00]"] == "11/20(金), 11/21(土)"


def test_read_script_errors(monkeypatch):
    import pytest

    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda url, timeout: _Resp("ERROR: 合言葉が違います".encode()))
    with pytest.raises(ValueError, match="合言葉"):
        gs.read_script(SCRIPT)
    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda url, timeout: _Resp(b"<!DOCTYPE html><html>"))
    with pytest.raises(ValueError, match="全員"):
        gs.read_script(SCRIPT)


def test_write_script(monkeypatch):
    import json

    import pandas as pd

    sent = {}

    def fake(req, timeout):
        sent["body"] = json.loads(req.data)
        sent["method"] = req.get_method()
        return _Resp(b"OK")

    monkeypatch.setattr(gs.urllib.request, "urlopen", fake)
    gs.write_script(SCRIPT, {"集計": pd.DataFrame({"氏名": ["佐藤"], "最大日数": [None], "合計時間": [2.5]})})
    assert sent["method"] == "POST"
    assert sent["body"]["sheets"]["集計"] == [["氏名", "最大日数", "合計時間"], ["佐藤", "", "2.5"]]


def test_form_url_and_shared_sheet_url():
    assert gs.is_form_url("https://docs.google.com/forms/d/e/1FAIpQLSc/viewform?usp=header")
    assert gs.is_form_url("https://forms.gle/abc123")
    assert not gs.is_form_url("https://docs.google.com/spreadsheets/d/abc/edit")
    assert gs.is_sheet_url("https://docs.google.com/spreadsheets/d/abc/edit?usp=sharing")
    assert gs.shared_export_url("https://docs.google.com/spreadsheets/d/1aB-c_d/edit?usp=sharing#gid=0") == \
        "https://docs.google.com/spreadsheets/d/1aB-c_d/export?format=xlsx"
    assert gs.shared_export_url("https://docs.google.com/spreadsheets/d/e/2PACX-x/pubhtml?gid=0") == \
        "https://docs.google.com/spreadsheets/d/e/2PACX-x/pub?output=xlsx"


def test_read_shared_sheet(monkeypatch):
    import pandas as pd
    import pytest

    buf = io.BytesIO()
    with pd.ExcelWriter(buf) as w:
        pd.DataFrame({"お名前": ["佐藤"], "入れる日": ["11/20(金)"]}).to_excel(w, sheet_name="フォームの回答 1", index=False)
    seen = {}

    def fake(url, timeout):
        seen["url"] = url
        return _Resp(buf.getvalue())

    monkeypatch.setattr(gs.urllib.request, "urlopen", fake)
    dfs = gs.read_shared_sheet("https://docs.google.com/spreadsheets/d/abc/edit")
    assert seen["url"].endswith("/d/abc/export?format=xlsx")
    assert dfs["フォームの回答 1"].loc[0, "お名前"] == "佐藤"
    # 共有されていないとログイン画面が返る
    monkeypatch.setattr(gs.urllib.request, "urlopen", lambda url, timeout: _Resp(b"<!DOCTYPE html><html>"))
    with pytest.raises(ValueError, match="リンクを知っている全員"):
        gs.read_shared_sheet("https://docs.google.com/spreadsheets/d/abc/edit")
