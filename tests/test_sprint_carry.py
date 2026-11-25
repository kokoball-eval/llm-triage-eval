"""스프린트 자동 이월 시험 (S5-6b) — sync.carry, 어댑터의 스프린트 쓰기, 이월 지표와 대시보드.

실제 BTS 는 부르지 않는다. 날짜는 시험 안에서 고정한다(오늘 = 2026-11-27 금요일, 지난 업데이트 11/19·11/26).
"""

import json
from datetime import date

import pytest

from test_bts import adapter
from test_milestone import RULES, fake, register, store, web   # noqa: F401 — store 는 픽스처
from triage_service import metrics, sync
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import CLOSED, TrackerError

D = date.fromisoformat
FRI = D("2026-11-27")   # 11/26 업데이트가 지난 첫 날


def planned(store, tracker, target="2026-11-19", i=1, milestone="0.0.1"):
    """개발 배정 티켓 하나를 만들어 목표 스프린트를 target(지난 업데이트)으로 맞추고 동기화로 읽어 둔다."""
    rec = register(store, tracker, 처리="개발 배정", i=i, milestone=milestone)
    tracker.update(rec["ticket_key"], sprint=target)
    sync.run(store, tracker)
    return rec["ticket_key"]


def ticket(store, key):
    return next(t for m in store.milestones() for t in store.tickets(m["name"]) if t["key"] == key)


def test_carry_moves_overdue_ticket_to_current_sprint_once(store):
    tracker = fake(store)
    key = planned(store, tracker)
    result = sync.carry(store, tracker, {"errors": []}, today=FRI)
    assert result["carried"] == [key] and not result["errors"]
    assert tracker.get(key)["sprint"] == "2026-12-03" and ticket(store, key)["sprint"] == "2026-12-03"
    assert tracker.get(key)["comments"][-1] == "[트리아지 서비스 · 스프린트 이월] 11/19(목) 업데이트 → 12/3(목) 업데이트 (이월 1회)"
    assert store.carries()[key][0][1:] == ("2026-11-19", "2026-12-03")
    assert sync.carry(store, tracker, {"errors": []}, today=FRI)["carried"] == []   # 이미 진행 중인 업데이트라 다시 옮기지 않는다


def test_carry_waits_for_update_day_and_skips_closed_and_future(store):
    tracker = fake(store)
    key = planned(store, tracker, target="2026-11-26")
    assert sync.carry(store, tracker, {"errors": []}, today=D("2026-11-26"))["carried"] == []   # 업데이트 날은 아직 진행 중
    assert sync.carry(store, tracker, {"errors": []}, today=D("2026-11-25"))["carried"] == []
    assert sync.carry(store, tracker, {"errors": []}, today=FRI)["carried"] == [key]            # 금요일부터
    closed = planned(store, tracker, i=2)
    tracker.set_status(closed, CLOSED)
    sync.run(store, tracker)
    assert sync.carry(store, tracker, {"errors": []}, today=D("2026-12-04"))["carried"] == [key]   # Closed 는 옮기지 않는다


def test_carry_includes_check_and_goes_straight_to_current_after_weeks(store):
    tracker = fake(store)
    key = planned(store, tracker, target="2026-10-29")      # 4주 밀림
    tracker.update(key, status="Check", comment="보류")
    sync.run(store, tracker)
    assert sync.carry(store, tracker, {"errors": []}, today=FRI)["carried"] == [key]
    assert tracker.get(key)["sprint"] == "2026-12-03" and len(store.carries()[key]) == 1   # 지난 업데이트를 하나씩 거치지 않는다


def test_carry_rechecks_live_status_and_skips_released_milestones(store):
    tracker = fake(store)
    key = planned(store, tracker)
    original = FakeTracker.status
    FakeTracker.status = lambda self, k: CLOSED   # 읽어 둔 뒤 BTS 에서 닫혔다
    try:
        assert sync.carry(store, tracker, {"errors": []}, today=FRI)["carried"] == []
    finally:
        FakeTracker.status = original
    tracker.add_version("0.0.1", "2027-01-13", released=True)
    store.save_milestones(tracker.versions(), "t1")
    assert sync.carry(store, tracker, {"errors": []}, today=FRI)["carried"] == []   # 출시한 마일스톤은 옮기지 않는다
    assert tracker.get(key)["sprint"] == "2026-11-19"


def test_carry_failure_is_retried_and_comment_failure_keeps_count(store, monkeypatch):
    tracker = fake(store)
    key = planned(store, tracker)
    original = FakeTracker.update

    def broken(self, *a, **k):
        raise TrackerError("BTS 쓰기 거부")
    monkeypatch.setattr(FakeTracker, "update", broken)
    result = sync.carry(store, tracker, {"errors": []}, today=FRI)
    assert result["carried"] == [] and any("스프린트 이월" in e and "BTS 쓰기 거부" in e for e in result["errors"]) and not store.carries()
    monkeypatch.setattr(FakeTracker, "update", original)   # 다음 동기화에서 다시 시도한다
    assert sync.carry(store, tracker, {"errors": []}, today=FRI)["carried"] == [key]
    # 표식은 옮겨졌는데 코멘트만 실패하면 이월 기록은 남는다
    key2 = planned(store, tracker, i=2)

    def comment_fails(self, key, status="", team="", comment="", assignee="", sprint=""):
        if comment:
            raise TrackerError("코멘트 거부")
        return original(self, key, status, team, comment, assignee, sprint)
    monkeypatch.setattr(FakeTracker, "update", comment_fails)
    result = sync.carry(store, tracker, {"errors": []}, today=FRI)
    assert result["carried"] == [key2] and len(store.carries()[key2]) == 1 and any("코멘트 거부" in e for e in result["errors"])   # 표식은 옮겼으니 센다


