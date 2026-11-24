"""마일스톤 시험 (S5-4b) — 어댑터(버전 목록·버전 넣어 등록·버전의 티켓 전체), 저장, 동기화, 성과 계산, 검토·첫 화면, 연결 확인.

실제 BTS 는 부르지 않는다. Jira·Redmine 응답은 실제 서버에서 녹화한 것이다(tests/bts_responses/*.json 의 milestone_source).
"""

import json
from datetime import date

import httpx2
import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_bts import MAPPING, adapter, an_issue
from test_service import DEV_ITEMS, triaged
from triage_service import cli, config, qa, review, sync
from triage_service.bts.fake import FakeTracker
from triage_service.bts.jira import JiraTracker
from triage_service.bts.redmine import RedmineTracker
from triage_service.contracts import CLOSED, IN_DEV, QA_REVIEW, RESOLVE, BtsState, BtsTicket, Milestone, TrackerError
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker

RULES = MAPPING["assign"]
PLAN = [Milestone("0.0.1", "2027-01-13"), Milestone("0.1.0", "2027-03-10"), Milestone("0.2.0", "2027-05-12")]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "service.db")


def fake(store, released=(), saved=True):
    tracker = FakeTracker(store.path)
    for m in PLAN:
        tracker.add_version(m.name, m.release, m.name in released)
    if saved:   # 동기화로 읽어 둔 목록 (검토 화면은 이것만 받는다)
        store.save_milestones(tracker.versions(), "t0")
    return tracker


def register(store, tracker, milestone="0.0.1", 처리="등록(재현 대기)", i=0):
    rid = triaged(store, DEV_ITEMS[i % len(DEV_ITEMS)])
    values = {**store.get(rid)["model_fields"], "처리": 처리}
    return review.register(store, tracker, rid, values, RULES, milestone=milestone)


# ── 어댑터 ───────────────────────────────────────────────

@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_versions(name, store):
    tracker, _ = adapter(name, store)
    assert tracker.versions() == PLAN   # 녹화: 두 BTS 에 같은 이름·출시일로 만든 버전


def test_redmine_closed_version_is_released(store):
    tracker, rec = adapter("redmine", store)
    rec.data["versions"]["body"]["versions"][0]["status"] = "closed"
    assert tracker.versions()[0].released and not tracker.versions()[1].released


def test_jira_create_with_milestone(store):
    tracker, rec = adapter("jira", store)
    issue = an_issue(store)
    tracker.create(issue.__class__(**{**issue.__dict__, "milestone": "0.0.1"}))
    sent = json.loads(next(r for r in rec.requests if r.method == "POST").content)
    assert sent["fields"]["fixVersions"] == [{"name": "0.0.1"}]
    tracker.create(an_issue(store))
    assert "fixVersions" not in json.loads([r for r in rec.requests if r.method == "POST"][-1].content)["fields"]


def test_redmine_create_with_milestone(store):
    tracker, rec = adapter("redmine", store)
    issue = an_issue(store)
    tracker.create(issue.__class__(**{**issue.__dict__, "milestone": "0.1.0"}))
    assert json.loads(next(r for r in rec.requests if r.method == "POST").content)["issue"]["fixed_version_id"] == 2   # 녹화: 0.1.0 = 2
    with pytest.raises(TrackerError, match="버전 9.9.9 이 없습니다"):
        tracker.create(issue.__class__(**{**issue.__dict__, "milestone": "9.9.9"}))
    assert sum(r.method == "POST" for r in rec.requests) == 1   # 없는 버전이면 보내기 전에 멈춘다


def test_jira_milestone_tickets(store):
    tracker, rec = adapter("jira", store)
    found = tracker.milestone_tickets("0.0.1")
    assert [(t.key, t.state.status, t.state.assignee) for t in found] == [("KAN-9", QA_REVIEW, "QA 담당")]
    assert found[0].title == "[실제 확인] S5-4b 마일스톤 등록"
    assert 'fixVersion = "0.0.1"' in rec.requests[-1].url.params["jql"] and "reconcileIssues" not in rec.requests[-1].url.params
    tracker.milestone_tickets("0.0.1")   # 방금 다룬 이슈는 reconcileIssues 로 최신 값을 받는다(설계 의도 10·11)
    assert rec.requests[-1].url.params.get_list("reconcileIssues") == [rec.data["milestone"]["body"]["issues"][0]["id"]]


