"""BTS 상태 동기화와 QA 보드 시험 (S5-2) — 상태 대응 설정, 어댑터가 읽는 상태, 동기화(묶음·실패·상태 변화 기록), 보드, 화면.

실제 BTS 는 부르지 않는다. Jira 검색 응답은 실제 KAN 공간의 응답 모양, Redmine 은 녹화한 단건 응답으로 만든 목록이다
(tests/bts_responses/*.json 의 snapshot_source).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_bts import MAPPING, SECRETS, adapter, write_mapping
from test_service import DEV_ITEMS, triaged
from triage_service import cli, config, review, sync
from triage_service.bts import settings
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import BtsState, TrackerError
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker

RULES = MAPPING["assign"]
STATUS = settings.FAKE_STATUS
VERIFY, DONE = STATUS["verify"][0], STATUS["done"][0]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "service.db")


def register(store, tracker, i: int = 0, 처리: str = "개발 배정", 모듈: str = "결제·재화") -> dict:
    rid = triaged(store, DEV_ITEMS[i])
    values = {**store.get(rid)["model_fields"], "처리": 처리, "모듈": 모듈}
    return review.register(store, tracker, rid, values, RULES)


# ── 상태 대응 설정 ─────────────────────────────────────

def test_classify_uses_only_listed_names():
    assert sync.classify(STATUS, VERIFY) == sync.VERIFY
    assert sync.classify(STATUS, DONE) == sync.DONE
    assert sync.classify(STATUS, "진행 중") == sync.OPEN and sync.classify(STATUS, "엉뚱한 이름") == sync.OPEN


def test_committed_mapping_has_status_for_both_btss():
    for name in ("jira", "redmine"):
        assert set(MAPPING[name]["status"]) == {"verify", "done"}
    assert settings.load(config.MAPPING_PATH, env={})["status"] == STATUS   # 가짜 BTS 는 정해 둔 이름


def test_selected_tracker_status_comes_from_its_section(tmp_path):
    text = config.MAPPING_PATH.read_text(encoding="utf-8").replace('use = "fake"', 'use = "jira"')
    loaded = settings.load(write_mapping(tmp_path, text), env=SECRETS["jira"])
    assert loaded["status"] == MAPPING["jira"]["status"] and loaded["settings"]["status"] == loaded["status"]


def test_mapping_without_status_still_loads(tmp_path):
    text = config.MAPPING_PATH.read_text(encoding="utf-8").replace('use = "fake"', 'use = "jira"')
    text = text[:text.index("[jira.status]")] + text[text.index("# ── Redmine"):]
    assert settings.load(write_mapping(tmp_path, text), env=SECRETS["jira"])["status"] is None


@pytest.mark.parametrize("old, new, message", [
    ('verify = ["검토 중", "In Review"]', 'verify = []', "verify 는 상태 이름 목록"),
    ('verify = ["검토 중", "In Review"]', 'verify = "검토 중"', "verify 는 상태 이름 목록"),
    ('done = ["완료", "Done"]', 'done = ["완료", " "]', "done 는 상태 이름 목록"),
    ('done = ["완료", "Done"]', 'done = ["검토 중"]', "같은 상태가 있습니다"),
    ('done = ["완료", "Done"]', 'done = ["완료"]\nstatus = 1', "모르는 키"),
    ('[jira.status]', '[jira.stauts]', "모르는 키"),
])
def test_bad_status_is_refused(tmp_path, old, new, message):
    text = config.MAPPING_PATH.read_text(encoding="utf-8")
    assert old in text
    with pytest.raises(settings.SettingsError, match=message):
        settings.load(write_mapping(tmp_path, text.replace(old, new, 1)), env={})


# ── 어댑터가 읽는 상태 ──────────────────────────────────

def test_jira_snapshot_reads_status_assignee_updated(store):
    tracker, rec = adapter("jira", store)
    key = rec.data["ticket_key"]
    found = tracker.snapshot([key, "KAN-999"])   # 없는 키는 빠진다
    assert found == {key: BtsState("검토 중", "QA 담당", "2026-11-23T10:06:47.530+0900")}
    params = rec.requests[-1].url.params
    assert params["fields"] == "status,assignee,updated" and f'key in ("{key}", "KAN-999")' in params["jql"]
    assert rec.requests[-1].url.path == "/rest/api/3/search/jql"


def test_redmine_snapshot_reads_every_status(store):
    tracker, rec = adapter("redmine", store)
    found = tracker.snapshot(["1", "2"])
    assert found["1"] == BtsState("Resolved", "QA 담당", rec.data["snapshot"]["body"]["issues"][0]["updated_on"])
    params = rec.requests[-1].url.params
    assert params["issue_id"] == "1,2" and params["status_id"] == "*"   # 닫힌 티켓도 읽는다


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_snapshot_failure_is_a_tracker_error(name, store):
    tracker, _ = adapter(name, store, fail={"snapshot": "timeout"})
    with pytest.raises(TrackerError):
        tracker.snapshot(["1"])


def test_fake_snapshot_and_set_status(store, monkeypatch):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker)
    key = rec["ticket_key"]
    first = tracker.snapshot([key, "FAKE-99"])
    assert list(first) == [key] and first[key].status == "신규" and first[key].assignee == rec["assignment"]["team"]
    monkeypatch.setattr("triage_service.bts.fake.now", lambda: "2099-01-01T00:00:00")   # 초 단위 시계라 같은 초면 구분되지 않는다
    tracker.set_status(key, VERIFY)
    after = tracker.snapshot([key])[key]
    assert after.status == VERIFY and after.updated == "2099-01-01T00:00:00"   # 마지막 변경 시각이 새로 찍힌다
    assert tracker.get(key)["fields"]["요약"]   # 등록 내용은 그대로


# ── 동기화 ─────────────────────────────────────────────

def test_sync_saves_state_and_records_changes_once(store):
    tracker = FakeTracker(store.path)
    key = register(store, tracker)["ticket_key"]
    assert sync.run(store, tracker, STATUS) == {"synced": 1, "missing": [], "errors": []}
    rid = store.list()[0]["id"]
    assert store.get(rid)["bts"]["status"] == "신규"
    sync.run(store, tracker, STATUS)   # 그대로면 변화 기록을 더하지 않는다
    assert [(src, dst) for _, src, dst in store.bts_history()[rid]] == [(None, "신규")]
    tracker.set_status(key, VERIFY)
    sync.run(store, tracker, STATUS)
    assert [(src, dst) for _, src, dst in store.bts_history()[rid]] == [(None, "신규"), ("신규", VERIFY)]
    assert store.get(rid)["state"] == "등록 완료"   # 리포트 흐름은 BTS 상태와 따로다


def test_sync_ignores_unregistered_reports(store):
    tracker = FakeTracker(store.path)
    triaged(store)   # 검토 대기 — 티켓 없음
    assert sync.run(store, tracker, STATUS)["synced"] == 0 and sync.targets(store) == []


def test_sync_reports_missing_tickets_and_keeps_old_value(store):
    tracker = FakeTracker(store.path)
    key = register(store, tracker)["ticket_key"]
    sync.run(store, tracker, STATUS)
    with sqlite3.connect(store.path) as c:   # BTS 에서 지워진 티켓
        c.execute("DELETE FROM fake_tickets")
    out = sync.run(store, tracker, STATUS)
    assert out == {"synced": 0, "missing": [key], "errors": []}
    assert store.list()[0]["bts"]["status"] == "신규"


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
    tracker = ChunkTracker(store.path, fail_chunks=[1])
    for i in range(5):
        register(store, tracker, i % len(DEV_ITEMS))
    out = sync.run(store, tracker, STATUS)
    assert [len(c) for c in tracker.calls] == [2, 2, 1]
    assert out["synced"] == 3 and len(out["errors"]) == 1 and "응답하지 않습니다" in out["errors"][0]
    assert sum(1 for r in store.list() if r["bts"]) == 3   # 실패한 묶음만 비어 있다


def test_sync_only_reads_the_latest_limit(store, monkeypatch):
    monkeypatch.setattr(config, "SYNC_LIMIT", 2)
    tracker = FakeTracker(store.path)
    for i in range(3):
        register(store, tracker, i % len(DEV_ITEMS))
    assert sync.run(store, tracker, STATUS)["synced"] == 2
    assert [r["bts"] is not None for r in store.list()] == [True, True, False]   # 번호 큰 순
    assert sync.board(STATUS, RULES, sync.targets(store))["unsynced"] == 0   # 대상 밖은 "동기화 전"으로 세지 않는다


def test_old_database_gets_bts_column_and_table(tmp_path):
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
    assert store.get(1)["bts"] is None and store.bts_history() == {}


# ── 보드 ───────────────────────────────────────────────

def synced_store(store):
    """검증 대기 2건(오래된 쪽이 먼저)·재현 대기 1건·개발팀 진행 1건·완료 1건·동기화 전 1건."""
    tracker = FakeTracker(store.path)
    recs = [register(store, tracker, i % len(DEV_ITEMS), 처리, 모듈) for i, (처리, 모듈) in enumerate([
        ("개발 배정", "결제·재화"), ("개발 배정", "그래픽·사운드"), ("등록(재현 대기)", "네트워크"),
        ("개발 배정", "UI·텍스트"), ("개발 배정", "네트워크"), ("개발 배정", "콘텐츠 진행")])]
    old, new = "2026-11-23T09:00:00+09:00", "2026-11-23T11:30:00+09:00"
    for rec, status, updated in [(recs[0], VERIFY, old), (recs[1], VERIFY, new), (recs[2], "신규", new),
                                 (recs[3], "신규", new), (recs[4], DONE, new)]:
        store.save_bts(rec["id"], {"status": status, "assignee": "누군가", "updated": updated, "synced_at": "2026-11-23T12:00:00"})
    return recs, datetime(2026, 11, 23, 12, 0, tzinfo=timezone(timedelta(hours=9)))


def test_board_lists(store):
    recs, at = synced_store(store)
    b = sync.board(STATUS, RULES, sync.targets(store), at)
    assert [(r["id"], w) for r, w in b["verify"]] == [(recs[0]["id"], "3시간"), (recs[1]["id"], "30분")]   # 오래된 순
    assert [r["id"] for r in b["repro"]] == [recs[2]["id"]]   # 배정 팀이 QA 이고 검증 대기·완료가 아닌 건
    assert b["teams"] == {"QA": 1, "서버": 1, "아트": 1, "클라": 1}   # 완료(recs[4])는 뺀다, 검증 대기는 센다
    assert b["unsynced"] == 1 and b["synced_at"] == "2026-11-23T12:00:00" and b["known"] == sorted({VERIFY, DONE, "신규"})


def test_board_without_assign_rules_has_no_repro(store):
    recs, at = synced_store(store)
    b = sync.board(STATUS, None, sync.targets(store), at)
    assert b["repro"] == [] and b["teams"] == {"QA": 1, "서버": 1, "아트": 1, "클라": 1}


def test_board_survives_unreadable_updated(store):
    recs, at = synced_store(store)
    store.save_bts(recs[0]["id"], {"status": VERIFY, "assignee": "", "updated": "엉뚱한 값", "synced_at": "2026-11-23T12:00:00"})
    store.save_bts(recs[1]["id"], {"status": VERIFY, "assignee": "", "updated": "2026-11-23T13:00:00+09:00", "synced_at": "2026-11-23T12:00:00"})   # 앞선 시각
    waits = {r["id"]: w for r, w in sync.board(STATUS, RULES, sync.targets(store), at)["verify"]}
    assert waits[recs[0]["id"]] == "0분" and waits[recs[1]["id"]] == "0분"   # 화면이 죽지 않고 음수도 없다


def test_ago():
    assert (sync.ago(5), sync.ago(90), sync.ago(3000)) == ("5분", "2시간", "2일")


# ── 화면 ───────────────────────────────────────────────

def web_client(store, tracker=None, status_rules=STATUS):
    tracker = tracker or FakeTracker(store.path)
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=RULES, status_rules=status_rules)
    return TestClient(app), tracker


def test_web_qa_without_status_mapping_explains(store):
    client, _ = web_client(store, status_rules=None)
    page = client.get("/qa").text
    assert "상태 대응이 없어" in page and "지금 동기화" not in page
    assert client.post("/qa/sync").status_code == 200   # 눌러도 BTS 를 부르지 않는다


def test_web_qa_syncs_only_on_button(store):
    client, tracker = web_client(store)
    key = register(store, tracker)["ticket_key"]
    tracker.set_status(key, VERIFY)
    page = client.get("/qa").text
    assert "마지막 동기화: 아직 없음" in page and "검증 대기인 티켓이 없습니다" in page   # 열기만 해서는 읽지 않는다
    page = client.post("/qa/sync").text
    assert "1건을 읽었습니다" in page and key in page and "검증 대기 <span" in page
    assert "마지막 동기화: 아직 없음" not in client.get("/qa").text
    assert VERIFY in client.get(f"/reports/{store.list()[0]['id']}").text   # 상세 화면에도 읽어 둔 상태


def test_web_qa_shows_sync_failure_and_keeps_old_values(store):
    tracker = ChunkTracker(store.path)
    client, _ = web_client(store, tracker)
    register(store, tracker)
    client.post("/qa/sync")
    tracker.fail_chunks = {1}   # 두 번째 호출(다음 동기화)이 실패
    page = client.post("/qa/sync").text
    assert "읽지 못했습니다" in page and "응답하지 않습니다" in page and "이전에 읽은 값은 그대로" in page
    assert store.list()[0]["bts"]["status"] == "신규"


def test_web_header_links_qa(store):
    client, _ = web_client(store)
    assert 'href="/qa"' in client.get("/").text and client.get("/qa").status_code == 200


def test_check_reports_status_mapping(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(config.MAPPING_PATH))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "service.db")
    assert cli.main(["--check"]) == 0
    assert "BTS 상태 동기화: 상태 대응 있음" in capsys.readouterr().out
