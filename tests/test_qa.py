"""QA 동작 시험 (S5-3) — 어댑터 쓰기 계약(가짜·Jira·Redmine), 첫 상태와 CS 응대 재현 선택, 동작 허용 조건·상태 경합·쓰기 확인,
화면, 연결 확인.

실제 BTS 는 부르지 않는다. Jira·Redmine 쓰기 응답은 실제 서버에서 녹화한 것이다(tests/bts_responses/*.json 의 write_source).
"""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_bts import MAPPING, adapter
from test_service import DEV_ITEMS, triaged
from triage_service import assign, cli, config, qa, review
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import CHECK, CLOSED, IN_DEV, QA_REVIEW, RESOLVE, BtsState, Issue, TrackerError
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker

RULES = MAPPING["assign"]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "service.db")


def register(store, tracker, 처리="개발 배정", 모듈="결제·재화", 분류=None, repro=assign.AUTO, i=0) -> dict:
    rid = triaged(store, DEV_ITEMS[i])
    values = {**store.get(rid)["model_fields"], "처리": 처리, "모듈": 모듈}
    if 분류:
        values["분류"] = 분류
    return review.register(store, tracker, rid, values, RULES, repro=repro)


# ── 어댑터 쓰기 계약 ─────────────────────────────────────

def test_jira_update_sends_assignee_transition_comment(store):
    tracker, rec = adapter("jira", store)
    key = rec.data["ticket_key"]
    tracker.update(key, status=IN_DEV, team="서버", comment="[트리아지 서비스 · 재현 확인] 1줄\n2줄", assignee="dev-account-id")
    sent = [(r.method, r.url.path, json.loads(r.content) if r.content else None) for r in rec.requests]
    edit = sent[0][2]
    assert sent[0][:2] == ("PUT", f"/rest/api/3/issue/{key}")
    assert edit["fields"]["assignee"] == {"accountId": "dev-account-id"}   # 팀 그룹에서 고른 구성원 (jira 설계 의도 13)
    assert {"add": "팀_서버"} in edit["update"]["labels"] and {"remove": "팀_QA"} in edit["update"]["labels"]
    assert {"remove": "팀_서버"} not in edit["update"]["labels"]
    assert sent[1][:2] == ("GET", f"/rest/api/3/issue/{key}/transitions")
    assert sent[2] == ("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": "21"}})   # 녹화: 21 → In Dev
    texts = [p["content"][0]["text"] for p in sent[3][2]["body"]["content"]]
    assert sent[3][:2] == ("POST", f"/rest/api/3/issue/{key}/comment") and texts == ["[트리아지 서비스 · 재현 확인] 1줄", "2줄"]


def test_jira_closed_goes_by_done_category(store):
    """'종료'로 번역된 상태라도 완료 범주로 가는 전환을 고른다(jira.py 설계 의도 8)."""
    tracker, rec = adapter("jira", store)
    tracker.update(rec.data["ticket_key"], status=CLOSED)
    assert json.loads(rec.requests[-1].content) == {"transition": {"id": "41"}}
    assert [r.method for r in rec.requests] == ["GET", "POST"]   # 팀·코멘트가 비면 보내지 않는다


def test_jira_missing_transition_is_an_error(store):
    tracker, rec = adapter("jira", store)
    rec.data["transitions"]["body"]["transitions"] = [t for t in rec.data["transitions"]["body"]["transitions"]
                                                      if t["to"]["name"] != RESOLVE]
    with pytest.raises(TrackerError, match="Resolve 로 가는 전환이 없습니다"):
        tracker.update(rec.data["ticket_key"], status=RESOLVE)
    assert all(r.method == "GET" for r in rec.requests)


def test_jira_check_statuses(store):
    tracker, rec = adapter("jira", store)
    assert tracker.check_statuses() == []   # 녹화: 종료(완료 범주) · Check · In Dev · QA Review · Resolve
    statuses = rec.data["project_statuses"]["body"][0]["statuses"]
    statuses[:] = [s for s in statuses if s["name"] != "Check"] + [{"name": "완료", "id": "99", "statusCategory": {"key": "done"}}]
    problems = tracker.check_statuses()
    assert "Jira 에 Check 상태가 없습니다" in problems and any("완료 범주 상태가 둘 이상" in p for p in problems)


