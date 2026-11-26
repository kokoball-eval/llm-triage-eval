"""망 접속 모드 시험 (A: 시연용 휴대폰 접속) — auth.Login, 로그인이 필요한 화면, 실행 명령 --lan.

실제 망은 쓰지 않는다. TestClient 의 접속 기기 주소는 "testclient"(이 PC 가 아닌 기기)라 휴대폰에서 온 요청처럼 다뤄진다.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_milestone import fake, store   # noqa: F401 — store 는 픽스처
from triage_service import auth, cli, config
from triage_service.web.app import create_app
from triage_service.worker import Worker

PASSWORD = "demo-pass-1234"


def web(store, login=None):
    app = create_app(store, Worker(store, client_factory=FakeClient), fake(store), start_worker=False,
                     allowed_hosts=("testserver",), sources=[], login=login, lan_url="http://192.168.0.23:8000" if login else "")
    return TestClient(app)


# ── auth.Login ───────────────────────────────────────────

def test_check_and_lock_after_too_many_fails():
    login = auth.Login(PASSWORD)
    assert login.check(PASSWORD, "a", now=0) is True
    assert [login.check("틀림", "a", now=t) for t in range(5)] == [False] * 5
    assert login.check(PASSWORD, "a", now=10) is None          # 1분 안에 5번 틀려 잠시 막힘 — 맞는 비밀번호도 받지 않는다
    assert login.check(PASSWORD, "b", now=10) is True          # 다른 기기는 영향 없음
    assert login.check(PASSWORD, "a", now=70) is True          # 1분이 지나면 다시 받는다


def test_token_signed_and_expires():
    login = auth.Login(PASSWORD)
    token = login.token(now=1_000)
    assert login.valid(token, now=1_000 + 60)
    assert not login.valid(token, now=1_000 + config.LOGIN_HOURS * 3600)              # 만료
    issued, nonce, sig = token.split(".")
    assert not login.valid(f"{int(issued) + 99999}.{nonce}.{sig}", now=1_000)         # 시각을 고치면 서명이 맞지 않는다
    assert not login.valid(token[:-1] + ("0" if token[-1] != "0" else "1"), now=1_000)
    assert not login.valid(auth.Login(PASSWORD).token(now=1_000), now=1_000)          # 서버를 다시 켜면(새 키) 옛 쿠키는 무효
    assert not any(login.valid(t, now=1_000) for t in (None, "", "abc", "x.y.z", "..", "1.x.é", "1.é.ab"))   # 이상한 문자도 오류 없이 거절


# ── 화면 ─────────────────────────────────────────────────

def test_without_lan_nothing_changes(store):
    client = web(store)
    assert client.get("/").status_code == 200 and "로그아웃" not in client.get("/").text
    assert client.get("/login").status_code == 404 and client.get("/connect").status_code == 404


def test_lan_requires_login_then_works(store):
    client = web(store, auth.Login(PASSWORD))
    for path in ("/", "/qa", "/mine", "/dashboard", "/triage", "/reports", "/new", "/connect"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert client.post("/fetch", data={"url": "x"}, follow_redirects=False).headers["location"] == "/login"   # 쓰기 요청도
    login_page = client.get("/login").text
    assert "/reports" not in login_page and "class=\"bts" not in login_page   # 로그인 전에는 메뉴·BTS 연결 상태를 보이지 않는다
    r = client.post("/login", data={"password": "틀림"})
    assert r.status_code == 401 and "비밀번호가 맞지 않습니다" in r.text and "class=\"bts" not in r.text
    r = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    page = client.get("/").text
    assert "로그아웃" in page and "휴대폰 접속" in page
    connect = client.get("/connect").text
    assert "<svg" in connect and "http://192.168.0.23:8000" in connect
    client.post("/logout")
    assert client.get("/", follow_redirects=False).status_code == 303


def test_lan_login_lock(store):
    client = web(store, auth.Login(PASSWORD))
    for _ in range(config.LOGIN_MAX_FAILS):
        client.post("/login", data={"password": "틀림"})
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 429 and "1분 뒤" in r.text


def test_forged_cookie_and_foreign_host_rejected(store):
    client = web(store, auth.Login(PASSWORD))
    client.cookies.set(auth.COOKIE, auth.Login(PASSWORD).token())   # 다른 키로 만든 쿠키
    assert client.get("/", follow_redirects=False).status_code == 303
    assert client.get("/login", headers={"host": "evil.example"}).status_code == 400   # 받는 주소 밖은 그대로 거절


# ── 실행 명령 ────────────────────────────────────────────

@pytest.mark.parametrize("password", [None, "short"])
def test_cli_lan_refuses_without_strong_password(password, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(config.MAPPING_PATH))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "service.db")
    monkeypatch.setattr(config, "load_env", lambda: None)   # 이 PC 의 .env 에 시연 비밀번호가 있어도 시험이 흔들리지 않게
    if password is None:
        monkeypatch.delenv("TRIAGE_DEMO_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("TRIAGE_DEMO_PASSWORD", password)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: pytest.fail("서버를 띄우면 안 된다"))
    assert cli.main(["--lan"]) == 2
    assert "TRIAGE_DEMO_PASSWORD" in capsys.readouterr().out


def test_cli_lan_starts_on_all_interfaces_with_lan_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(config.MAPPING_PATH))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "service.db")
    monkeypatch.setenv("TRIAGE_DEMO_PASSWORD", PASSWORD)
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(cli, "lan_addresses", lambda: ["192.168.0.23"])
    seen = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, host, port, proxy_headers: seen.update(app=app, host=host, proxy=proxy_headers))
    assert cli.main(["--lan"]) == 0
    out = capsys.readouterr().out
    assert seen["host"] == config.LAN_HOST and "http://192.168.0.23:8000" in out
    assert seen["proxy"] is False   # X-Forwarded-For 로 이 PC(127.0.0.1)인 척해 로그인을 건너뛰지 못하게
    client = TestClient(seen["app"], base_url="http://192.168.0.23:8000")
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"   # 와이파이 주소로 온 요청을 받는다(로그인 화면으로)


def test_lan_addresses_skip_loopback(monkeypatch):
    monkeypatch.setattr(cli.socket, "gethostbyname_ex", lambda name: (name, [], ["127.0.0.1", "169.254.3.4", "192.168.0.23"]))
    assert "192.168.0.23" in cli.lan_addresses() and "127.0.0.1" not in cli.lan_addresses()