def test_redmine_milestone_tickets(store):
    tracker, rec = adapter("redmine", store)
    found = tracker.milestone_tickets("0.0.1")
    assert [(t.key, t.state.status) for t in found] == [("3", QA_REVIEW)]
    p = rec.requests[-1].url.params
    assert (p["fixed_version_id"], p["status_id"], p["project_id"]) == ("1", "*", "triage")   # 닫힌 티켓도 읽는다


def issue_json(n):
    return {"id": str(10000 + n), "key": f"KAN-{n}", "fields": {"summary": f"t{n}", "status": {"name": "In Dev", "statusCategory": {"key": "indeterminate"}},
                                                               "assignee": None, "updated": "2026-11-24T10:00:00.000+0900"}}


def test_jira_milestone_reads_every_page(store, monkeypatch):
    monkeypatch.setattr(config, "MILESTONE_LIMIT", 150)
    calls = []

    def pages(request):
        calls.append(request.url.params.get("nextPageToken"))
        first = request.url.params.get("nextPageToken") is None
        rows = [issue_json(n) for n in (range(100) if first else range(100, 180))]
        return httpx2.Response(200, json={"issues": rows, "isLast": not first, **({"nextPageToken": "p2"} if first else {})})
    tracker = JiraTracker({**MAPPING["jira"]}, {}, httpx2.Client(base_url="https://x", transport=httpx2.MockTransport(pages)))
    found = tracker.milestone_tickets("0.0.1")
    assert calls == [None, "p2"] and len(found) == 150   # 상한에서 자른다


def test_redmine_milestone_reads_every_page(store):
    def pages(request):
        if request.url.path.endswith("versions.json"):
            return httpx2.Response(200, json={"versions": [{"id": 1, "name": "0.0.1", "status": "open"}]})
        off = int(request.url.params["offset"])
        rows = [{"id": n, "subject": "t", "status": {"name": "Closed"}} for n in range(off, min(off + 100, 130))]
        return httpx2.Response(200, json={"issues": rows, "total_count": 130})
    tracker = RedmineTracker({**MAPPING["redmine"]}, {}, httpx2.Client(base_url="http://x", transport=httpx2.MockTransport(pages)))
    assert len(tracker.milestone_tickets("0.0.1")) == 130


def test_fake_versions_and_tickets(store):
    tracker = fake(store)
    assert tracker.versions() == PLAN
    key = register(store, tracker)["ticket_key"]
    register(store, tracker, milestone="0.1.0", i=1)
    assert [t.key for t in tracker.milestone_tickets("0.0.1")] == [key]
    issue = an_issue(store)
    with pytest.raises(TrackerError, match="버전이 없습니다"):
        tracker.create(issue.__class__(**{**issue.__dict__, "milestone": "9.9.9"}))


# ── 저장 ─────────────────────────────────────────────────

def bt(key, status, title="t"):
    return BtsTicket(key, title, BtsState(status, "누군가", "2026-11-24T10:00:00"))


def test_store_milestones_replaced(store):
    store.save_milestones(PLAN, "t1")
    store.save_milestones([Milestone("0.2.0", "2027-05-12"), Milestone("x")], "t2")
    assert [(m["name"], m["synced_at"]) for m in store.milestones()] == [("0.2.0", "t2"), ("x", "t2")]   # 출시일 없는 버전은 뒤로


def test_store_tickets_changes_and_moved_out(store):
    store.save_tickets("0.0.1", [bt("A", QA_REVIEW), bt("B", RESOLVE)], "t1")
    store.save_tickets("0.0.1", [bt("A", QA_REVIEW), bt("B", IN_DEV)], "t2")   # B 재오픈
    store.save_tickets("0.0.1", [bt("B", IN_DEV)], "t3")                       # A 는 다른 버전으로 옮겨짐
    assert [t["key"] for t in store.tickets("0.0.1")] == ["B"]
    changes = store.ticket_changes()
    assert [(s, d) for _, s, d in changes["B"]] == [(None, RESOLVE), (RESOLVE, IN_DEV)] and len(changes["A"]) == 1
    store.ticket_status("B", RESOLVE, "t4")
    store.ticket_status("없는키", CLOSED, "t4")   # 마일스톤 티켓이 아니면 무시한다
    assert store.tickets("0.0.1")[0]["status"] == RESOLVE and "없는키" not in store.ticket_changes()


# ── 동기화 ───────────────────────────────────────────────

