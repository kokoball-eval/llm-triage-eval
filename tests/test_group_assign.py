"""팀 그룹 배정 시험 (S5-5a) — 팀 = BTS 그룹, 담당자 = 미해결이 가장 적은 구성원, 긴급 사인 요청 = QA팀장 사인 후 개발 전달.

실제 BTS 는 부르지 않는다. Jira·Redmine 응답은 실제 서버에서 녹화한 것이다(tests/bts_responses/*.json 의 group_source).
"""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_bts import MAPPING, adapter
from test_qa import register
from test_service import DEV_ITEMS, triaged
from triage_service import assign, flow, qa, review
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import IN_DEV, QA_REVIEW, Member, TrackerError
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker

RULES = MAPPING["assign"]
A, B = Member("a", "가"), Member("b", "나")


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "service.db")


# ── 규칙 ────────────────────────────────────────────────

def test_least_busy_then_least_recent():
    assert assign.least_busy([(A, 3), (B, 1)], []) == B                 # 미해결이 적은 사람
    assert assign.least_busy([(A, 1), (B, 1)], ["a"]) == B              # 같으면 배정한 적 없는 사람
    assert assign.least_busy([(A, 1), (B, 1)], ["a", "b"]) == B         # 둘 다 있으면 더 오래전에 받은 사람(b)
    assert assign.least_busy([(A, 1), (B, 1)], ["b", "a", "b"]) == A


def test_assignee_urgent_goes_to_qa_lead(store):
    tracker = FakeTracker(store.path)
    who = assign.assignee(tracker, RULES, "QA", {"처리": assign.URGENT}, [], choice="QA팀원")
    assert who == {"id": "QA팀장", "name": "QA팀장", "rule": "QA팀장"}   # 사람이 고른 구성원은 쓰지 않는다(기준서 §0)
    assert assign.assignee(tracker, RULES, "", {"처리": "폐기"}, []) == {}


def test_assignee_choice_must_be_a_member(store):
    tracker = FakeTracker(store.path)
    assert assign.assignee(tracker, RULES, "서버", {"처리": "개발 배정"}, [], "서버팀원")["id"] == "서버팀원"
    with pytest.raises(ValueError, match="서버 팀 구성원이 아닙니다"):
        assign.assignee(tracker, RULES, "서버", {"처리": "개발 배정"}, [], "클라팀원")


def test_empty_group_is_an_error(store):
    class Empty(FakeTracker):
        def members(self, team):
            return []
    with pytest.raises(TrackerError, match="서버 팀 그룹에 구성원이 없습니다"):
        assign.candidates(Empty(store.path), "서버")


# ── 등록 ────────────────────────────────────────────────

def test_registrations_spread_over_members(store):
    """미해결이 적은 사람에게 — 두 사람 그룹이면 번갈아 받는다(가짜 BTS 의 미해결 수는 실제로 센다)."""
    tracker = FakeTracker(store.path)
    got = [register(store, tracker, 처리="개발 배정", 모듈="결제·재화", i=i)["assignment"]["member"]["id"] for i in range(4)]
    assert got == ["서버팀장", "서버팀원", "서버팀장", "서버팀원"]
    assert [tracker.open_count(m) for m in ("서버팀장", "서버팀원")] == [2, 2]


def test_urgent_registers_to_qa_lead_in_qa_review(store):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker, 처리=assign.URGENT, 모듈="그래픽·사운드")
    a, t = rec["assignment"], tracker.get(rec["ticket_key"])
    assert (a["team"], a["member"]["id"], a["qa_owner"]["id"]) == ("QA", "QA팀장", "QA팀장")
    assert (t["status"], t["team"], t["assignee"]) == (QA_REVIEW, "QA", "QA팀장")


def test_qa_owner_only_for_qa_team(store):
    tracker = FakeTracker(store.path)
    assert register(store, tracker, 처리="등록(재현 대기)")["assignment"]["qa_owner"]["id"].startswith("QA")
    assert register(store, tracker, 처리="개발 배정", i=1)["assignment"]["qa_owner"] is None


def test_bad_member_choice_stays_in_review(store):
    tracker = FakeTracker(store.path)
    rid = triaged(store, DEV_ITEMS[0])
    values = {**store.get(rid)["model_fields"], "처리": "개발 배정", "모듈": "결제·재화"}
    with pytest.raises(ValueError, match="구성원이 아닙니다"):
        review.register(store, tracker, rid, values, RULES, member="없는사람")
    assert store.get(rid)["state"] == flow.REVIEW and tracker.find(store.ref(rid)) is None


def test_group_read_failure_fails_registration(store):
    class Down(FakeTracker):
        def members(self, team):
            raise TrackerError("그룹 조회 거절")
    tracker = Down(store.path)
    rid = triaged(store, DEV_ITEMS[0])
    values = {**store.get(rid)["model_fields"], "처리": "개발 배정"}
    rec = review.register(store, tracker, rid, values, RULES)
    assert rec["state"] == flow.FAILED and "그룹 조회 거절" in rec["error"] and tracker.find(store.ref(rid)) is None