def test_redmine_update_is_one_put(store):
    tracker, rec = adapter("redmine", store)
    tracker.update("1", status=IN_DEV, team="서버", comment="코멘트", assignee="8")
    put = rec.requests[-1]
    assert (put.method, put.url.path) == ("PUT", "/issues/1.json")
    assert json.loads(put.content) == {"issue": {"status_id": 8, "assigned_to_id": 8,   # 구성원(서버 팀장) 번호
                                                 "category_id": 4, "notes": "코멘트"}}   # 녹화: 서버 범주 = 4
    tracker.update("1", comment="코멘트만")
    assert json.loads(rec.requests[-1].content) == {"issue": {"notes": "코멘트만"}}


def test_redmine_unknown_status_and_check(store):
    tracker, rec = adapter("redmine", store)
    assert tracker.check_statuses() == []
    with pytest.raises(TrackerError, match="Redmine 에 Rejected 상태가 없습니다"):
        tracker.update("1", status="Rejected")
    rec.data["issue_statuses"]["body"]["issue_statuses"].pop()
    assert tracker.check_statuses() == ["Redmine 에 Closed 상태가 없습니다"]


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_update_rejection_is_a_tracker_error(name, store):
    tracker, rec = adapter(name, store, fail={"edit" if name == "jira" else "update": "reject"})
    with pytest.raises(TrackerError) as e:
        tracker.update(rec.data["ticket_key"], team="QA")
    assert "SECRET-TOKEN-123" not in str(e.value)


def test_fake_update(store):
    tracker = FakeTracker(store.path)
    key = register(store, tracker, 처리="등록(재현 대기)")["ticket_key"]
    tracker.update(key, status=IN_DEV, team="서버", comment="a")
    tracker.update(key, comment="b")
    t = tracker.get(key)
    assert (t["status"], t["team"], t["comments"]) == (IN_DEV, "서버", ["a", "b"])


# ── 첫 상태와 CS 응대 ─────────────────────────────────────

@pytest.mark.parametrize("처리, repro, status", [
    ("등록(재현 대기)", False, QA_REVIEW), ("정보 요청 후 보류", False, QA_REVIEW),
    ("개발 배정", False, IN_DEV), ("긴급 사인 요청", False, QA_REVIEW),   # 리드 QA 사인 먼저(기준서 §0)
    ("CS 응대", True, QA_REVIEW), ("CS 응대", False, CHECK),
])
def test_first_status(처리, repro, status):
    assert assign.first_status({"처리": 처리}, repro) == status


def test_cs_repro_default_and_choice():
    assert assign.cs_repro({"분류": "결함"}) is True and assign.cs_repro({"분류": "문의·건의"}) is False
    assert assign.cs_repro({"분류": "결함"}, "필요 없음") is False and assign.cs_repro({"분류": "무관"}, "필요") is True
    assert assign.suggest(RULES, {"처리": "CS 응대"}, True) == RULES["qa"]
    assert assign.suggest(RULES, {"처리": "CS 응대"}, False) == RULES["cs"]


def test_register_writes_first_status(store):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker, 처리="개발 배정", 모듈="그래픽·사운드")
    assert tracker.status(rec["ticket_key"]) == IN_DEV and store.get(rec["id"])["bts"]["status"] == IN_DEV
    [log] = store.qa_log()[rec["id"]]
    assert (log["action"], log["before"], log["after"], log["error"]) == (qa.FIRST, QA_REVIEW, IN_DEV, None)


def test_register_keeps_qa_review_without_writing(store):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker, 처리="등록(재현 대기)")
    assert tracker.status(rec["ticket_key"]) == QA_REVIEW and rec["id"] not in store.qa_log()


@pytest.mark.parametrize("분류, repro, status, team", [
    ("결함", assign.AUTO, QA_REVIEW, "QA"), ("문의·건의", assign.AUTO, CHECK, "운영"),
    ("문의·건의", "필요", QA_REVIEW, "QA"), ("결함", "필요 없음", CHECK, "운영"),
])
def test_register_cs_case(store, 분류, repro, status, team):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker, 처리="CS 응대", 분류=분류, repro=repro)
    t = tracker.get(rec["ticket_key"])
    assert (t["status"], t["team"], rec["assignment"]["team"]) == (status, team, team)
    if status == CHECK:   # 보류는 사유 코멘트를 남긴다
        assert "CS 응대" in t["comments"][0]