def test_sync_reads_versions_and_unreleased_milestones(store):
    tracker = fake(store, released={"0.2.0"})
    register(store, tracker)
    out = sync.run(store, tracker)
    assert out["milestones"] == [("0.0.1", 1), ("0.1.0", 0)] and not out["errors"]   # 출시한 0.2.0 은 읽지 않는다
    assert [m["name"] for m in store.milestones()] == ["0.0.1", "0.1.0", "0.2.0"]


def test_sync_milestone_failures_do_not_stop_others(store):
    class Flaky(FakeTracker):
        def milestone_tickets(self, name):
            if name == "0.0.1":
                raise TrackerError("검색 실패")
            return super().milestone_tickets(name)

        def versions(self):
            if getattr(self, "down", False):
                raise TrackerError("버전 목록 실패")
            return super().versions()
    tracker = Flaky(store.path)
    for m in PLAN:
        tracker.add_version(m.name, m.release)
    out = sync.run(store, tracker)
    assert out["errors"] == ["마일스톤 0.0.1: 검색 실패"] and [n for n, _ in out["milestones"]] == ["0.1.0", "0.2.0"]
    tracker.down = True
    out = sync.run(store, tracker)
    assert out["errors"] == ["마일스톤 목록: 버전 목록 실패"] and len(store.milestones()) == 3   # 읽어 둔 목록은 그대로


def test_current_milestone():
    ms = [{"name": "0.0.1", "released": True}, {"name": "0.1.0", "released": False}, {"name": "0.2.0", "released": False}]
    assert sync.current(ms) == "0.1.0" and sync.current([]) == ""


def test_milestone_summary(store):
    tracker = fake(store)
    recs = [register(store, tracker, i=i) for i in range(4)]
    keys = [r["ticket_key"] for r in recs]
    sync.run(store, tracker)
    # 0: QA 가 서비스에서 회귀 통과로 닫음 / 1: Resolve → In Dev 재오픈(BTS 쪽) / 2: BTS 에서 직접 닫음 / 3: QA Review 그대로
    for key in keys[:2]:
        tracker.set_status(key, RESOLVE)
    sync.run(store, tracker)
    assert qa.act(store, tracker, recs[0]["id"], "pass")["ok"]
    tracker.set_status(keys[1], IN_DEV)
    tracker.set_status(keys[2], CLOSED)
    sync.run(store, tracker)
    s = sync.milestone_summary(store, "0.0.1", today=date(2026, 11, 24))
    assert (s["total"], s["closed"], s["rate"], s["qa_closed"], s["reopened"]) == (4, 2, 50.0, 1, 1)
    assert s["left"] == {QA_REVIEW: 1, IN_DEV: 1, RESOLVE: 0, "Check": 0} and s["d_day"] == 50 and s["other"] == {}
    assert sync.milestone_summary(store, "없는 버전") is None
    empty = sync.milestone_summary(store, "0.1.0")
    assert empty["total"] == 0 and empty["rate"] is None


# ── 검토·화면 ─────────────────────────────────────────────

def test_register_with_milestone(store):
    tracker = fake(store, released={"0.2.0"})
    sync.run(store, tracker)
    rec = register(store, tracker, milestone="0.1.0")
    assert tracker.get(rec["ticket_key"])["milestone"] == "0.1.0" and rec["assignment"]["milestone"] == "0.1.0"
    for bad in ("0.2.0", "9.9.9"):   # 출시한 버전·모르는 버전
        with pytest.raises(ValueError, match="출시 전 마일스톤이 아닙니다"):
            register(store, tracker, milestone=bad)
    assert register(store, tracker, milestone="")["assignment"]["milestone"] == ""


def web(store, tracker):
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=RULES)
    return TestClient(app)


def test_web_home_milestone_card(store):
    tracker = fake(store, saved=False)
    client = web(store, tracker)
    assert "마일스톤(BTS 버전)을 아직 읽지 않았습니다" in client.get("/").text   # 동기화 전에는 BTS 를 부르지 않고 안내만
    client.post("/qa/sync")
    key = register(store, tracker)["ticket_key"]
    register(store, tracker, milestone="0.1.0", i=1)
    tracker.set_status(key, CLOSED)
    client.post("/qa/sync")
    page = client.get("/").text
    assert "마일스톤 0.0.1" in page and "/ 1건 (100.0%)" in page and "D-" in page
    page = client.get("/?milestone=0.1.0").text
    assert "마일스톤 0.1.0" in page and "/ 1건 (0.0%)" in page
    assert "마일스톤 0.0.1" in client.get("/?milestone=없는것").text   # 모르는 값이면 현재 마일스톤


