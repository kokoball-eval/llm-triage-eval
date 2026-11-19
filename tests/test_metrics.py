"""대시보드 지표(metrics.py)와 상태 이력 시험 — 시각을 정해 둔 가짜 시계로, 손으로 계산할 수 있는 값을 고정한다."""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from test_service import DEV_ITEMS, report_from_item
from triage_service import flow, metrics
from triage_service import store as store_mod
from triage_service.bts.fake import FakeTracker
from triage_service.store import Store
from triage_service.web.app import create_app

NOW = datetime(2026, 11, 19, 12, 0)


class Clock:
    def __init__(self):
        self.t = "2026-11-19T00:00:00"

    def at(self, text: str):
        self.t = text
        return self

    def __call__(self) -> str:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(store_mod, "now", c)
    return c


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "service.db")


FIELDS = {"요약": "요약", "분류": "결함", "모듈": "결제·재화 / UI·텍스트", "우선순위": "Major", "재현 정보": "충분",
          "발생 빈도": "항상", "처리": "등록(재현 대기)", "누락 정보 및 권장 조치": "없음"}


def through_review(store, clock, day, start, review_at, elapsed, fields=FIELDS):
    clock.at(f"{day}T{start}")
    rid = store.add(report_from_item(DEV_ITEMS[0]))
    store.move(rid, flow.TRIAGING)
    clock.at(f"{day}T{review_at}")
    store.move(rid, flow.REVIEW, result={"success": True, "elapsed_sec": elapsed}, model_fields=dict(fields))
    return rid


@pytest.fixture
def scenario(store, clock):
    # r1: 모델 Major → 사람이 Critical 로 올려 등록 (검토 대기 30분, 접수→등록 31분, 트리아지 6초)
    r1 = through_review(store, clock, "2026-11-19", "09:00:00", "09:01:00", 6.0)
    clock.at("2026-11-19T09:31:00")
    final = {**FIELDS, "우선순위": "Critical", "처리": "긴급 사인 요청"}
    store.move(r1, flow.REGISTERED, final_fields=final,
               edits=[{"field": "우선순위", "before": "Major", "after": "Critical"},
                      {"field": "처리", "before": "등록(재현 대기)", "after": "긴급 사인 요청"}])
    # r2: 고치지 않고 등록 (검토 대기 10분, 접수→등록 12분, 트리아지 8초)
    r2 = through_review(store, clock, "2026-11-19", "10:00:00", "10:02:00", 8.0)
    clock.at("2026-11-19T10:12:00")
    store.move(r2, flow.REGISTERED, final_fields=dict(FIELDS), edits=[])
    # r3: 트리아지 실패 → 다시 시도 → 검토 대기 → 폐기 (검토 대기 10분, 트리아지 4초)
    clock.at("2026-11-18T08:00:00")
    r3 = store.add(report_from_item(DEV_ITEMS[1]))
    store.move(r3, flow.TRIAGING)
    clock.at("2026-11-18T08:01:00")
    store.move(r3, flow.FAILED, failed_step="트리아지", error="x")
    clock.at("2026-11-18T08:05:00")
    store.move(r3, flow.RECEIVED, error=None, failed_step=None)
    store.move(r3, flow.TRIAGING)
    clock.at("2026-11-18T08:06:00")
    store.move(r3, flow.REVIEW, result={"success": True, "elapsed_sec": 4.0}, model_fields=dict(FIELDS))
    clock.at("2026-11-18T08:16:00")
    store.move(r3, flow.DISCARDED)
    # r4: 7일 밖, 트리아지 실패로 남음
    clock.at("2026-11-01T08:00:00")
    r4 = store.add(report_from_item(DEV_ITEMS[2]))
    store.move(r4, flow.TRIAGING)
    store.move(r4, flow.FAILED, failed_step="트리아지", error="y")
    return store


def test_last_7_days(scenario):
    m = metrics.compute(scenario.list(), scenario.history(), "7", now=NOW)
    assert m["total"] == 3 and m["since"] == "2026-11-13"
    assert (m["states"][flow.REGISTERED], m["states"][flow.DISCARDED], m["states"][flow.FAILED]) == (2, 1, 0)
    assert m["edited"] == {"n": 1, "d": 2, "pct": 50.0}
    assert m["edited_by_field"]["우선순위"] == {"n": 1, "d": 2, "pct": 50.0}
    assert m["edited_by_field"]["모듈"] == {"n": 0, "d": 2, "pct": 0.0}
    assert m["critical_up"] == {"n": 1, "d": 2, "pct": 50.0} and m["critical_down"]["n"] == 0
    assert m["auto"] == {"n": 0, "d": 3, "pct": 0.0}
    t = m["times"]
    assert (t["triage_sec"]["p50"], t["triage_sec"]["p95"]) == (6.0, 8.0)       # [4, 6, 8]
    assert (t["review_min"]["p50"], t["review_min"]["p95"]) == (10.0, 30.0)     # [10, 10, 30]
    assert (t["total_min"]["p50"], t["total_min"]["p95"]) == (12.0, 31.0)       # [12, 31]
    assert (t["no_receipt"], t["waiting_now"]) == (0, 0)
    assert m["failures"]["events"] == 1 and m["failures"]["now_triage"] == 0
    days = {d: n for d, n, _sources in m["intake_rows"]}
    assert len(days) == 7 and (days["11-19"], days["11-18"]) == (2, 1)
    assert dict(m["distribution"]["우선순위"]) == {"Critical": 1, "Major": 1}
    assert dict(m["distribution"]["모듈"]) == {"결제·재화": 2, "UI·텍스트": 2}
    assert [k for k, _ in m["distribution"]["우선순위"]] == ["Critical", "Major"]   # 허용 값 순서


