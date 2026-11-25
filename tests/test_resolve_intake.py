"""Resolve 접수 시험 (S5-5b 보강) — 바로 개발 배정된(QA 담당 없는) 건이 Resolve 가 되면 동기화가 QA팀장에게 넘긴다.

실제 BTS 는 부르지 않는다(가짜 BTS). sync 설계 의도 14.
"""

from test_assign import cli, config
from test_milestone import RULES, fake, register, store, web   # noqa: F401 — store 는 픽스처
from triage_service import qa, sync
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import IN_DEV, RESOLVE, Member


def dev_ticket(store, tracker, i=1):
    rec = register(store, tracker, 처리="개발 배정", i=i)
    assert not rec["assignment"]["qa_owner"] and rec["assignment"]["team"] != "QA"   # 바로 개발로 갔다
    return rec


def intake_log(store, rid):
    return [a for a in store.qa_log().get(rid, []) if a["action"] == qa.INTAKE]


def test_resolve_of_dev_ticket_goes_to_qa_lead(store):
    tracker = fake(store)
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    rec = dev_ticket(store, tracker)
    key = rec["ticket_key"]
    assert syncer.now()["intake"] == []                    # 아직 In Dev
    tracker.set_status(key, RESOLVE)
    result = syncer.now()
    assert result["intake"] == [key] and not result["errors"]
    assert tracker.get(key)["assignee"] == "QA팀장" and tracker.get(key)["comments"][-1].startswith("[트리아지 서비스 · 회귀 접수]")
    assert [a["error"] for a in intake_log(store, rec["id"])] == [None]
    assert store.get(rec["id"])["bts"]["assignee"] == "QA팀장"
    # 한 번만 — 팀장이 BTS 에서 다른 QA 구성원에게 넘겨도 되돌리지 않는다
    tracker.update(key, assignee="QA팀원")
    assert syncer.now()["intake"] == [] and tracker.get(key)["assignee"] == "QA팀원"
    assert len(intake_log(store, rec["id"])) == 1


def test_resolve_with_qa_owner_is_left_alone(store):
    tracker = fake(store)
    rec = register(store, tracker, 처리="등록(재현 대기)")      # QA팀으로 감 → qa_owner 있음
    assert rec["assignment"]["qa_owner"]
    tracker.set_status(rec["ticket_key"], RESOLVE)
    assert sync.Syncer(store, tracker, RULES["qa"]).now()["intake"] == []
    assert not intake_log(store, rec["id"])


def test_intake_failure_is_retried_and_shown(store, monkeypatch):
    tracker = fake(store)
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    key = dev_ticket(store, tracker)["ticket_key"]
    tracker.set_status(key, RESOLVE)
    original = FakeTracker.update

    def broken(self, *a, **k):
        raise RuntimeError("BTS 쓰기 거부")
    monkeypatch.setattr(FakeTracker, "update", broken)
    result = syncer.now()
    assert result["intake"] == [] and any("회귀 접수" in e and "BTS 쓰기 거부" in e for e in result["errors"])
    monkeypatch.setattr(FakeTracker, "update", original)   # 다음 동기화에서 다시 시도한다
    assert syncer.now()["intake"] == [key]


def test_intake_does_nothing_without_lead_or_qa_team(store, monkeypatch):
    tracker = fake(store)
    key = dev_ticket(store, tracker)["ticket_key"]
    tracker.set_status(key, RESOLVE)
    assert sync.Syncer(store, tracker).now().get("intake") is None     # 자동 배정 설정이 없으면 하지 않는다
    monkeypatch.setattr(FakeTracker, "lead", lambda self, team: "")
    assert sync.Syncer(store, tracker, RULES["qa"]).now()["intake"] == []
    assert tracker.get(key)["assignee"] != "QA팀장"


def test_lead_sees_returned_ticket_on_mine_screen(store):
    tracker = fake(store)
    client = web(store, tracker)
    key = dev_ticket(store, tracker)["ticket_key"]
    client.post("/qa/sync")
    assert key not in client.get("/mine").text            # In Dev — 내 차례가 아니다
    tracker.set_status(key, RESOLVE)
    page = client.post("/qa/sync").text
    assert "QA팀장에게 회귀 테스트를 배정했습니다" in page and key in page
    assert "내 차례 1건" in client.get("/mine").text      # 토큰 주인(QA팀장)의 내 차례


def test_check_warns_about_same_display_name(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(config.MAPPING_PATH))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "service.db")
    monkeypatch.setattr(FakeTracker, "members", lambda self, team: [Member("a", "경민"), Member("b", "경민"), Member("c", "박대무")])
    assert cli.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "표시 이름이 같은 구성원" in out and "경민" in out and "박대무" not in out.split("경고")[1].split("—")[0]


def test_qa_team_ticket_without_owner_record_is_skipped(store):
    """독립 검토 반영 — S5-5a 이전 기록처럼 QA팀으로 갔는데 qa_owner 가 없는 건은 개발 배정이 아니다."""
    import json
    from triage_service.store import connect
    tracker = fake(store)
    rec = register(store, tracker, 처리="등록(재현 대기)")
    with connect(store.path) as c:
        c.execute("UPDATE reports SET assignment = ? WHERE id = ?",
                  (json.dumps({**rec["assignment"], "qa_owner": None}, ensure_ascii=False), rec["id"]))
    tracker.set_status(rec["ticket_key"], RESOLVE)
    assert sync.Syncer(store, tracker, RULES["qa"]).now()["intake"] == []


def test_intake_rechecks_live_status(store, monkeypatch):
    """독립 검토 반영 — 읽어 둔 상태는 오래됐을 수 있어 쓰기 직전에 BTS 상태를 다시 본다."""
    tracker = fake(store)
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    key = dev_ticket(store, tracker)["ticket_key"]
    tracker.set_status(key, RESOLVE)
    monkeypatch.setattr(FakeTracker, "status", lambda self, k: IN_DEV)   # 그 사이 개발로 되돌아갔다
    assert syncer.now()["intake"] == [] and tracker.get(key)["assignee"] != "QA팀장"
