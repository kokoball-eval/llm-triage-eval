"""대시보드 QA 지표 시험 (S5-4c) — 마일스톤 티켓의 팀 표식 읽기, 저장, 지표 계산(metrics.qa), 대시보드 화면.

실제 BTS 는 부르지 않는다. Jira·Redmine 응답은 실제 서버에서 녹화한 것이다(tests/bts_responses/*.json 의 milestone_source).
"""

import sqlite3

from test_bts import adapter
from test_milestone import RULES, bt, fake, register, store, web   # noqa: F401 — store 는 픽스처
from test_service import DEV_ITEMS, triaged
from triage_service import metrics, review, sync
from triage_service.bts.jira import team_label, team_of
from triage_service.contracts import CLOSED, IN_DEV, QA_REVIEW, RESOLVE, BtsState, BtsTicket
from triage_service.store import Store


# ── 팀 표식 읽기 ─────────────────────────────────────────

def test_jira_and_redmine_read_team_marks(store):
    """녹화: KAN-9 는 팀_QA 라벨, Redmine #3 은 일감 범주 QA — 두 BTS 가 같은 팀을 돌려준다."""
    for name in ("jira", "redmine"):
        tracker, _ = adapter(name, store)
        assert [t.team for t in tracker.milestone_tickets("0.0.1")] == ["QA"]
    jira, rec = adapter("jira", store)
    jira.milestone_tickets("0.0.1")
    assert "labels" in rec.requests[-1].url.params["fields"].split(",")


def test_team_of_is_inverse_of_team_label():
    assert team_of([team_label("품질 보증"), "triage"]) == "품질 보증"
    assert team_of(["triage", "모듈_UI·텍스트"]) == ""
    assert team_of(["팀_서버", "팀_QA"]) == "QA"   # 사람이 둘 붙였으면 이름순 첫 번째


def test_redmine_ticket_without_category(store):
    tracker, rec = adapter("redmine", store)
    rec.data["milestone"]["body"]["issues"][0].pop("category")
    assert tracker.milestone_tickets("0.0.1")[0].team == ""


# ── 저장 ─────────────────────────────────────────────────

def test_store_keeps_ticket_team(store):
    store.save_tickets("0.0.1", [BtsTicket("A", "t", BtsState(IN_DEV), "서버")], "t1")
    store.save_tickets("0.0.1", [BtsTicket("A", "t", BtsState(IN_DEV), "클라")], "t2")   # BTS 에서 팀을 옮김
    assert store.tickets("0.0.1")[0]["team"] == "클라"