def test_all_period_and_failures(scenario):
    m = metrics.compute(scenario.list(), scenario.history(), "all", now=NOW)
    assert m["total"] == 4 and m["since"] is None
    assert m["failures"]["rate"] == {"n": 1, "d": 4, "pct": 25.0} and m["failures"]["now_triage"] == 1
    assert m["failures"]["events"] == 2
    assert m["intake_rows"][0][0] == "11-01" and len(m["intake_rows"]) == 19


def test_empty_store_has_no_division_by_zero(store):
    m = metrics.compute(store.list(), store.history(), "30", now=NOW)
    assert m["total"] == 0 and m["edited"]["pct"] is None and m["times"]["review_min"]["p50"] is None
    assert len(m["intake_rows"]) == 30


def test_unknown_period_falls_back_to_30_days():
    assert metrics.compute([], {}, "999", now=NOW)["period"] == "30"


def test_percentile_nearest_rank():
    assert metrics.percentile([5], 95) == 5
    assert metrics.percentile(list(range(1, 21)), 95) == 19 and metrics.percentile(list(range(1, 21)), 50) == 10


def test_reports_without_receipt_history_are_counted_not_timed(scenario):
    """이력 표가 생기기 전에 접수한 리포트 — 접수 이력 없이 중간부터 이력이 있어도 접수→등록 시간에서 빠지고 건수로 보인다."""
    with store_mod.connect(scenario.path) as c:
        c.execute("DELETE FROM transitions WHERE report_id = 1 AND src IS NULL")
    m = metrics.compute(scenario.list(), scenario.history(), "7", now=NOW)
    assert m["times"]["no_receipt"] == 1 and m["times"]["total_min"]["n"] == 1
    assert m["times"]["review_min"]["n"] == 3          # 검토 대기 이력은 남아 있어 그대로 센다


def test_review_wait_sums_stints_per_report(store, clock):
    """BTS 등록 실패 → 검토로 되돌리기: 검토 대기에 두 번 머문 시간(30분 + 5분)을 합쳐 한 건으로 센다. 실패에 있던 30분은 뺀다."""
    rid = through_review(store, clock, "2026-11-19", "09:00:00", "09:00:00", 5.0)
    clock.at("2026-11-19T09:30:00")
    store.move(rid, flow.FAILED, failed_step="등록", error="BTS 점검 중")
    clock.at("2026-11-19T10:00:00")
    store.move(rid, flow.REVIEW)
    clock.at("2026-11-19T10:05:00")
    store.move(rid, flow.REGISTERED, final_fields=dict(FIELDS), edits=[])
    waiting = through_review(store, clock, "2026-11-19", "11:00:00", "11:00:00", 5.0)   # 아직 검토 대기
    m = metrics.compute(store.list(), store.history(), "7", now=NOW)
    assert m["times"]["review_min"] == {"n": 1, "p50": 35.0, "p95": 35.0}
    assert m["times"]["waiting_now"] == 1 and store.get(waiting)["state"] == flow.REVIEW


# ── 상태 이력 ────────────────────────────────────────────

def test_every_transition_is_recorded(store, clock):
    rid = through_review(store, clock, "2026-11-19", "09:00:00", "09:01:00", 6.0)
    assert store.history()[rid] == [("2026-11-19T09:00:00", None, flow.RECEIVED),
                                    ("2026-11-19T09:00:00", flow.RECEIVED, flow.TRIAGING),
                                    ("2026-11-19T09:01:00", flow.TRIAGING, flow.REVIEW)]


def test_refused_transition_leaves_no_history(store, clock):
    rid = through_review(store, clock, "2026-11-19", "09:00:00", "09:01:00", 6.0)
    with pytest.raises(flow.FlowError):
        store.move(rid, flow.RECEIVED)
    assert len(store.history()[rid]) == 3


# ── 화면 ─────────────────────────────────────────────────

def test_dashboard_page(scenario):
    app = create_app(scenario, tracker=FakeTracker(scenario.path), start_worker=False,
                     allowed_hosts=("testserver",), sources=[])
    with TestClient(app) as c:
        page = c.get("/dashboard?period=all").text
        assert "사람 수정 비율" in page and "<strong>50.0%</strong> <span class=\"muted\">(1 / 2건)</span>" in page
        assert "Critical 로 <strong>올림</strong>: <strong>50.0%</strong>" in page
        assert "현재 실패 상태: <strong>25.0%</strong> <span class=\"muted\">(1 / 4건)</span>" in page
        assert "11-19 <span class=\"muted\">(paste 2)</span>" in page          # 일자별 유입의 출처별 건수
        assert c.get("/dashboard?period=7").status_code == 200


def test_dashboard_on_empty_store(store):
    app = create_app(store, tracker=FakeTracker(store.path), start_worker=False, allowed_hosts=("testserver",),
                     sources=[])
    with TestClient(app) as c:
        page = c.get("/dashboard").text
        assert "데이터 없음" in page and c.get("/").status_code == 200
