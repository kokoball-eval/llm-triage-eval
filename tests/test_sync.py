"""BTS 상태 동기화 시험 (S5-2, S5-3 에서 다섯 상태로 바꿈, QA 보드는 2026-11-26 홈·내 업무로 옮김) — 어댑터가 읽는 상태, 동기화(묶음·실패·상태 변화 기록),
보드, 화면.

실제 BTS 는 부르지 않는다. Jira·Redmine 응답은 실제 서버에서 녹화한 것이다(tests/bts_responses/*.json 의 snapshot_source).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_bts import MAPPING, adapter, an_issue, write_mapping
from test_service import DEV_ITEMS, triaged
from triage_service import config, review, sync
from triage_service.bts import settings
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import CHECK, CLOSED, IN_DEV, QA_REVIEW, RESOLVE, BtsState, TrackerError
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker

RULES = MAPPING["assign"]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "service.db")


def register(store, tracker, i: int = 0, 처리: str = "개발 배정", 모듈: str = "결제·재화") -> dict:
    rid = triaged(store, DEV_ITEMS[i])
    values = {**store.get(rid)["model_fields"], "처리": 처리, "모듈": 모듈}
    return review.register(store, tracker, rid, values, RULES)


# ── 설정: 상태 대응은 없앴다 ───────────────────────────

@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_old_status_section_is_refused(tmp_path, name):
    """S5-2 의 [<bts>.status] 가 남아 있으면 조용히 무시하지 않고 멈춘다(settings 설계 의도 5)."""
    text = config.MAPPING_PATH.read_text(encoding="utf-8") + f'\n[{name}.status]\nverify = ["Resolve"]\n'
    with pytest.raises(settings.SettingsError, match="모르는 키: status"):
        settings.load(write_mapping(tmp_path, text), env={})


def test_committed_mapping_has_no_status_section():
    assert all("status" not in MAPPING[n] for n in ("jira", "redmine"))
    assert "status" not in settings.load(config.MAPPING_PATH, env={})


# ── 어댑터가 읽는 상태 ──────────────────────────────────

def test_jira_search_reconciles_known_ids(store):
    """Jira 검색은 색인이라 늦을 수 있다 — 아는 이슈 숫자 ID 를 reconcileIssues 로 함께 보낸다(jira 설계 의도 10, S5-4a)."""
    tracker, rec = adapter("jira", store)
    key = rec.data["ticket_key"]
    tracker.snapshot([key])
    assert "reconcileIssues" not in rec.requests[-1].url.params   # 처음에는 모른다
    tracker.snapshot([key])
    assert rec.requests[-1].url.params.get_list("reconcileIssues") == [rec.data["snapshot"]["body"]["issues"][0]["id"]]
    ticket = tracker.create(an_issue(store))   # 등록 응답의 ID 도 기억한다
    tracker.snapshot([key, ticket.key])
    assert rec.requests[-1].url.params.get_list("reconcileIssues") == [
        rec.data["snapshot"]["body"]["issues"][0]["id"], rec.data["create"]["body"]["id"]]


def test_jira_snapshot_reads_status_assignee_updated(store):
    tracker, rec = adapter("jira", store)
    key = rec.data["ticket_key"]
    found = tracker.snapshot([key, "KAN-999"])   # 없는 키는 빠진다
    assert found == {key: BtsState(IN_DEV, "QA 담당", "2026-11-24T16:41:26.613+0900")}
    params = rec.requests[-1].url.params
    assert params["fields"] == "status,assignee,updated" and f'key in ("{key}", "KAN-999")' in params["jql"]
    assert rec.requests[-1].url.path == "/rest/api/3/search/jql"


def test_jira_done_category_reads_as_closed(store):
    """한국어 계정에서 Closed 는 '종료'로 온다 — 완료 범주면 Closed 로 읽는다(jira.py 설계 의도 8)."""
    from triage_service.bts.jira import name
    assert name({"name": "종료", "statusCategory": {"key": "done"}}) == CLOSED
    assert name({"name": "In Dev", "statusCategory": {"key": "indeterminate"}}) == IN_DEV
    assert name({"name": "QA Review"}) == QA_REVIEW


def test_redmine_snapshot_reads_every_status(store):
    tracker, rec = adapter("redmine", store)
    found = tracker.snapshot(["1", "2"])
    assert found["1"] == BtsState(IN_DEV, "Redmine Admin", rec.data["snapshot"]["body"]["issues"][0]["updated_on"])
    params = rec.requests[-1].url.params
    assert params["issue_id"] == "1,2" and params["status_id"] == "*"   # 닫힌 티켓도 읽는다


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_snapshot_failure_is_a_tracker_error(name, store):
    tracker, _ = adapter(name, store, fail={"snapshot": "timeout"})
    with pytest.raises(TrackerError):
        tracker.snapshot(["1"])


def test_fake_snapshot_and_set_status(store, monkeypatch):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker, 처리="등록(재현 대기)")
    key = rec["ticket_key"]
    first = tracker.snapshot([key, "FAKE-99"])
    assert list(first) == [key] and first[key].status == QA_REVIEW and first[key].assignee == rec["assignment"]["member"]["name"]   # 가짜 BTS 도 실제처럼 담당 구성원을 돌려준다
    monkeypatch.setattr("triage_service.bts.fake.now", lambda: "2099-01-01T00:00:00")   # 초 단위 시계라 같은 초면 구분되지 않는다
    tracker.set_status(key, RESOLVE)
    after = tracker.snapshot([key])[key]
    assert after.status == RESOLVE and after.updated == "2099-01-01T00:00:00"   # 마지막 변경 시각이 새로 찍힌다
    assert tracker.get(key)["fields"]["요약"]   # 등록 내용은 그대로


# ── 동기화 ─────────────────────────────────────────────

def test_sync_saves_state(store):
    tracker = FakeTracker(store.path)
    key = register(store, tracker, 처리="등록(재현 대기)")["ticket_key"]
    assert sync.run(store, tracker) == {"synced": 1, "missing": [], "errors": [], "milestones": []}
    rid = store.list()[0]["id"]
    assert store.get(rid)["bts"]["status"] == QA_REVIEW
    tracker.set_status(key, IN_DEV)
    sync.run(store, tracker)
    assert store.get(rid)["bts"]["status"] == IN_DEV
    assert store.get(rid)["state"] == "등록 완료"   # 리포트 흐름은 BTS 상태와 따로다
    with pytest.raises(KeyError):
        store.save_bts(999, {"status": IN_DEV})   # 없는 리포트는 조용히 넘기지 않는다


def test_sync_ignores_unregistered_reports(store):
    tracker = FakeTracker(store.path)
    triaged(store)   # 검토 대기 — 티켓 없음
    assert sync.run(store, tracker)["synced"] == 0 and sync.targets(store) == []


def test_sync_reports_missing_tickets_and_keeps_old_value(store):
    tracker = FakeTracker(store.path)
    key = register(store, tracker)["ticket_key"]
    sync.run(store, tracker)
    with sqlite3.connect(store.path) as c:   # BTS 에서 지워진 티켓
        c.execute("DELETE FROM fake_tickets")
    out = sync.run(store, tracker)
    assert out == {"synced": 0, "missing": [key], "errors": [], "milestones": []}
    assert store.list()[0]["bts"]["status"] == IN_DEV


class ChunkTracker(FakeTracker):
    def __init__(self, path, fail_chunks=()):
        super().__init__(path)
        self.calls, self.fail_chunks = [], set(fail_chunks)

    def snapshot(self, keys):
        self.calls.append(list(keys))
        if len(self.calls) - 1 in self.fail_chunks:
            raise TrackerError("BTS 가 응답하지 않습니다")
        return super().snapshot(keys)


def test_sync_reads_in_chunks_and_survives_a_failed_chunk(store, monkeypatch):
    monkeypatch.setattr(config, "SYNC_CHUNK", 2)
    tracker = ChunkTracker(store.path)
    for i in range(5):
        register(store, tracker, i % len(DEV_ITEMS), 처리="등록(재현 대기)")
    tracker.calls, tracker.fail_chunks = [], {1}
    out = sync.run(store, tracker)
    assert [len(c) for c in tracker.calls] == [2, 2, 1]
    assert out["synced"] == 3 and len(out["errors"]) == 1 and "응답하지 않습니다" in out["errors"][0]
    assert sum(1 for r in store.list() if r["bts"]) == 3   # 실패한 묶음만 비어 있다


def test_sync_only_reads_the_latest_limit(store, monkeypatch):
    monkeypatch.setattr(config, "SYNC_LIMIT", 2)
    tracker = FakeTracker(store.path)
    for i in range(3):
        register(store, tracker, i % len(DEV_ITEMS), 처리="등록(재현 대기)")
    assert sync.run(store, tracker)["synced"] == 2
    assert [r["bts"] is not None for r in store.list()] == [True, True, False]   # 번호 큰 순
    assert len(sync.targets(store)) == 2   # 대상 밖은 읽지도 화면에 세지도 않는다


def test_old_database_gets_new_columns_and_tables(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                  "updated_at TEXT NOT NULL, state TEXT NOT NULL, report TEXT NOT NULL, result TEXT, pipeline TEXT, "
                  "routing TEXT, model_fields TEXT, final_fields TEXT, edits TEXT, ticket_key TEXT, ticket_url TEXT, "
                  "error TEXT, failed_step TEXT, assignment TEXT)")
        c.execute("INSERT INTO reports (created_at, updated_at, state, report) VALUES ('t', 't', '접수', ?)",
                  ('{"track": "A", "title": "옛 리포트", "body": "본문"}',))
    store = Store(path)
    with sqlite3.connect(path) as c:
        assert "bts" in {r[1] for r in c.execute("PRAGMA table_info(reports)")}
    assert store.get(1)["bts"] is None and store.qa_log() == {}


# ── 표시 ───────────────────────────────────────────────

def test_ago():
    assert (sync.ago(5), sync.ago(90), sync.ago(3000)) == ("5분", "2시간", "2일")


# ── 화면 ───────────────────────────────────────────────

def web_client(store, tracker=None):
    tracker = tracker or FakeTracker(store.path)
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=RULES)
    return TestClient(app), tracker


def test_web_sync_button_reads_and_returns_home(store):
    """[지금 동기화]는 홈에 있고, 끝나면 홈으로 돌아가 마지막 결과를 보인다. 예전 QA 보드 주소는 홈으로 넘긴다."""
    client, tracker = web_client(store)
    key = register(store, tracker, 처리="등록(재현 대기)")["ticket_key"]
    tracker.set_status(key, RESOLVE)
    assert client.get("/qa", follow_redirects=False).headers["location"] == "/"
    r = client.post("/qa/sync", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "1건 읽음" in client.get("/").text
    assert RESOLVE in client.get(f"/reports/{store.list()[0]['id']}").text   # 상세 화면에도 읽어 둔 상태


def test_web_home_shows_sync_failure_and_keeps_old_values(store):
    tracker = ChunkTracker(store.path)
    client, _ = web_client(store, tracker)
    register(store, tracker)
    client.post("/qa/sync")
    tracker.calls, tracker.fail_chunks = [], {0}   # 다음 동기화의 첫 호출이 실패
    page = client.post("/qa/sync").text
    assert "읽지 못한 것" in page and "응답하지 않습니다" in page and "이전에 읽은 값은 그대로" in page
    assert store.list()[0]["bts"]["status"] == IN_DEV


def test_web_menu_marks_current_page(store):
    """메뉴는 누를 수 있는 탭이고 지금 있는 메뉴에 표시가 붙는다(문서/화면_설계.md)."""
    client, _ = web_client(store)
    for path, name in (("/", "홈"), ("/mine", "내 업무"), ("/new", "내 업무"), ("/reports", "리포트"), ("/dashboard", "대시보드"), ("/triage", "트리아지 품질")):
        page = client.get(path).text
        assert f'aria-current="page">{name}</a>' in page and page.count('aria-current="page">') == 1, path
    assert 'href="/qa"' not in client.get("/").text and '<nav class="menu"' in client.get("/").text
    bad = client.post("/reports", data={"track": "A", "title": "", "body": ""})   # 입력 오류로 새 리포트를 다시 그려도
    assert bad.status_code == 422 and 'aria-current="page">내 업무</a>' in bad.text   # 메뉴는 내 업무