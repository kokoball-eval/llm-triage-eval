"""사람별 로그인 시험 (역할 2단계의 1단계) — 사용자 파일·비밀번호 해시(auth), 로그인·"나"·누른 사람 기록(web·qa·review), 실행 명령.

실제 BTS 는 부르지 않는다(가짜 BTS: 구성원은 "<팀>팀장"·"<팀>팀원", 계정 ID 도 같은 글자).
"""

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_milestone import RULES, fake, register, store   # noqa: F401 — store 는 픽스처
from triage_service import auth, cli, config, qa, review
from triage_service.web.app import create_app
from triage_service.worker import Worker

PW = {"lead": "lead-pass-123", "mem": "member-pass-1"}


def users_file(tmp_path, rows=None):
    rows = rows if rows is not None else [
        ("lead", "경민", "leader", "QA팀장", auth.hash_password(PW["lead"])),
        ("mem", "박대무", "member", "QA팀원", auth.hash_password(PW["mem"]))]
    text = "".join(f'[[user]]\nid = "{i}"\nname = "{n}"\nrole = "{r}"\nbts = "{b}"\npassword = "{p}"\n\n' for i, n, r, b, p in rows)
    path = tmp_path / "사용자.local.toml"
    path.write_text(text, encoding="utf-8")
    return path


def app_with_users(store, tmp_path, tracker=None):
    tracker = tracker or fake(store)
    users, hashes = auth.load_users(users_file(tmp_path))
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False, allowed_hosts=("testserver",),
                     sources=[], assign_rules=RULES, login=auth.Login(users=users, hashes=hashes))
    return app, tracker


def login(client, who):
    return client.post("/login", data={"id": who, "password": PW[who]}, follow_redirects=False)


# ── 사용자 파일·해시 ─────────────────────────────────────

def test_hash_and_verify():
    h = auth.hash_password("abc-12345")
    assert h.startswith("scrypt$") and auth.verify("abc-12345", h) and not auth.verify("abc-12346", h)
    assert h != auth.hash_password("abc-12345")                       # 소금이 달라 같은 비밀번호도 해시가 다르다
    assert not auth.verify("x", "평문") and not auth.verify("x", "scrypt$zz$zz")


def test_load_users_ok(tmp_path):
    users, hashes = auth.load_users(users_file(tmp_path))
    assert users["lead"] == auth.User("lead", "경민", "leader", "QA팀장") and set(hashes) == {"lead", "mem"}


@pytest.mark.parametrize("rows, said", [
    ([("a", "가", "leader", "x", "plain-text")], "해시"),
    ([("a", "가", "boss", "x", auth.hash_password("abcdefgh"))], "role"),
    ([("a", "가", "member", "x", auth.hash_password("abcdefgh"))], "리더"),
    ([("ab", "가", "leader", "x", "scrypt$1$2"), ("ab", "나", "member", "y", "scrypt$1$2")], "겹칩니다"),
    ([("ab", "가", "leader", "x", "scrypt$여기에-triage-user-hash-결과$붙여넣기")], "해시"),   # 예시 파일의 자리 표시자는 통과하지 않는다
    ([("ab", "가\\n줄바꿈", "leader", "x", auth.hash_password("abcdefgh"))], "제어 문자"),
    ([("Bad Name", "가", "leader", "x", "scrypt$1$2")], "로그인 이름"),
    ([("a", "가", "leader", "", "scrypt$1$2")], "bts"),
])
def test_load_users_rejects(tmp_path, rows, said):
    with pytest.raises(auth.UsersError, match=said):
        auth.load_users(users_file(tmp_path, rows))


def test_login_per_user_token_and_removed_user(tmp_path):
    users, hashes = auth.load_users(users_file(tmp_path))
    login_ = auth.Login(users=users, hashes=hashes)
    assert login_.check(PW["mem"], "dev1", "mem") == users["mem"]
    assert login_.check(PW["mem"], "dev1", "lead") is False and login_.check(PW["mem"], "dev1", "없는사람") is False
    token = login_.token(users["mem"])
    assert login_.valid(token) == users["mem"] and token.startswith("mem.")
    del login_.users["mem"]                                            # 사용자 파일에서 지운 사람의 쿠키는 받지 않는다
    assert login_.valid(token) is None


# ── 화면 ─────────────────────────────────────────────────

def test_this_pc_also_needs_login_with_users(store, tmp_path):
    app, _ = app_with_users(store, tmp_path)
    here = TestClient(app, client=("127.0.0.1", 50000))
    assert here.get("/", follow_redirects=False).headers["location"] == "/login"   # 사람별 로그인이면 이 PC 도 로그인
    page = here.get("/login").text
    assert 'name="id"' in page and "로그인 이름" in page
    assert login(here, "lead").status_code == 303
    page = here.get("/").text
    assert "경민 · 리더" in page and "로그아웃" in page