# ── 팀장 사인 ────────────────────────────────────────────

def test_lead_signs_urgent_and_hands_to_dev(store):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker, 처리=assign.URGENT, 모듈="그래픽·사운드")
    with pytest.raises(ValueError, match=r"\[사인 후 개발 전달\]로 넘깁니다"):
        qa.act(store, tracker, rec["id"], "repro", "재현됨", rules=RULES)
    out = qa.act(store, tracker, rec["id"], "sign", rules=RULES)
    t = tracker.get(rec["ticket_key"])
    assert out["ok"] and (t["status"], t["team"], t["assignee"]) == (IN_DEV, "아트", "아트팀장")
    assert t["comments"] == ["[트리아지 서비스 · 사인 후 개발 전달] (담당: 아트팀장)"]
    assert store.get(rec["id"])["assignment"]["qa_owner"]["id"] == "QA팀장"   # 개발로 넘어가도 QA 담당은 그대로


def test_only_qa_lead_can_sign(store):
    class Member2(FakeTracker):
        def me(self):
            return "QA팀원"
    tracker = Member2(store.path)
    rec = register(store, tracker, 처리=assign.URGENT)
    with pytest.raises(ValueError, match="QA팀장만 사인할 수 있습니다"):
        qa.act(store, tracker, rec["id"], "sign", rules=RULES)
    assert tracker.get(rec["ticket_key"])["status"] == QA_REVIEW


def test_sign_is_only_for_urgent(store):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker, 처리="등록(재현 대기)")
    with pytest.raises(ValueError, match="긴급 사인 요청 건에만"):
        qa.act(store, tracker, rec["id"], "sign", rules=RULES)
    assert qa.allowed(QA_REVIEW) == ["repro", "hold", "comment"] and qa.allowed(QA_REVIEW, urgent=True) == ["sign", "hold", "comment"]


# ── 어댑터 (녹화 응답) ──────────────────────────────────────

def test_jira_members_count_me(store):
    tracker, rec = adapter("jira", store)
    assert tracker.members("QA") == [Member("dev-account-id", "개발 담당"), Member("qa-account-id", "QA 담당")]
    assert rec.requests[-1].url.params["groupname"] == "QA팀"
    assert tracker.open_count("qa-account-id") == 2   # 녹화: 미해결 2건(KAN-8·9)
    p = rec.requests[-1].url.params
    assert p["jql"] == 'project = "TRI" AND assignee = "qa-account-id" AND statusCategory != Done' and p["fields"] == "id"
    assert tracker.me() == "qa-account-id" and tracker.lead("QA") == MAPPING["jira"]["leads"]["QA"] and tracker.lead("서버") == ""


def test_redmine_members_count_me(store):
    tracker, rec = adapter("redmine", store)
    assert tracker.members("QA") == [Member("1", "QA 팀장"), Member("5", "QA 팀원")]
    assert rec.requests[-1].url.path == "/groups/11.json" and rec.requests[-1].url.params["include"] == "users"
    assert tracker.open_count("1") == 3
    p = rec.requests[-1].url.params
    assert (p["assigned_to_id"], p["status_id"], p["project_id"]) == ("1", "open", "triage")
    assert tracker.me() == "1" and tracker.lead("QA") == "1"


# ── 화면 ────────────────────────────────────────────────

def web(store, tracker):
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=RULES)
    return TestClient(app)


def form(store, rid, **extra):
    values = store.get(rid)["model_fields"]
    return {f: v for f, v in values.items() if f != "모듈"} | {"모듈": [m.strip() for m in values["모듈"].split("/")]} | extra


def test_web_review_shows_members_and_takes_choice(store):
    tracker = FakeTracker(store.path)
    client = web(store, tracker)
    rid = triaged(store, DEV_ITEMS[0])
    data = form(store, rid, 처리="개발 배정", 모듈=["결제·재화"])
    page = client.post(f"/reports/{rid}/register", data={**data, "member": "없는사람"})
    assert page.status_code == 422 and "구성원이 아닙니다" in page.text
    assert "서버팀원 (미해결 0건)" in page.text and "규칙대로 — 서버팀장" in page.text
    client.post(f"/reports/{rid}/register", data={**data, "member": "서버팀원"})
    rec = store.get(rid)
    assert rec["assignment"]["member"]["id"] == "서버팀원" and "담당자: 서버팀원 (사람이 고름)" in client.get(f"/reports/{rid}").text


def test_web_urgent_shows_lead_and_sign_button(store):
    tracker = FakeTracker(store.path)
    client = web(store, tracker)
    rid = triaged(store, DEV_ITEMS[0])
    assert "QA팀장 — 긴급 사인 요청은 위에서 고른 팀과 관계없이 리드 QA 가 받아" in client.post(
        f"/reports/{rid}/register", data={**form(store, rid, 처리=assign.URGENT), "milestone": "없는것"}).text
    client.post(f"/reports/{rid}/register", data=form(store, rid, 처리=assign.URGENT))
    client.post("/qa/sync")   # QA 동작 버튼은 읽어 둔 BTS 상태로 고른다
    page = client.get(f"/reports/{rid}").text
    assert "사인 후 개발 전달 → In Dev" in page and "재현 확인 → In Dev" not in page and "QA 담당: QA팀장" in page


