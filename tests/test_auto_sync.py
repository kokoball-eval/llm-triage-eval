"""화면을 열 때 자동 동기화와 BTS 연결 표시 시험 (S5-4d) — sync.Syncer 와 홈·내 업무·대시보드.

실제 BTS 는 부르지 않는다(가짜 BTS). conftest 가 끈 자동 동기화를 이 파일에서만 켠다.
"""

import threading
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_milestone import RULES, fake, register
from triage_service import config, sync
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import TrackerError
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTO_SYNC_MINUTES", 10)
    return Store(tmp_path / "service.db")


class Counting(FakeTracker):
    calls = 0

    def versions(self):
        Counting.calls += 1
        return super().versions()


def wait(syncer):
    if syncer.thread:
        syncer.thread.join(5)


def test_due_after_interval(store, monkeypatch):
    s = sync.Syncer(store, FakeTracker(store.path))
    assert s.due()                                   # 서버를 켠 뒤 처음
    s.now()
    assert not s.due()                               # 방금 읽음
    assert s.due(datetime.now() + timedelta(minutes=10))
    monkeypatch.setattr(config, "AUTO_SYNC_MINUTES", None)
    assert not s.due(datetime.now() + timedelta(days=1))   # None 이면 끈다


def test_start_runs_in_background_once(store):
    tracker = Counting(store.path)
    Counting.calls = 0
    s = sync.Syncer(store, tracker)
    s.start()
    wait(s)
    s.start()                                        # 10분 안 — 다시 읽지 않는다
    wait(s)
    assert Counting.calls == 1 and s.status()["state"] == "ok"


def test_start_does_nothing_while_another_sync_runs(store):
    s = sync.Syncer(store, FakeTracker(store.path))
    with s.lock:                                     # 다른 동기화가 도는 중
        s.start()
        assert s.thread is None and s.status()["state"] == "running"


def test_button_waits_for_running_sync(store, monkeypatch):
    """설계 의도 11 — 겹치지 않는다: [지금 동기화]는 자동 동기화가 끝난 뒤에 돈다."""
    gate, order = threading.Event(), []
    real = sync.run

    def slow(st, tr):
        order.append("시작")
        gate.wait(5)
        out = real(st, tr)
        order.append("끝")
        return out
    monkeypatch.setattr(sync, "run", slow)
    s = sync.Syncer(store, FakeTracker(store.path))
    s.start()
    button = threading.Thread(target=s.now)
    button.start()
    gate.set()
    wait(s)
    button.join(5)
    assert order == ["시작", "끝", "시작", "끝"]


class Down(FakeTracker):
    def snapshot(self, keys):
        raise TrackerError("연결 거부")

    def versions(self):
        raise TrackerError("연결 거부")


def test_status_ok_partial_down(store, monkeypatch):
    assert sync.Syncer(store, FakeTracker(store.path)).status() == {"bts": "fake", "state": "never", "ago": "", "errors": []}
    tracker = fake(store)
    register(store, tracker)
    s = sync.Syncer(store, tracker)
    s.now()
    assert s.status()["state"] == "ok" and s.status()["ago"] == "방금"
    s = sync.Syncer(store, Down(store.path))
    s.now()
    assert s.status()["state"] == "down" and "연결 거부" in s.status()["errors"][0]
    monkeypatch.setattr(FakeTracker, "versions", Down.versions)   # 상태는 읽고 버전 목록만 실패
    s = sync.Syncer(store, tracker)
    s.now()
    assert s.status()["state"] == "partial"


def test_unexpected_error_is_shown_not_raised(store, monkeypatch):
    def broken(st, tr):
        raise OSError("디스크 가득 참")
    monkeypatch.setattr(sync, "run", broken)
    s = sync.Syncer(store, FakeTracker(store.path))
    s.start()
    wait(s)
    assert s.status()["state"] == "down" and "디스크 가득 참" in s.status()["errors"][0]
    assert not s.lock.locked()                       # 실패해도 잠금이 풀린다
    assert not s.due()                               # 실패도 시도로 쳐서 화면을 열 때마다 부르지 않는다


# ── 화면 ─────────────────────────────────────────────────

def web(store, tracker):
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=RULES)
    return app, TestClient(app)


def test_web_opening_screen_starts_sync_and_shows_status(store):
    tracker = fake(store, saved=False)
    app, client = web(store, tracker)
    assert "가짜 BTS 확인 전" in client.get("/reports").text   # 리포트 목록은 동기화를 시작하지 않는다
    assert app.state.syncer.thread is None
    client.get("/")
    wait(app.state.syncer)
    page = client.get("/").text
    assert "가짜 BTS 연결됨 · 방금 전" in page and "마일스톤 0.0.1" in page   # 뒤에서 읽은 마일스톤
    assert 'http-equiv="refresh"' not in page


@pytest.mark.parametrize("path", ["/", "/mine", "/dashboard"])
def test_web_refreshes_while_sync_runs(store, path, monkeypatch):
    gate = threading.Event()
    real = sync.run
    monkeypatch.setattr(sync, "run", lambda st, tr: (gate.wait(5), real(st, tr))[1])
    app, client = web(store, FakeTracker(store.path))
    page = client.get(path).text
    assert "가짜 BTS 읽는 중" in page and 'http-equiv="refresh"' in page   # 화면은 기다리지 않고 열린다
    assert 'http-equiv="refresh"' not in client.get("/reports").text       # 자동 동기화 화면에서만 다시 읽는다
    gate.set()
    wait(app.state.syncer)


def test_web_home_shows_last_auto_sync_errors(store):
    app, client = web(store, Down(store.path))
    client.get("/")
    wait(app.state.syncer)
    page = client.get("/").text
    assert "가짜 BTS 연결 안 됨" in page and "읽지 못했습니다 — 마일스톤 목록: 연결 거부" in page
    assert 'title="마일스톤 목록: 연결 거부"' in page                     # 연결 표시에 이유


def test_qa_action_waits_for_running_sync(store, monkeypatch):
    """독립 검토 반영 — 동기화가 읽어 둔 옛 상태가 QA 동작의 새 상태를 덮지 않게, QA 동작은 동기화가 끝난 뒤에 한다."""
    from triage_service import qa
    gate, order = threading.Event(), []
    real = sync.run
    monkeypatch.setattr(sync, "run", lambda st, tr: (order.append("동기화"), gate.wait(5), real(st, tr), order.append("동기화 끝"))[2])
    monkeypatch.setattr(qa, "act", lambda *a: order.append("QA 동작") or {"ok": True, "after": None, "error": None})
    tracker = fake(store)
    rid = register(store, tracker)["id"]
    app, client = web(store, tracker)
    client.get("/")                                   # 뒤에서 동기화 시작
    action = threading.Thread(target=client.post, args=(f"/reports/{rid}/qa",), kwargs={"data": {"action": "comment", "comment": "확인"}})
    action.start()
    action.join(0.3)
    assert order == ["동기화"]                        # QA 동작은 기다린다
    gate.set()
    action.join(5)
    wait(app.state.syncer)
    assert order == ["동기화", "동기화 끝", "QA 동작"]


def test_auto_sync_zero_minutes_is_off(store, monkeypatch):
    monkeypatch.setattr(config, "AUTO_SYNC_MINUTES", 0)
    assert not sync.Syncer(store, FakeTracker(store.path)).due()