def test_register_first_status_failure_keeps_registration(store):
    class NoWrite(FakeTracker):
        def update(self, key, status="", team="", comment="", assignee=""):
            raise TrackerError("BTS 점검 중")
    tracker = NoWrite(store.path)
    rec = register(store, tracker, 처리="개발 배정")
    assert rec["state"] == "등록 완료" and tracker.status(rec["ticket_key"]) == QA_REVIEW
    [log] = store.qa_log()[rec["id"]]
    assert "BTS 점검 중" in log["error"] and log["after"] == QA_REVIEW


def test_register_linked_existing_does_not_touch_status(store):
    tracker = FakeTracker(store.path)
    rid = triaged(store)
    fields = {**store.get(rid)["model_fields"], "처리": "개발 배정"}
    key = tracker.create(Issue(report=store.get(rid)["report"], fields=fields, ref=store.ref(rid))).key
    review.register(store, tracker, rid, fields, RULES)
    assert tracker.status(key) == QA_REVIEW and rid not in store.qa_log()


# ── QA 동작 ──────────────────────────────────────────────

def ready(store, status, 처리="등록(재현 대기)", 모듈="결제·재화"):
    tracker = FakeTracker(store.path)
    rec = register(store, tracker, 처리=처리, 모듈=모듈)
    tracker.set_status(rec["ticket_key"], status)
    return tracker, rec["id"], rec["ticket_key"]


def test_allowed_actions():
    assert qa.allowed(QA_REVIEW) == ["repro", "hold", "comment"]
    assert qa.allowed(RESOLVE) == ["pass", "fail", "hold", "comment"]
    assert qa.allowed(IN_DEV) == ["hold", "comment"] and qa.allowed(CHECK) == ["comment"]
    assert qa.allowed(CLOSED) == [] and qa.allowed(None) == []


def test_repro_moves_to_module_team(store):
    tracker, rid, key = ready(store, QA_REVIEW, 모듈="그래픽·사운드")
    out = qa.act(store, tracker, rid, "repro", "재현 절차 1~3, 안드로이드 14", rules=RULES)
    t = tracker.get(key)
    assert out == {"ok": True, "after": IN_DEV, "error": None}
    assert (t["status"], t["team"], t["assignee"]) == (IN_DEV, "아트", "아트팀장")   # 그룹 구성원 중 미해결이 적은 사람
    assert t["comments"] == ["[트리아지 서비스 · 재현 확인] 재현 절차 1~3, 안드로이드 14 (담당: 아트팀장)"]
    log = store.qa_log()[rid][-1]
    assert (log["action"], log["before"], log["after"], log["team"]) == ("재현 확인", QA_REVIEW, IN_DEV, "아트")
    assert store.get(rid)["bts"]["status"] == IN_DEV and store.get(rid)["assignment"]["team"] == "QA"   # 등록 때 배정은 그대로


def test_repro_with_chosen_and_bad_team(store):
    tracker, rid, key = ready(store, QA_REVIEW)
    with pytest.raises(ValueError, match="설정에 없는 팀"):
        qa.act(store, tracker, rid, "repro", "재현됨", team="없는팀", rules=RULES)
    qa.act(store, tracker, rid, "repro", "재현됨", team="클라", rules=RULES)
    assert tracker.get(key)["team"] == "클라"


def test_repro_without_rules_changes_status_only(store):
    tracker, rid, key = ready(store, QA_REVIEW)
    qa.act(store, tracker, rid, "repro", "재현됨", rules=None)
    assert tracker.get(key)["status"] == IN_DEV and tracker.get(key)["team"] == "QA"


@pytest.mark.parametrize("action, target", [("pass", CLOSED), ("fail", IN_DEV), ("hold", CHECK)])
def test_regression_and_hold(store, action, target):
    tracker, rid, key = ready(store, RESOLVE, 처리="개발 배정")
    assert qa.act(store, tracker, rid, action, "사유")["after"] == target and tracker.status(key) == target