def test_web_review_milestone_select(store):
    tracker = fake(store, saved=False)
    client = web(store, tracker)
    rid = triaged(store)
    assert 'name="milestone"' not in client.get(f"/reports/{rid}").text   # 목록을 읽기 전에는 칸이 없다
    client.post("/qa/sync")
    page = client.get(f"/reports/{rid}").text
    assert '<option value="0.0.1" selected>' in page and "출시 2027-03-10" in page
    values = store.get(rid)["model_fields"]
    data = {f: v for f, v in values.items() if f != "모듈"} | {"모듈": [m.strip() for m in values["모듈"].split("/")], "milestone": "9.9.9"}
    r = client.post(f"/reports/{rid}/register", data=data)
    assert r.status_code == 422 and "출시 전 마일스톤이 아닙니다" in r.text and '<option value="9.9.9" selected>' in r.text   # 다시 고르게 보인다
    client.post(f"/reports/{rid}/register", data={**data, "milestone": "0.1.0"})
    rec = store.get(rid)
    assert tracker.get(rec["ticket_key"])["milestone"] == "0.1.0" and "마일스톤: 0.1.0" in client.get(f"/reports/{rid}").text


def test_check_prints_milestones(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(config.MAPPING_PATH))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "service.db")
    assert cli.main(["--check"]) == 0
    assert "마일스톤(BTS 버전): 없음" in capsys.readouterr().out
    FakeTracker(tmp_path / "service.db").add_version("0.0.1", "2027-01-13")
    assert cli.main(["--check"]) == 0
    assert "마일스톤(BTS 버전): 0.0.1(2027-01-13)" in capsys.readouterr().out


# ── 독립 검토 반영 ───────────────────────────────────────

def test_jira_milestone_name_is_quoted(store):
    tracker, rec = adapter("jira", store)
    tracker.milestone_tickets(r'1.0 "hot\fix"')
    assert r'fixVersion = "1.0 \"hot\\fix\"" ORDER BY key' in rec.requests[-1].url.params["jql"]


def test_jira_reconcile_ids_stay_the_same_across_pages(store, monkeypatch):
    """페이지 토큰은 첫 질의에 묶여 있어 매개변수를 바꾸면 안 된다 — reconcileIssues 는 첫 페이지 전에 한 번만 정한다."""
    monkeypatch.setattr(config, "MILESTONE_LIMIT", 500)
    seen = []

    def pages(request):
        seen.append(request.url.params.get_list("reconcileIssues"))
        first = request.url.params.get("nextPageToken") is None
        rows = [issue_json(n) for n in (range(100) if first else range(100, 120))]
        return httpx2.Response(200, json={"issues": rows, "isLast": not first, **({"nextPageToken": "p2"} if first else {})})
    tracker = JiraTracker({**MAPPING["jira"]}, {}, httpx2.Client(base_url="https://x", transport=httpx2.MockTransport(pages)))
    tracker.seen("KAN-1", "777")
    tracker.milestone_tickets("0.0.1")
    assert seen == [["777"], ["777"]]


def test_sync_warns_when_milestone_limit_reached(store, monkeypatch):
    monkeypatch.setattr(config, "MILESTONE_LIMIT", 1)
    tracker = fake(store)
    register(store, tracker)
    assert any("1건까지만 읽었습니다" in e for e in sync.run(store, tracker)["errors"])


def test_linked_existing_ticket_keeps_no_milestone(store):
    from triage_service.contracts import Issue
    tracker = fake(store)
    rid = triaged(store)
    fields = {**store.get(rid)["model_fields"], "처리": "등록(재현 대기)"}
    tracker.create(Issue(report=store.get(rid)["report"], fields=fields, ref=store.ref(rid)))   # 끊긴 요청이 남긴 티켓
    rec = review.register(store, tracker, rid, fields, RULES, milestone="0.0.1")
    assert rec["assignment"]["linked_existing"] and rec["assignment"]["milestone"] == ""


def test_web_bad_milestone_is_not_silently_replaced(store):
    tracker = fake(store)
    client = web(store, tracker)
    rid = triaged(store)
    values = store.get(rid)["model_fields"]
    data = {f: v for f, v in values.items() if f != "모듈"} | {"모듈": [m.strip() for m in values["모듈"].split("/")], "milestone": "0.9.9"}
    page = client.post(f"/reports/{rid}/register", data=data).text
    assert '<option value="0.9.9" selected>0.9.9 — 출시 전 마일스톤 아님' in page and '<option value="0.0.1" selected>' not in page
