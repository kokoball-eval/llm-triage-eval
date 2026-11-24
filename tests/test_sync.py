"""BTS 상태 동기화와 QA 보드 시험 (S5-2, S5-3 에서 다섯 상태로 바꿈) — 어댑터가 읽는 상태, 동기화(묶음·실패·상태 변화 기록),
보드, 화면.

실제 BTS 는 부르지 않는다. Jira·Redmine 응답은 실제 서버에서 녹화한 것이다(tests/bts_responses/*.json 의 snapshot_source).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_bts import MAPPING, adapter, write_mapping
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
    assert list(first) == [key] and first[key].status == QA_REVIEW and first[key].assignee == rec["assignment"]["team"]
    monkeypatch.setattr("triage_service.bts.fake.now", lambda: "2099-01-01T00:00:00")   # 초 단위 시계라 같은 초면 구분되지 않는다
    tracker.set_status(key, RESOLVE)
    after = tracker.snapshot([key])[key]
    assert after.status == RESOLVE and after.updated == "2099-01-01T00:00:00"   # 마지막 변경 시각이 새로 찍힌다
    assert tracker.get(key)["fields"]["요약"]   # 등록 내용은 그대로


# ── 동기화 ─────────────────────────────────────────────

def test_sync_saves_state_and_records_changes_once(store):
    tracker = FakeTracker(store.path)
    key = register(store, tracker, 처리="등록(재현 대기)")["ticket_key"]
    assert sync.run(store, tracker) == {"synced": 1, "missing": [], "errors": []}
    rid = store.list()[0]["id"]
    assert store.get(rid)["bts"]["status"] == QA_REVIEW
    sync.run(store, tracker)   # 그대로면 변화 기록을 더하지 않는다
    assert [(src, dst) for _, src, dst in store.bts_history()[rid]] == [(None, QA_REVIEW)]
    tracker.set_status(key, IN_DEV)
    sync.run(store, tracker)
    assert [(src, dst) for _, src, dst in store.bts_history()[rid]] == [(None, QA_REVIEW), (QA_REVIEW, IN_DEV)]
    assert store.get(rid)["state"] == "등록 완료"   # 리포트 흐름은 BTS 상태와 따로다


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
    assert out == {"synced": 0, "missing": [key], "errors": []}
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
    assert sync.board(sync.targets(store), {})["unsynced"] == 0   # 대상 밖은 "동기화 전"으로 세지 않는다


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
    assert store.get(1)["bts"] is None and store.bts_history() == {} and store.qa_log() == {}


# ── 보드 ───────────────────────────────────────────────

def synced_store(store):
    """재현 대기 1건·회귀 테스트 대기 2건(오래된 쪽이 먼저)·개발 중 1건·보류 1건·종료 1건·동기화 전 1건.
    등록 때 첫 상태를 바꾼 건은 바꾼 뒤 읽은 상태가 저장된다(qa.write) — 아래에서 시험할 상태로 덮어쓴다."""
    tracker = FakeTracker(store.path)
    recs = [register(store, tracker, i % len(DEV_ITEMS), 처리, 모듈) for i, (처리, 모듈) in enumerate([
        ("등록(재현 대기)", "네트워크"), ("개발 배정", "결제·재화"), ("개발 배정", "그래픽·사운드"),
        ("개발 배정", "UI·텍스트"), ("개발 배정", "콘텐츠 진행"), ("개발 배정", "네트워크"), ("등록(재현 대기)", "UI·텍스트")])]   # 마지막 건은 첫 상태를 쓰지 않아 동기화 전
    old, new = "2026-11-23T09:00:00+09:00", "2026-11-23T11:30:00+09:00"
    for rec, status, updated in [(recs[0], QA_REVIEW, new), (recs[1], RESOLVE, old), (recs[2], RESOLVE, new),
                                 (recs[3], IN_DEV, new), (recs[4], CHECK, new), (recs[5], CLOSED, new)]:
        store.save_bts(rec["id"], {"status": status, "assignee": "누군가", "updated": updated, "synced_at": "2026-11-23T12:00:00"})
    return recs, datetime(2026, 11, 23, 12, 0, tzinfo=timezone(timedelta(hours=9)))


def test_board_lists(store):
    recs, at = synced_store(store)
    b = sync.board(sync.targets(store), store.qa_log(), at)
    assert [r["id"] for r in b["repro"]] == [recs[0]["id"]]
    assert [(r["id"], w) for r, w in b["verify"]] == [(recs[1]["id"], "3시간"), (recs[2]["id"], "30분")]   # 오래된 순
    assert [r["id"] for r in b["hold"]] == [recs[4]["id"]]
    assert b["teams"] == {"QA": 1, "기획": 1, "서버": 1, "아트": 1, "클라": 1}   # Closed(recs[5])는 뺀다
    assert b["unsynced"] == 1 and b["synced_at"] == "2026-11-23T12:00:00" and b["unknown"] == []


def test_board_counts_reassigned_team(store):
    """재현 확인으로 넘긴 팀이 있으면 그 팀으로 센다(sync 설계 의도 7). 쓰기 요청이 실패한 기록은 팀이 비어 세지 않고,
    요청은 받았지만 상태 확인에서 실패한 기록의 팀은 센다."""
    recs, at = synced_store(store)
    store.save_qa(recs[0]["id"], "재현 확인", QA_REVIEW, IN_DEV, team="서버")
    store.save_qa(recs[3]["id"], "재현 확인", QA_REVIEW, QA_REVIEW, team="", error="BTS 쓰기 실패")
    store.save_qa(recs[4]["id"], "재현 확인", QA_REVIEW, QA_REVIEW, team="아트", error="BTS 가 바꾸지 않았습니다")
    b = sync.board(sync.targets(store), store.qa_log(), at)
    assert b["teams"] == {"서버": 2, "아트": 2, "클라": 1}
    assert next(r for r in b["repro"] if r["id"] == recs[0]["id"])["team_now"] == "서버"


def test_board_shows_unknown_status_names(store):
    recs, at = synced_store(store)
    store.save_bts(recs[3]["id"], {"status": "해야 할 일", "assignee": "", "updated": "", "synced_at": "2026-11-23T12:00:00"})
    b = sync.board(sync.targets(store), {}, at)
    assert b["unknown"] == ["해야 할 일"]   # 다섯 상태 밖 — 워크플로가 바뀐 것을 알린다


def test_board_survives_unreadable_updated(store):
    recs, at = synced_store(store)
    store.save_bts(recs[1]["id"], {"status": RESOLVE, "assignee": "", "updated": "엉뚱한 값", "synced_at": "2026-11-23T12:00:00"})
    store.save_bts(recs[2]["id"], {"status": RESOLVE, "assignee": "", "updated": "2026-11-23T13:00:00+09:00", "synced_at": "2026-11-23T12:00:00"})   # 앞선 시각
    waits = {r["id"]: w for r, w in sync.board(sync.targets(store), {}, at)["verify"]}
    assert waits[recs[1]["id"]] == "0분" and waits[recs[2]["id"]] == "0분"   # 화면이 죽지 않고 음수도 없다


def test_ago():
    assert (sync.ago(5), sync.ago(90), sync.ago(3000)) == ("5분", "2시간", "2일")


# ── 화면 ───────────────────────────────────────────────

def web_client(store, tracker=None):
    tracker = tracker or FakeTracker(store.path)
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=RULES)
    return TestClient(app), tracker


def test_web_qa_syncs_only_on_button(store):
    client, tracker = web_client(store)
    key = register(store, tracker, 처리="등록(재현 대기)")["ticket_key"]   # 첫 상태를 바꾸지 않는 건이라 아직 읽은 값이 없다
    tracker.set_status(key, RESOLVE)
    page = client.get("/qa").text
    assert "마지막 동기화: 아직 없음" in page and "회귀 테스트를 기다리는 티켓이 없습니다" in page   # 열기만 해서는 읽지 않는다
    page = client.post("/qa/sync").text
    assert "1건을 읽었습니다" in page and key in page and "회귀 테스트를 기다리는 티켓이 없습니다" not in page
    assert "마지막 동기화: 아직 없음" not in client.get("/qa").text
    assert RESOLVE in client.get(f"/reports/{store.list()[0]['id']}").text   # 상세 화면에도 읽어 둔 상태


def test_web_qa_shows_sync_failure_and_keeps_old_values(store):
    tracker = ChunkTracker(store.path)
    client, _ = web_client(store, tracker)
    register(store, tracker)
    client.post("/qa/sync")
    tracker.calls, tracker.fail_chunks = [], {0}   # 다음 동기화의 첫 호출이 실패
    page = client.post("/qa/sync").text
    assert "읽지 못했습니다" in page and "응답하지 않습니다" in page and "이전에 읽은 값은 그대로" in page
    assert store.list()[0]["bts"]["status"] == IN_DEV


def test_web_qa_warns_unknown_status(store):
    client, tracker = web_client(store)
    key = register(store, tracker)["ticket_key"]
    tracker.set_status(key, "해야 할 일")
    assert "다섯 상태" in client.post("/qa/sync").text


def test_web_header_links_qa(store):
    client, _ = web_client(store)
    assert 'href="/qa"' in client.get("/").text and client.get("/qa").status_code == 200
