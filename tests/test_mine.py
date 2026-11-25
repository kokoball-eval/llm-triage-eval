"""내 담당 화면 시험 (S5-5b) — sync.mine 의 분류·병목 계산과 /mine 화면, 동기화가 기억하는 "나".

실제 BTS 는 부르지 않는다(가짜 BTS).
"""

from datetime import datetime
from types import SimpleNamespace

from test_milestone import RULES, fake, register, store, web   # noqa: F401 — store 는 픽스처
from triage_service import sync
from triage_service.contracts import CHECK, CLOSED, IN_DEV, QA_REVIEW, RESOLVE, Member

ME = Member("u1", "경민")
AT = datetime.fromisoformat("2026-11-25T12:00:00").astimezone()


def tk(key, status, assignee="", updated="2026-11-25T11:00:00", team=""):
    return {"key": key, "title": f"제목 {key}", "status": status, "assignee": assignee, "updated": updated, "team": team}


def rec(rid, key, status, assignee="", owner=None, updated="2026-11-25T11:00:00"):
    return {"id": rid, "ticket_key": key, "report": SimpleNamespace(title=f"리포트 {key}"), "assignment": {"team": "QA", "qa_owner": owner},
            "bts": {"status": status, "assignee": assignee, "updated": updated, "synced_at": updated}}


def test_mine_classifies_turn_and_handed():
    me = {"id": "u1", "name": "경민"}
    records = [rec(1, "A", QA_REVIEW, "경민"),                      # 내 차례: QA Review 담당자가 나
               rec(2, "B", QA_REVIEW, "박대무"),                     # 남의 차례
               rec(3, "C", RESOLVE, "서버담당", owner=me),            # 내 차례: 개발에 갔다 돌아온 건은 QA 담당이 나
               rec(4, "D", RESOLVE, "경민", owner={"id": "u2", "name": "박대무"}),   # BTS 담당자는 나여도 QA 담당은 남
               rec(5, "E", IN_DEV, "서버담당", owner=me),             # 넘긴 이슈
               rec(6, "F", CHECK, "경민", owner=me),                 # 넘긴 이슈(보류)
               rec(7, "G", CLOSED, "경민", owner=me)]                # 끝난 건은 없다
    tickets = [tk("H", RESOLVE, "경민"),                            # 서비스를 거치지 않음 → BTS 담당자가 나이면 내 차례
               tk("I", IN_DEV, "경민"),                             # 서비스를 거치지 않은 In Dev 는 넘긴 이슈가 아니다
               tk("A", QA_REVIEW, "경민", team="서버")]              # 리포트와 같은 키면 리포트 쪽이 이긴다
    m = sync.mine(records, tickets, {}, {}, ME, AT)
    assert [r["key"] for r in m["mine"]] == ["A", "C", "H"] or sorted(r["key"] for r in m["mine"]) == ["A", "C", "H"]
    assert sorted(r["key"] for r in m["handed"]) == ["E", "F"]
    assert {r["key"]: r["title"] for r in m["mine"]}["A"] == "리포트 A" and m["me"] == "경민"


def test_mine_days_and_bottleneck():
    records = [rec(1, "A", QA_REVIEW, "경민", updated="2026-11-20T12:00:00"),   # BTS 변경 시각은 5일 전
               rec(2, "B", RESOLVE, "경민", updated="2026-11-25T00:00:00")]    # 12시간
    changes = {"A": [("2026-11-24T12:00:00", None, QA_REVIEW)]}               # 변화 기록이 있으면 그 시각(1일 전)이 이긴다
    m = sync.mine(records, [], changes, {}, ME, AT)
    a, b = {r["key"]: r for r in m["mine"]}["A"], {r["key"]: r for r in m["mine"]}["B"]
    assert (a["days"], a["late"], b["days"], b["late"]) == (1.0, False, 0.5, False)
    changes = {"A": [("2026-11-22T12:00:00", None, QA_REVIEW)]}                 # 3일 > 기준 2일
    m = sync.mine(records, [], changes, {}, ME, AT)
    assert [r["key"] for r in m["late"]] == ["A"] and m["mine"][0]["key"] == "A"   # 오래 머문 순


def test_mine_bad_time_does_not_break():
    m = sync.mine([rec(1, "A", QA_REVIEW, "경민", updated="")], [], {}, {}, ME, AT)
    assert m["mine"][0]["days"] == 0 and not m["late"]


def test_syncer_remembers_me(store):
    tracker = fake(store)
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    syncer.now()
    assert syncer.me == Member("QA팀장", "QA팀장")          # 가짜 BTS 의 토큰 주인
    assert sync.Syncer(store, tracker).me is None           # 자동 배정 설정이 없으면 알 수 없다


def test_syncer_me_none_when_not_qa_member(store):
    tracker = fake(store)
    tracker.me = lambda: "누군가"
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    syncer.now()
    assert syncer.me is None


def test_syncer_keeps_old_me_when_read_fails(store):
    tracker = fake(store)
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    syncer.now()

    def boom():
        raise RuntimeError("BTS 끊김")
    tracker.me = boom
    result = syncer.now()
    assert syncer.me is not None and syncer.me_error == "BTS 끊김" and not result["errors"]   # 연결 표시는 건드리지 않는다


def test_web_mine(store):
    tracker = fake(store)
    client = web(store, tracker)
    assert "내 계정을 읽는 중" in client.get("/mine").text              # 동기화 전
    key = register(store, tracker)["ticket_key"]                     # 재현 대기 → QA팀 구성원(미해결이 같으면 첫 사람 = 팀장)
    client.post("/qa/sync")
    page = client.get("/mine").text
    assert "내 담당" in page and key in page and "내 차례 1건" in page
    tracker.set_status(key, IN_DEV)
    client.post("/qa/sync")
    page = client.get("/mine").text
    assert "내 차례 0건 · 넘긴 이슈 0건" in page or "넘긴 이슈 1건" in page   # QA Review 에서 In Dev 로 갔으나 QA 담당은 나
    assert "지금 내가 처리할 이슈가 없습니다" in page


def test_web_mine_without_rules(store):
    from fastapi.testclient import TestClient
    from test_service import FakeClient
    from triage_service.web.app import create_app
    from triage_service.worker import Worker
    app = create_app(store, Worker(store, client_factory=FakeClient), fake(store), start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=None)
    assert "자동 배정 설정" in TestClient(app).get("/mine").text