def test_old_db_gets_team_column(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:   # S5-4b 의 표 (team 칸 없음)
        c.execute("CREATE TABLE bts_tickets (key TEXT PRIMARY KEY, milestone TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL, "
                  "assignee TEXT NOT NULL, updated TEXT NOT NULL, synced_at TEXT NOT NULL)")
        c.execute("INSERT INTO bts_tickets VALUES ('A', '0.0.1', 't', 'In Dev', '', '', 't0')")
    s = Store(path)
    assert s.tickets("0.0.1")[0]["team"] == ""   # 기존 행은 팀 없음으로 이어 쓴다
    s.save_tickets("0.0.1", [BtsTicket("A", "t", BtsState(IN_DEV), "서버")], "t1")
    assert s.tickets("0.0.1")[0]["team"] == "서버"


# ── 지표 계산 ────────────────────────────────────────────

def T(hour):
    return f"2026-11-24T{hour:02d}:00:00"


def tk(key, status, team=""):
    return {"key": key, "status": status, "team": team}


def test_qa_metrics():
    from datetime import datetime
    tickets = [tk("A", CLOSED, "서버"), tk("B", RESOLVE, "서버"), tk("C", RESOLVE, "서버"), tk("D", QA_REVIEW)]
    changes = {
        # A: 회귀 실패로 재오픈 → 다시 Resolve → Closed. Resolve 에 3시간, 1시간 머묾
        "A": [(T(0), None, QA_REVIEW), (T(1), QA_REVIEW, IN_DEV), (T(2), IN_DEV, RESOLVE), (T(5), RESOLVE, IN_DEV),
              (T(6), IN_DEV, RESOLVE), (T(7), RESOLVE, CLOSED)],
        # B: 처음 읽었을 때 이미 Resolve — 들어간 시각을 모른다
        "B": [(T(0), None, RESOLVE)],
        # C: 1시에 Resolve 로 들어가 아직 대기 중
        "C": [(T(0), None, IN_DEV), (T(1), IN_DEV, RESOLVE)],
        "D": [(T(0), None, QA_REVIEW)],
    }
    q = metrics.qa(tickets, changes, [], now=datetime.fromisoformat(T(5)))
    assert (q["total"], q["resolve_now"], q["unknown_entry"]) == (4, 2, 1)
    assert q["resolve_wait"] == {"n": 2, "p50": 1.0, "p95": 3.0} and q["oldest_wait"] == 4.0
    assert q["reopen"] == {"n": 1, "d": 3, "pct": 33.3}   # Resolve 에 있는 것을 본 A·B·C 중 A
    assert q["teams"] == [("서버", 2), ("팀 없음", 1)]   # Closed 인 A 는 빠진다
    assert q["assign_changed"]["pct"] is None


def test_qa_metrics_assign_changed_counts_only_service_assignments():
    def rec(key, team, suggested, linked=False):
        return {"ticket_key": key, "assignment": {"team": team, "suggested": suggested, **({"linked_existing": True} if linked else {})}}
    tickets = [tk(k, IN_DEV) for k in "ABCDE"]
    records = [rec("A", "서버", "서버"), rec("B", "클라", "서버"), rec("C", "QA", "서버", linked=True),   # C: 배정 적용 안 함
               rec("D", "", ""), {"ticket_key": "E", "assignment": None},                         # D: 규칙의 팀 없음, E: 자동 배정 꺼짐
               rec("Z", "클라", "서버")]                                                           # Z: 다른 마일스톤
    assert metrics.qa(tickets, {}, records)["assign_changed"] == {"n": 1, "d": 2, "pct": 50.0}


def test_qa_metrics_empty():
    q = metrics.qa([], {}, [])
    assert q["reopen"]["pct"] is None and q["resolve_wait"]["n"] == 0 and q["oldest_wait"] is None and q["teams"] == []


# ── 동기화 → 화면 ─────────────────────────────────────────

def test_web_dashboard_qa_section(store):
    tracker = fake(store, saved=False)
    client = web(store, tracker)
    assert "마일스톤이 없습니다" in client.get("/dashboard").text
    client.post("/qa/sync")
    qa_key = register(store, tracker)["ticket_key"]                     # 재현 대기 → QA 팀
    dev = register(store, tracker, 처리="개발 배정", i=1)                # 규칙대로 모듈의 팀
    rid = triaged(store, DEV_ITEMS[2])
    moved = review.register(store, tracker, rid, {**store.get(rid)["model_fields"], "처리": "개발 배정"}, RULES,
                            team="QA", milestone="0.0.1")                # 사람이 팀을 바꿈
    assert moved["assignment"]["suggested"] != "QA"
    tracker.set_status(qa_key, CLOSED)
    client.post("/qa/sync")
    page = client.get("/dashboard").text
    assert "QA 지표 (마일스톤별)" in page and "<strong>0.0.1</strong>" in page and "BTS 티켓 3건" in page
    assert '사람이 바꾼 배정</span><br><span class="big">33.3%</span> <span class="muted">(1 / 3건)' in page
    assert f'<th>{dev["assignment"]["team"]}</th>' in page              # 팀별 미해결 막대
    assert "<strong>0.1.0</strong>" in client.get("/dashboard?milestone=0.1.0&period=7").text
    assert "<strong>0.0.1</strong>" in client.get("/dashboard?milestone=없는것").text   # 모르는 값이면 현재 마일스톤
    page = client.get("/dashboard?milestone=0.1.0&period=7").text
    assert "period=30&amp;milestone=0.1.0" in page and "period=7&amp;milestone=0.0.1" in page   # 한쪽을 바꿔도 다른 쪽 유지


def test_web_all_milestones_released_shows_last(store):
    """독립 검토 반영 — 모두 출시했으면 "마일스톤이 없습니다" 대신 마지막 마일스톤을 보인다."""
    tracker = fake(store, released={"0.0.1", "0.1.0", "0.2.0"})
    client = web(store, tracker)
    assert "<strong>0.2.0</strong>" in client.get("/dashboard").text and "마일스톤 0.2.0" in client.get("/").text


def test_sync_reads_ticket_team_from_fake(store):
    tracker = fake(store)
    rec = register(store, tracker, 처리="개발 배정")
    sync.run(store, tracker)
    assert store.tickets("0.0.1")[0]["team"] == rec["assignment"]["team"] != ""
