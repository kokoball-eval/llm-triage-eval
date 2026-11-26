"""스프린트(목표 수정 버전) 시험 (S5-6a) — 규칙(sprint.py), BTS 표식 쓰기·읽기(녹화 응답), 등록·검토 화면·대시보드.

실제 BTS 는 부르지 않는다. Jira·Redmine 은 녹화 응답에 스프린트 표식을 넣어 읽기를 확인한다.
"""

from dataclasses import replace
from datetime import date

import pytest

from test_bts import adapter, an_issue
from test_milestone import RULES, fake, register, store, web   # noqa: F401 — store 는 픽스처
from test_service import DEV_ITEMS, triaged
from triage_service import review, sprint, sync
from triage_service.bts.jira import sprint_of
from triage_service.contracts import CLOSED

D = date.fromisoformat


# ── 규칙 ─────────────────────────────────────────────────

@pytest.mark.parametrize("found, expected", [
    ("2026-11-25", "2026-11-26"),   # 수요일 발견 → 다음 날 목요일 업데이트
    ("2026-11-26", "2026-12-03"),   # 목요일(업데이트 날) 발견 → 그날 배포에 못 넣으니 다음 주
    ("2026-11-27", "2026-12-03"),   # 금요일
    ("2026-11-23", "2026-11-26"),   # 월요일
])
def test_base_is_next_update_after_found(found, expected):
    assert sprint.base(D(found)).isoformat() == expected


def test_default_by_priority_and_urgent():
    found = D("2026-11-25")
    assert [sprint.default(found, p, today=found) for p in ("Critical", "Major", "Minor", "Trivial")] == \
        ["2026-11-26", "2026-12-03", "2026-12-10", "2026-12-17"]
    assert sprint.default(found, "Trivial", urgent=True, today=found) == "2026-11-26"   # 긴급 사인 요청은 우선순위와 상관없이 기준 업데이트
    assert sprint.default(found, "모르는 값", today=found) == "2026-12-17"              # 모르는 우선순위는 가장 늦게


def test_default_never_in_the_past():
    """독립 검토 반영 — 오래전에 받은 리포트를 늦게 검토해도 지난 업데이트를 기본값으로 두지 않는다."""
    assert sprint.default(D("2026-10-28"), "Critical", today=D("2026-11-25")) == "2026-11-26"
    assert sprint.default(D("2026-10-28"), "Trivial", today=D("2026-11-25")) == "2026-11-26"


def test_current_window_valid_label():
    assert sprint.current(D("2026-11-26")) == D("2026-11-26")   # 업데이트 날에는 오늘이 진행 중
    assert sprint.current(D("2026-11-27")) == D("2026-12-03")
    assert sprint.window(D("2026-11-25"), before=2, after=1) == ["2026-11-12", "2026-11-19", "2026-11-26", "2026-12-03"]
    assert sprint.valid("2026-11-26") and not sprint.valid("2026-11-25") and not sprint.valid("다음주") and not sprint.valid("")
    assert sprint.label("2026-11-26") == "11/26(목) 업데이트"


# ── BTS 표식 ─────────────────────────────────────────────

def test_jira_sprint_label_read():
    assert sprint_of(["팀_서버", "스프린트_2026-11-26", "스프린트_2026-12-03"]) == "2026-12-03"   # 둘 이상이면 가장 늦은 날짜
    assert sprint_of(["스프린트_2026-11-25", "스프린트_다음주"]) == "" and sprint_of([]) == ""       # 업데이트 요일이 아닌 값은 무시


def test_jira_create_and_read_sprint(store):
    tracker, rec = adapter("jira", store)
    tracker.create(replace(an_issue(store), sprint="2026-11-26"))
    assert "스프린트_2026-11-26" in rec.created["fields"]["labels"]
    rec.data["milestone"]["body"]["issues"][0]["fields"]["labels"].append("스프린트_2026-12-03")
    assert tracker.milestone_tickets("0.0.1")[0].sprint == "2026-12-03"


