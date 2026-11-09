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
