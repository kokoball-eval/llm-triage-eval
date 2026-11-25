"""Resolve 접수 시험 (S5-5b 보강) — 바로 개발 배정된(QA 담당 없는) 건이 Resolve 가 되면 동기화가 QA 구성원에게 넘긴다.

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


def test_resolve_of_dev_ticket_goes_to_least_busy_qa_member(store):
    tracker = fake(store)
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    rec = dev_ticket(store, tracker)
    key = rec["ticket_key"]
    assert syncer.now()["intake"] == []                    # 아직 In Dev
    tracker.set_status(key, RESOLVE)
    result = syncer.now()
    assert result["intake"] == [key] and not result["errors"]
    assert tracker.get(key)["assignee"] == "QA팀장" and tracker.get(key)["comments"][-1].startswith("[트리아지 서비스 · 회귀 접수]")   # 미해결이 같으면 첫 구성원
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


def test_intake_spreads_over_qa_members(store):
    tracker = fake(store)
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    keys = [dev_ticket(store, tracker, i=i)["ticket_key"] for i in (1, 2)]
    for key in keys:
        tracker.set_status(key, RESOLVE)
    assert sorted(syncer.now()["intake"]) == sorted(keys)
    assert sorted(tracker.get(k)["assignee"] for k in keys) == ["QA팀원", "QA팀장"]   # 한 사람에게 몰리지 않는다


def test_intake_does_nothing_without_qa_team_and_reports_empty_group(store, monkeypatch):
    tracker = fake(store)
    key = dev_ticket(store, tracker)["ticket_key"]
    tracker.set_status(key, RESOLVE)
    assert sync.Syncer(store, tracker).now().get("intake") is None     # 자동 배정 설정이 없으면 하지 않는다
    monkeypatch.setattr(FakeTracker, "members", lambda self, team: [])
    result = sync.Syncer(store, tracker, RULES["qa"]).now()
    assert result["intake"] == [] and any("구성원이 없습니다" in e for e in result["errors"])
    assert tracker.get(key)["assignee"] not in ("QA팀장", "QA팀원")


def test_lead_sees_returned_ticket_on_mine_screen(store):
    tracker = fake(store)
    client = web(store, tracker)
    key = dev_ticket(store, tracker)["ticket_key"]
    client.post("/qa/sync")
    assert key not in client.get("/mine").text            # In Dev — 내 차례가 아니다
    tracker.set_status(key, RESOLVE)
    page = client.post("/qa/sync").text
    assert "QA팀에 회귀 테스트를 배정했습니다" in page and key in page
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


def test_regression_fail_returns_ticket_to_dev_team_member(store):
    """접수로 QA 구성원이 담당이 된 건이 회귀 실패하면 개발팀 구성원에게 돌아간다 — 담당이 QA 에 남아 사라지지 않게."""
    tracker = fake(store)
    syncer = sync.Syncer(store, tracker, RULES["qa"])
    rec = dev_ticket(store, tracker)
    key, dev_team = rec["ticket_key"], rec["assignment"]["team"]
    tracker.set_status(key, RESOLVE)
    syncer.now()
    assert tracker.get(key)["assignee"].startswith("QA")
    out = qa.act(store, tracker, rec["id"], "fail", "다시 재현됨", rules=RULES)
    assert out["ok"] and tracker.get(key)["status"] == IN_DEV
    # 그 팀 그룹에서 미해결이 가장 적은 사람이 받는다(첫 구성원이 아닐 수 있다)
    assert tracker.get(key)["assignee"].startswith(dev_team) and tracker.get(key)["team"] == dev_team
    assert f"(담당: {tracker.get(key)['assignee']})" in tracker.get(key)["comments"][-1]


def test_regression_fail_of_repro_ticket_also_goes_to_dev_team(store):
    tracker = fake(store)
    rec = register(store, tracker, 처리="등록(재현 대기)")
    qa.act(store, tracker, rec["id"], "repro", "재현됨", rules=RULES)
    dev_team = store.qa_log()[rec["id"]][-1]["team"]
    tracker.set_status(rec["ticket_key"], RESOLVE)
    qa.act(store, tracker, rec["id"], "fail", "또 재현", rules=RULES)
    assert tracker.get(rec["ticket_key"])["team"] == dev_team and tracker.get(rec["ticket_key"])["assignee"].startswith(dev_team)


def test_regression_fail_without_rules_only_changes_status(store):
    tracker = fake(store)
    rec = dev_ticket(store, tracker)
    tracker.set_status(rec["ticket_key"], RESOLVE)
    before = tracker.get(rec["ticket_key"])["assignee"]
    assert qa.act(store, tracker, rec["id"], "fail", "사유")["ok"] and tracker.get(rec["ticket_key"])["assignee"] == before


def test_regression_fail_never_hands_ticket_to_qa_team(store):
    """독립 검토 반영 — 지금 팀이 QA팀이고 모듈도 없어 규칙의 팀이 QA팀이면 담당은 그대로 두고 상태만 바꾼다."""
    import json
    from triage_service.store import connect
    tracker = fake(store)
    rec = dev_ticket(store, tracker)
    tracker.set_status(rec["ticket_key"], RESOLVE)
    before = tracker.get(rec["ticket_key"])["assignee"]
    final = {**store.get(rec["id"])["final_fields"], "모듈": "해당 없음"}
    with connect(store.path) as c:
        c.execute("UPDATE reports SET assignment = ?, final_fields = ? WHERE id = ?",
                  (json.dumps({**rec["assignment"], "team": RULES["qa"]}, ensure_ascii=False), json.dumps(final, ensure_ascii=False), rec["id"]))
    out = qa.act(store, tracker, rec["id"], "fail", "사유", rules=RULES)
    assert out["ok"] and tracker.get(rec["ticket_key"])["status"] == IN_DEV and tracker.get(rec["ticket_key"])["assignee"] == before


def test_regression_fail_skips_linked_existing_and_empty_group(store, monkeypatch):
    import json
    from triage_service.store import connect
    from triage_service.contracts import TrackerError
    tracker = fake(store)
    rec = dev_ticket(store, tracker)
    tracker.set_status(rec["ticket_key"], RESOLVE)
    before = tracker.get(rec["ticket_key"])["assignee"]
    monkeypatch.setattr(FakeTracker, "members", lambda self, team: [])        # 팀 그룹이 비면 보내지 않고 이유를 알린다
    import pytest
    with pytest.raises(qa.Conflict):
        qa.act(store, tracker, rec["id"], "fail", "사유", rules=RULES)
    with connect(store.path) as c:
        c.execute("UPDATE reports SET assignment = ? WHERE id = ?",
                  (json.dumps({**rec["assignment"], "linked_existing": True}, ensure_ascii=False), rec["id"]))
    assert qa.act(store, tracker, rec["id"], "fail", "사유", rules=RULES)["ok"]   # 연결한 건은 담당을 건드리지 않아 그룹을 읽지도 않는다
    assert tracker.get(rec["ticket_key"])["assignee"] == before