def test_web_review_survives_group_read_failure(store):
    class Down(FakeTracker):
        def members(self, team):
            raise TrackerError("그룹 조회 거절")
    client = web(store, Down(store.path))
    rid = triaged(store, DEV_ITEMS[0])
    page = client.post(f"/reports/{rid}/register", data={**form(store, rid, 처리="개발 배정"), "milestone": "없는것"})
    assert page.status_code == 422 and "구성원을 읽지 못했습니다" in page.text   # 화면은 열린다(등록할 때 다시 읽음)


# ── 독립 검토 반영 ───────────────────────────────────────

def test_urgent_ignores_team_override(store):
    """긴급 사인 요청은 사람이 다른 팀을 골라도 QA팀장에게 간다 (assign 설계 의도 10, 판정 기준서 §0)."""
    tracker = FakeTracker(store.path)
    rid = triaged(store, DEV_ITEMS[1])
    values = {**store.get(rid)["model_fields"], "처리": assign.URGENT}
    rec = review.register(store, tracker, rid, values, RULES, "클라")
    assert (rec["assignment"]["team"], rec["assignment"]["member"]["id"]) == ("QA", "QA팀장")


def test_urgent_without_rules_uses_repro(store):
    """자동 배정을 쓰지 않으면 팀장이 없으니 긴급 사인 요청도 재현 확인으로 넘긴다."""
    tracker = FakeTracker(store.path)
    rid = triaged(store, DEV_ITEMS[0])
    rec = review.register(store, tracker, rid, {**store.get(rid)["model_fields"], "처리": assign.URGENT}, None)
    assert qa.act(store, tracker, rec["id"], "repro", "재현됨")["ok"]
    with pytest.raises(ValueError, match="자동 배정을 쓸 때"):
        qa.act(store, tracker, rec["id"], "sign")


def test_bad_bts_json_is_a_bts_failure_not_input(store):
    """BTS 응답 해석 오류(ValueError 의 하위)는 입력 문제가 아니라 등록 실패다."""
    import json as _json

    class Garbled(FakeTracker):
        def open_count(self, member):
            raise _json.JSONDecodeError("Expecting value", "<html>", 0)
    tracker = Garbled(store.path)
    rid = triaged(store, DEV_ITEMS[1])
    rec = review.register(store, tracker, rid, {**store.get(rid)["model_fields"], "처리": "개발 배정"}, RULES)
    assert rec["state"] == flow.FAILED and "Expecting value" in rec["error"]


def test_linking_existing_ticket_does_not_read_group(store):
    from triage_service.contracts import Issue

    class NoGroup(FakeTracker):
        def members(self, team):
            raise TrackerError("그룹 조회 거절")
    tracker = NoGroup(store.path)
    rid = triaged(store, DEV_ITEMS[1])
    values = {**store.get(rid)["model_fields"], "처리": "개발 배정"}
    tracker.create(Issue(report=store.get(rid)["report"], fields=values, ref=store.ref(rid)))
    rec = review.register(store, tracker, rid, values, RULES)
    assert rec["state"] == flow.REGISTERED and rec["assignment"]["linked_existing"] and rec["assignment"]["member"] is None


def test_old_teams_without_leads_explains_the_change(tmp_path):
    from test_bts import write_mapping
    from triage_service import config
    from triage_service.bts import settings
    text = config.MAPPING_PATH.read_text(encoding="utf-8").replace('use = "fake"', 'use = "redmine"').replace(chr(10) + "QA = 1" + chr(10), chr(10))
    with pytest.raises(settings.SettingsError, match="팀 → 계정이 아니라 팀 → BTS 그룹"):
        settings.load(write_mapping(tmp_path, text), env={"REDMINE_API_KEY": "k"})


def test_jira_open_count_reads_fresh_and_every_page(store):
    """방금 등록·배정한 이슈도 바로 센다 — 검색에 최근 이슈 ID(reconcileIssues)를 붙이고 끝 페이지까지 센다 (jira 설계 의도 13).
    approximate-count 는 색인이라 연달아 등록한 두 건이 같은 사람에게 갔다(KAN-11·12)."""
    import httpx2

    from triage_service.bts.jira import JiraTracker
    seen = []

    def pages(request):
        seen.append((request.url.params.get_list("reconcileIssues"), request.url.params.get("nextPageToken")))
        first = request.url.params.get("nextPageToken") is None
        rows = [{"id": str(n)} for n in (range(1000) if first else range(3))]
        return httpx2.Response(200, json={"issues": rows, "isLast": not first, **({"nextPageToken": "p2"} if first else {})})
    tracker = JiraTracker({**MAPPING["jira"]}, {}, httpx2.Client(base_url="https://x", transport=httpx2.MockTransport(pages)))
    tracker.seen("KAN-12", "10021")   # 방금 등록한 이슈
    assert tracker.open_count("someone") == 1003
    assert seen == [(["10021"], None), (["10021"], "p2")]