def test_carry_ignores_stale_read_right_after_carrying(store):
    """독립 검토 반영 — Jira 색인이 늦어 옛 스프린트가 다시 읽혀도 방금 옮긴 티켓을 또 옮기지 않는다."""
    from triage_service.store import connect
    tracker = fake(store)
    key = planned(store, tracker)
    sync.carry(store, tracker, {"errors": []}, today=FRI)
    with connect(store.path) as c:   # 동기화가 옛 값을 읽어 저장한 것처럼
        c.execute("UPDATE bts_tickets SET sprint = '2026-11-19' WHERE key = ?", (key,))
    assert sync.carry(store, tracker, {"errors": []}, today=FRI)["carried"] == [] and len(store.carries()[key]) == 1
    assert tracker.get(key)["comments"].count(tracker.get(key)["comments"][-1]) == 1


def test_second_carry_counts_and_metrics_show_repeat(store):
    tracker = fake(store)
    key = planned(store, tracker)
    sync.carry(store, tracker, {"errors": []}, today=FRI)
    tracker.update(key, sprint="2026-12-03")                              # 12/3 에도 못 닫음
    sync.run(store, tracker)
    sync.carry(store, tracker, {"errors": []}, today=D("2026-12-04"))
    assert tracker.get(key)["comments"][-1].endswith("(이월 2회)") and tracker.get(key)["sprint"] == "2026-12-10"
    tickets = [t for m in store.milestones() for t in store.tickets(m["name"])]
    sp = metrics.sprint(tickets, "2026-12-10", store.carries())
    assert (sp["total"], sp["carried_in"]) == (1, 1) and [(r["key"], r["n"]) for r in sp["repeat"]] == [(key, 2)]
    assert metrics.sprint(tickets, "2026-12-03", store.carries())["carried_in"] == 0   # 그 스프린트로 넘어온 것만 센다


def test_syncer_runs_carry_and_dashboard_shows_it(store, monkeypatch):
    tracker = fake(store)
    client = web(store, tracker)
    key = planned(store, tracker)
    monkeypatch.setattr(sync, "date", type("D", (date,), {"today": staticmethod(lambda: FRI)}))   # 오늘 = 금요일
    page = client.post("/qa/sync").text
    assert "진행 중인 스프린트로 옮겼습니다" in page and key in page
    assert tracker.get(key)["sprint"] == "2026-12-03"


# ── 어댑터 ───────────────────────────────────────────────

def test_jira_update_sprint_replaces_old_labels(store):
    tracker, rec = adapter("jira", store)
    rec.data["status"]["body"]["fields"]["labels"] = ["팀_서버", "스프린트_2026-11-19", "스프린트_2026-11-12"]
    tracker.update(rec.data["ticket_key"], sprint="2026-12-03", comment="이월")
    puts = [json.loads(r.content) for r in rec.requests if r.method == "PUT"]
    labels = puts[0]["update"]["labels"]
    assert {"remove": "스프린트_2026-11-19"} in labels and {"remove": "스프린트_2026-11-12"} in labels
    assert {"add": "스프린트_2026-12-03"} in labels and {"remove": "팀_서버"} not in labels
    assert [r.method for r in rec.requests] == ["GET", "PUT", "POST"]   # 라벨 읽기 → 라벨 쓰기 → 코멘트


def test_jira_update_sprint_already_set_adds_nothing(store):
    tracker, rec = adapter("jira", store)
    rec.data["status"]["body"]["fields"]["labels"] = ["스프린트_2026-12-03"]
    tracker.update(rec.data["ticket_key"], sprint="2026-12-03")
    assert not any(r.method == "PUT" and json.loads(r.content).get("update") for r in rec.requests)


def test_redmine_update_sprint_sets_custom_field(store):
    tracker, rec = adapter("redmine", store)
    tracker.update(rec.data["ticket_key"], sprint="2026-12-03")
    assert not any(r.method == "PUT" and "custom_fields" in json.loads(r.content)["issue"] for r in rec.requests)   # 필드 번호가 없으면 쓰지 않는다
    tracker.s["sprint_field_id"] = 9
    tracker.update(rec.data["ticket_key"], sprint="2026-12-03")
    put = next(json.loads(r.content) for r in reversed(rec.requests) if r.method == "PUT")
    assert put["issue"]["custom_fields"] == [{"id": 9, "value": "2026-12-03"}]