def test_pass_without_comment_but_others_need_one(store):
    tracker, rid, key = ready(store, RESOLVE, 처리="개발 배정")
    for action in ("fail", "hold", "comment"):
        with pytest.raises(ValueError, match="코멘트"):
            qa.act(store, tracker, rid, action, "  ")
    assert qa.act(store, tracker, rid, "pass")["ok"] and tracker.get(key).get("comments", []) == []


def test_comment_keeps_status(store):
    tracker, rid, key = ready(store, CHECK, 처리="개발 배정")
    assert qa.act(store, tracker, rid, "comment", "일정 확인 중")["after"] == CHECK
    assert tracker.get(key)["comments"][-1] == "[트리아지 서비스 · 코멘트] 일정 확인 중"


def test_conflict_when_bts_changed(store):
    """화면은 Resolve 를 보고 있었지만 그사이 개발자가 In Dev 로 되돌렸다 — 쓰지 않고, 지금 상태를 저장한다."""
    tracker, rid, key = ready(store, RESOLVE, 처리="개발 배정")
    store.save_bts(rid, {"status": RESOLVE, "assignee": "", "updated": "", "synced_at": "t"})
    tracker.set_status(key, IN_DEV)
    with pytest.raises(qa.Conflict, match="지금 In Dev"):
        qa.act(store, tracker, rid, "pass")
    assert tracker.status(key) == IN_DEV and store.get(rid)["bts"]["status"] == IN_DEV
    assert [a["action"] for a in store.qa_log()[rid]] == [qa.FIRST]   # 동작 기록은 남지 않는다


def test_double_submit_is_refused(store):
    tracker, rid, key = ready(store, RESOLVE, 처리="개발 배정")
    qa.act(store, tracker, rid, "pass")
    with pytest.raises(qa.Conflict):
        qa.act(store, tracker, rid, "pass")


def test_write_failure_and_ignored_change_are_recorded(store):
    class Broken(FakeTracker):
        mode = "ok"

        def update(self, key, status="", team="", comment="", assignee=""):
            if self.mode == "ok":
                return super().update(key, status, team, comment, assignee)
            if self.mode == "raise":
                raise TrackerError("BTS 가 거절했습니다 (PUT → 422)")
            # mode == "ignore": 응답은 성공인데 상태가 그대로(워크플로가 막은 경우)
    tracker = Broken(store.path)
    rec = register(store, tracker, 처리="등록(재현 대기)")
    rid = rec["id"]
    tracker.set_status(rec["ticket_key"], RESOLVE)
    tracker.mode = "raise"
    out = qa.act(store, tracker, rid, "fail", "다시 남")
    assert not out["ok"] and "422" in out["error"] and out["after"] == RESOLVE
    tracker.mode = "ignore"
    out = qa.act(store, tracker, rid, "pass")
    assert not out["ok"] and "바꾸지 않았습니다" in out["error"]
    assert [a["error"] is not None for a in store.qa_log()[rid]] == [True, True]


def test_failed_write_does_not_claim_team(store):
    """쓰기 요청이 도중에 실패하면 팀이 바뀌었는지 모른다 — 기록의 팀을 비우고 BTS 에서 확인하라고 알린다."""
    class Halfway(FakeTracker):
        def update(self, key, status="", team="", comment="", assignee=""):
            raise TrackerError("전환 거절")
    tracker = Halfway(store.path)
    rid = register(store, tracker, 처리="등록(재현 대기)")["id"]
    out = qa.act(store, tracker, rid, "repro", "재현됨", rules=RULES)
    assert not out["ok"] and "담당 팀 서버 변경 여부는 BTS 에서 확인" in out["error"]
    assert store.qa_log()[rid][-1]["team"] == ""


def test_bts_read_failure_sends_nothing(store):
    class Down(FakeTracker):
        down = False

        def status(self, key):
            if self.down:
                raise TrackerError("BTS 응답 없음")
            return super().status(key)
    tracker = Down(store.path)
    rec = register(store, tracker, 처리="등록(재현 대기)")
    tracker.down = True
    with pytest.raises(qa.Conflict, match="읽지 못해 보내지 않았습니다"):
        qa.act(store, tracker, rec["id"], "comment", "메모")
    assert "comments" not in tracker.get(rec["ticket_key"])