def test_wrong_name_or_password(store, tmp_path):
    app, _ = app_with_users(store, tmp_path)
    client = TestClient(app)
    r = client.post("/login", data={"id": "mem", "password": PW["lead"]})
    assert r.status_code == 401 and "이름 또는 비밀번호가 맞지 않습니다" in r.text and 'value="mem"' in r.text


def test_mine_is_the_logged_in_person(store, tmp_path):
    app, tracker = app_with_users(store, tmp_path)
    key = register(store, tracker)["ticket_key"]                     # 재현 대기 → QA팀 첫 구성원(QA팀장)
    lead, mem = TestClient(app), TestClient(app)
    login(lead, "lead")
    login(mem, "mem")
    lead.post("/qa/sync")
    assert key in lead.get("/mine").text and "재현 확인하기" in lead.get("/mine").text
    page = mem.get("/mine").text
    assert key not in page and "지금 처리할 일이 없습니다" in page    # 실무자에게는 남의 이슈가 보이지 않는다
    assert "박대무 · 실무자" in page and "QA팀원 님" in page          # 머리는 로그인한 사람, 제목은 BTS 의 이름


def test_actions_record_who_pressed(store, tmp_path):
    app, tracker = app_with_users(store, tmp_path)
    rec = register(store, tracker)
    client = TestClient(app)
    login(client, "mem")
    client.post("/qa/sync")
    r = client.post(f"/reports/{rec['id']}/qa", data={"action": "comment", "comment": "확인 중"}, follow_redirects=False)
    assert r.status_code == 303
    log = store.qa_log()[rec["id"]]
    assert log[-1]["actor"] == "박대무" and tracker.get(rec["ticket_key"])["comments"][-1].endswith("(실행: 박대무)")
    assert "박대무" in client.get(f"/reports/{rec['id']}").text      # QA 기록 표의 실행 칸


def test_sign_checks_the_person_not_the_token(store):
    tracker = fake(store)
    rec = register(store, tracker, 처리="긴급 사인 요청")
    from triage_service import assign
    with pytest.raises(assign.BadChoice, match="팀장만 사인할 수 있습니다'") as e:
        qa.act(store, tracker, rec["id"], "sign", rules=RULES, actor_bts="QA팀원", actor_name="박대무")   # 토큰 주인이 팀장이어도
    assert "토큰 주인" not in str(e.value)
    out = qa.act(store, tracker, rec["id"], "sign", rules=RULES, actor_bts="QA팀장", actor_name="경민")
    assert out["ok"] and store.qa_log()[rec["id"]][-1]["actor"] == "경민"


def test_register_records_who(store):
    tracker = fake(store)
    from test_service import DEV_ITEMS, triaged
    rid = triaged(store, DEV_ITEMS[1])
    rec = review.register(store, tracker, rid, {**store.get(rid)["model_fields"], "처리": "개발 배정"}, RULES, milestone="0.0.1", actor="경민")
    assert rec["assignment"]["by"] == "경민"


def test_old_db_gets_actor_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE qa_actions (report_id INTEGER NOT NULL, at TEXT NOT NULL, action TEXT NOT NULL, before TEXT, after TEXT, "
                  "team TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '', error TEXT)")
    from triage_service.store import Store
    Store(path).save_qa(1, "코멘트", None, None, actor="경민")
    assert Store(path).qa_log()[1][0]["actor"] == "경민"


# ── 실행 명령 ────────────────────────────────────────────

def cli_env(tmp_path, monkeypatch, users_path):
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(config.MAPPING_PATH))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "service.db")
    monkeypatch.setattr(config, "load_env", lambda: None)
    monkeypatch.setattr(config, "USERS_PATH", users_path)


def test_cli_check_users_against_bts_groups(tmp_path, monkeypatch, capsys):
    cli_env(tmp_path, monkeypatch, users_file(tmp_path))
    assert cli.main(["--check"]) == 0 and "사용자: 2명(리더 1명)" in capsys.readouterr().out
    cli_env(tmp_path, monkeypatch, users_file(tmp_path, [("ab", "가", "leader", "없는계정", auth.hash_password("abcdefgh"))]))
    assert cli.main(["--check"]) == 1 and "없는계정" in capsys.readouterr().out


def test_cli_refuses_bad_users_file_and_lan_needs_no_demo_password(tmp_path, monkeypatch, capsys):
    cli_env(tmp_path, monkeypatch, users_file(tmp_path, [("a", "가", "leader", "x", "plain")]))
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: pytest.fail("서버를 띄우면 안 된다"))
    assert cli.main([]) == 2 and "해시" in capsys.readouterr().out
    cli_env(tmp_path, monkeypatch, users_file(tmp_path))
    monkeypatch.delenv("TRIAGE_DEMO_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "lan_addresses", lambda: [("192.168.0.23", "phone")])
    seen = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, host, port, proxy_headers: seen.update(app=app, proxy=proxy_headers))
    assert cli.main(["--lan"]) == 0 and "사람별 계정으로 로그인" in capsys.readouterr().out   # 공용 비밀번호 없이도
    assert seen["proxy"] is False