def test_redmine_create_and_read_sprint(store):
    tracker, rec = adapter("redmine", store)
    tracker.create(replace(an_issue(store), sprint="2026-11-26"))
    assert not any(f["value"] == "2026-11-26" for f in rec.created["issue"].get("custom_fields", []))   # 필드 번호가 없으면 넣지 않는다
    tracker.s["sprint_field_id"] = 9
    tracker.create(replace(an_issue(store), sprint="2026-11-26"))
    assert {"id": 9, "value": "2026-11-26"} in rec.created["issue"]["custom_fields"]
    rec.data["milestone"]["body"]["issues"][0]["custom_fields"] = [{"id": 9, "name": "스프린트", "value": "2026-12-03"}]
    assert tracker.milestone_tickets("0.0.1")[0].sprint == "2026-12-03"
    rec.data["milestone"]["body"]["issues"][0]["custom_fields"] = [{"id": 9, "name": "스프린트", "value": "2026-12-02"}]
    assert tracker.milestone_tickets("0.0.1")[0].sprint == ""   # 업데이트 요일이 아니면 읽지 않는다


# ── 등록 · 화면 ──────────────────────────────────────────

def test_register_rejects_bad_sprint_and_stores_good_one(store):
    tracker = fake(store)
    rid = triaged(store, DEV_ITEMS[1])
    values = {**store.get(rid)["model_fields"], "처리": "개발 배정"}
    with pytest.raises(ValueError, match="업데이트 요일"):
        review.register(store, tracker, rid, values, RULES, milestone="0.0.1", sprint_="2026-11-25")
    with pytest.raises(ValueError, match="이미 지난 업데이트"):
        review.register(store, tracker, rid, values, RULES, milestone="0.0.1", sprint_="2020-01-02")
    future = sprint.window(date.today())[1]
    rec = review.register(store, tracker, rid, values, RULES, milestone="0.0.1", sprint_=future)
    assert rec["assignment"]["sprint"] == future and tracker.get(rec["ticket_key"])["sprint"] == future
    sync.run(store, tracker)
    assert store.tickets("0.0.1")[0]["sprint"] == future   # 동기화가 마일스톤 티켓의 표식을 읽어 저장


def test_web_detail_offers_default_sprint_and_dashboard_counts_it(store):
    tracker = fake(store)
    client = web(store, tracker)
    rid = triaged(store, DEV_ITEMS[1])
    rec = store.get(rid)
    expected = sprint.default(D(rec["created_at"][:10]), rec["model_fields"]["우선순위"])
    page = client.get(f"/reports/{rid}").text
    assert f'<option value="{expected}" selected>' in page and "목표 스프린트" in page
    now = sprint.current(date.today()).isoformat()
    form = {**rec["model_fields"], "처리": "개발 배정", "milestone": "0.0.1", "sprint": now}
    form["모듈"] = rec["model_fields"]["모듈"].split(" / ")
    assert client.post(f"/reports/{rid}/register", data=form, follow_redirects=False).status_code in (302, 303)
    assert "목표 스프린트: " + sprint.label(now) in client.get(f"/reports/{rid}").text
    tracker.set_status(store.get(rid)["ticket_key"], CLOSED)
    client.post("/qa/sync")
    page = client.get("/dashboard").text
    assert f"<strong>{sprint.label(now)}</strong>" in page and "넘어온 이슈" in page   # 대시보드는 이월 분석
    assert sprint.label(now) in client.get("/").text and "(1 / 1건)" in client.get("/").text   # 스프린트 완료 현황은 홈
    bad = client.post(f"/reports/{triaged(store, DEV_ITEMS[2])}/register", data={**form, "sprint": "다음주"})
    assert bad.status_code == 422 and "업데이트 요일" in bad.text


def test_dashboard_ignores_unknown_sprint_param(store):
    tracker = fake(store)
    client = web(store, tracker)
    client.post("/qa/sync")
    page = client.get("/dashboard?sprint=2020-01-02").text
    assert f"<strong>{sprint.label(sprint.current(date.today()).isoformat())}</strong>" in page and "2회 이상 이월된 이슈는 없습니다" in page