def test_verify_uses_single_read_not_search(store):
    """Jira 검색(snapshot)은 색인이라 늦을 수 있다 — 쓴 뒤 확인은 단건 조회(status)로 한다."""
    class StaleSearch(FakeTracker):
        def snapshot(self, keys):
            return {k: BtsState(RESOLVE, "옛 담당", "옛 시각") for k in keys}
    tracker = StaleSearch(store.path)
    _, rid, key = ready(store, RESOLVE, 처리="개발 배정")
    out = qa.act(store, tracker, rid, "pass")
    assert out == {"ok": True, "after": CLOSED, "error": None} and store.get(rid)["bts"]["status"] == CLOSED


def test_repro_does_not_send_to_qa_team(store):
    tracker, rid, key = ready(store, QA_REVIEW, 모듈="해당 없음")
    with pytest.raises(ValueError, match="넘길 팀을 고르세요"):
        qa.act(store, tracker, rid, "repro", "재현됨", rules=RULES)
    qa.act(store, tracker, rid, "repro", "재현됨", team="서버", rules=RULES)
    assert tracker.get(key)["team"] == "서버"


def test_act_input_errors(store):
    tracker, rid, key = ready(store, QA_REVIEW)
    with pytest.raises(ValueError, match="모르는 동작"):
        qa.act(store, tracker, rid, "delete", "x")
    other = triaged(store)   # 티켓 없음
    with pytest.raises(ValueError, match="티켓이 없는"):
        qa.act(store, tracker, other, "comment", "x")


# ── 화면 ───────────────────────────────────────────────

def web_client(store, tracker):
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=RULES)
    return TestClient(app)


def test_web_detail_shows_allowed_actions_and_acts(store):
    tracker, rid, key = ready(store, QA_REVIEW, 모듈="네트워크")
    client = web_client(store, tracker)
    page = client.get(f"/reports/{rid}").text
    assert "BTS 상태를 아직 읽지 않았습니다" in page   # 읽은 상태가 없으면 버튼을 내지 않는다
    client.post("/qa/sync")
    page = client.get(f"/reports/{rid}").text
    assert 'value="repro"' in page and 'value="pass"' not in page and "규칙대로 — 서버" in page
    r = client.post(f"/reports/{rid}/qa", data={"action": "repro", "comment": "재현됨", "team": assign.AUTO}, follow_redirects=False)
    assert r.status_code == 303 and tracker.get(key)["team"] == "서버"
    page = client.get(f"/reports/{rid}").text
    assert "재현 확인" in page and "QA Review → In Dev" in page


def test_web_qa_refusals(store):
    tracker, rid, key = ready(store, RESOLVE, 처리="개발 배정")
    client = web_client(store, tracker)
    r = client.post(f"/reports/{rid}/qa", data={"action": "fail", "comment": ""})
    assert r.status_code == 422 and "코멘트(사유)가 필요" in r.text
    r = client.post(f"/reports/{rid}/qa", data={"action": "repro", "comment": "남겨 둘 코멘트"})
    assert r.status_code == 409 and "지금 Resolve" in r.text and "남겨 둘 코멘트" in r.text   # 쓴 코멘트를 잃지 않는다
    assert client.post("/reports/999/qa", data={"action": "pass"}).status_code == 404
    r = client.post(f"/reports/{rid}/qa", data={"action": "pass"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403 and tracker.status(key) == RESOLVE


def test_web_review_form_has_cs_repro_choice(store):
    rid = triaged(store)
    page = web_client(store, FakeTracker(store.path)).get(f"/reports/{rid}").text
    assert 'name="repro"' in page and "QA 재현 필요" in page


def test_check_reports_statuses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(config.MAPPING_PATH))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "service.db")
    assert cli.main(["--check"]) == 0
    assert "BTS 상태: QA Review · In Dev · Resolve · Check · Closed 확인" in capsys.readouterr().out
    monkeypatch.setattr(FakeTracker, "check_statuses", lambda self: ["Jira 에 Check 상태가 없습니다"])
    assert cli.main(["--check"]) == 1
    assert "Check 상태가 없습니다" in capsys.readouterr().out
