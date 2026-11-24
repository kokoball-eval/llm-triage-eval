"""생성 시 자동 배정 시험 (S5-1) — 규칙 표, 설정 검사, 어댑터가 보내는 담당자, 담당자 계정 확인, 등록·화면·실행 명령.

실제 BTS 는 부르지 않는다. Jira 담당자 확인은 실제 KAN 공간 녹화(계정 정보 지움), Redmine 구성원 목록은 API 문서 형식 응답이다
(tests/bts_responses/*.json 의 *_source).
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_bts import MAPPING, FlakyTracker, adapter, an_issue, write_mapping
from test_service import triaged
from triage_eval.contracts import ENUMS, MODULES
from triage_service import assign, cli, config, flow, review
from triage_service.bts import settings
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import TrackerError
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker

RULES = MAPPING["assign"]


def fields(처리: str, 모듈: str = "결제·재화 / UI·텍스트") -> dict:
    return {"처리": 처리, "모듈": 모듈}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "service.db")


# ── 규칙 ────────────────────────────────────────────────

@pytest.mark.parametrize("처리, 모듈, team", [
    ("등록(재현 대기)", "결제·재화", "QA"),          # 재현이 필요하면 QA
    ("정보 요청 후 보류", "네트워크", "QA"),
    ("개발 배정", "결제·재화 / UI·텍스트", "서버"),   # 첫 모듈의 팀 (병기)
    ("개발 배정", "UI·텍스트 / 결제·재화", "클라"),
    ("긴급 사인 요청", "그래픽·사운드", "아트"),
    ("개발 배정", "해당 없음", "QA"),               # 모듈이 없으면 QA 가 나눈다
    ("CS 응대", "계정·로그인", "운영"),
    ("폐기", "결제·재화", ""),
])
def test_rule_table(처리, 모듈, team):
    assert assign.suggest(RULES, fields(처리, 모듈)) == team


def test_every_action_has_a_role():
    """[처리] 허용 값이 늘면 이 시험이 먼저 실패해 배정 규칙을 정하게 한다."""
    assert set(assign.ROLE) == set(ENUMS["처리"])


def test_no_rules_means_no_assignment():
    assert assign.suggest(None, fields("개발 배정")) == ""
    assert assign.teams(None) == []
    assert assign.resolve(None, "서버", fields("개발 배정")) == {"suggested": "", "team": ""}


def test_cs_team_is_optional():
    rules = {k: v for k, v in RULES.items() if k != "cs"}
    assert assign.suggest(rules, fields("CS 응대")) == ""
    assert "운영" not in assign.teams(rules)


def test_teams_are_unique_in_order():
    assert assign.teams(RULES) == ["QA", "운영", "클라", "서버", "아트", "기획"]


def test_resolve_choices():
    f = fields("개발 배정")
    assert assign.resolve(RULES, assign.AUTO, f) == {"suggested": "서버", "team": "서버"}
    assert assign.resolve(RULES, "기획", f) == {"suggested": "서버", "team": "기획"}
    assert assign.resolve(RULES, "", f) == {"suggested": "서버", "team": ""}
    with pytest.raises(ValueError, match="설정에 없는 팀"):
        assign.resolve(RULES, "해킹팀", f)


# ── 설정 검사 ─────────────────────────────────────────────

def test_committed_mapping_assigns_every_module():
    loaded = settings.load(config.MAPPING_PATH, env={})
    assert set(loaded["assign"]["modules"]) == set(MODULES)
    for name in ("jira", "redmine"):
        assert set(MAPPING[name]["teams"]) == set(assign.teams(loaded["assign"]))


def test_mapping_without_assign_still_loads(tmp_path):
    """[assign] 을 쓰지 않는 기존 설정 파일은 그대로 동작한다 (assign 설계 의도 5)."""
    text = config.MAPPING_PATH.read_text(encoding="utf-8")
    start, end = text.index("[assign]"), text.index("# ── Jira Cloud")
    text = text[:start] + text[end:]
    loaded = settings.load(write_mapping(tmp_path, text), env={})
    assert loaded["assign"] is None


@pytest.mark.parametrize("old, new, use, message", [
    ('"콘텐츠 진행" = "기획"\n', "", "fake", "팀이 없는 모듈: 콘텐츠 진행"),
    ('"콘텐츠 진행" = "기획"', '"콘텐츠" = "기획"', "fake", "허용 값이 아닌 모듈: 콘텐츠"),
    ('qa = "QA"', 'qa = " "', "fake", "qa 팀 이름이 없습니다"),
    ('cs = "운영"', 'cs = 3', "fake", "cs 의 형식"),
    ('cs = "운영"', 'cs = "  "', "fake", "cs 팀 이름이 공백뿐"),
    ('"아트"\n"게임플레이', '" 아트"\n"게임플레이', "fake", "앞뒤에 공백"),
    ('qa = "QA"', 'qa = "QA"\nlead = "x"', "fake", "모르는 키: lead"),
    ('"그래픽·사운드" = "아트"', '"그래픽·사운드" = "사운드"', "jira", "계정이 없는 팀: 사운드"),
    ('"기획" = 10', '"기획" = "10"', "redmine", "기획 의 계정 형식"),
    ('QA = "your-qa-account-id"', 'QA = ""', "jira", "QA 의 계정 형식"),
    ("[jira.teams]        #", "[jira.teamz]        #", "jira", "teamz"),
])
def test_bad_assign_is_refused(tmp_path, old, new, use, message):
    text = config.MAPPING_PATH.read_text(encoding="utf-8").replace('use = "fake"', f'use = "{use}"')
    assert old in text
    env = {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t", "REDMINE_API_KEY": "k"}
    with pytest.raises(settings.SettingsError, match=message):
        settings.load(write_mapping(tmp_path, text.replace(old, new, 1)), env=env)


def test_selected_tracker_needs_teams_table(tmp_path):
    text = config.MAPPING_PATH.read_text(encoding="utf-8").replace('use = "fake"', 'use = "redmine"')
    start = text.rindex("[redmine.teams]")   # 앞쪽 [assign] 설명에도 같은 글자가 있다
    with pytest.raises(settings.SettingsError, match=r"\[redmine.teams\] 표가 없습니다"):
        settings.load(write_mapping(tmp_path, text[:start]), env={"REDMINE_API_KEY": "k"})


# ── 어댑터: 담당자 넣기, 담당자 계정 확인 ─────────────────────

def sent_issue(rec) -> dict:
    import json
    return json.loads(next(r for r in rec.requests if r.method == "POST").content)


def test_jira_sends_assignee_and_team_label(store):
    tracker, rec = adapter("jira", store)
    issue = an_issue(store)
    tracker.create(issue.__class__(**{**issue.__dict__, "team": "서버"}))
    f = sent_issue(rec)["fields"]
    assert f["assignee"] == {"accountId": MAPPING["jira"]["teams"]["서버"]} and "팀_서버" in f["labels"]


def test_redmine_sends_assigned_to(store):
    tracker, rec = adapter("redmine", store)
    issue = an_issue(store)
    tracker.create(issue.__class__(**{**issue.__dict__, "team": "기획"}))
    assert sent_issue(rec)["issue"]["assigned_to_id"] == MAPPING["redmine"]["teams"]["기획"]


def test_redmine_sends_team_category_and_module_field(store):
    """Jira 의 팀·모듈 라벨 대신 Redmine 은 팀 범주와 모듈 사용자 정의 필드 (redmine 설계 의도 8·9, S5-4a)."""
    tracker, rec = adapter("redmine", store)
    tracker.s["module_field_id"] = 1   # 녹화한 Redmine 의 모듈 필드 번호(예시 설정에는 주석)
    issue = an_issue(store)   # 모듈: 결제·재화 / UI·텍스트
    tracker.create(issue.__class__(**{**issue.__dict__, "team": "기획"}))
    body = sent_issue(rec)["issue"]
    assert body["category_id"] == 6   # 녹화: 기획 범주 = 6
    assert body["custom_fields"] == [{"id": 1, "value": ["결제·재화", "UI·텍스트"]}]


def test_redmine_without_module_field_keeps_description_only(store):
    tracker, rec = adapter("redmine", store)
    assert "module_field_id" not in tracker.s
    tracker.create(an_issue(store))
    body = sent_issue(rec)["issue"]
    assert "custom_fields" not in body and "category_id" not in body   # 팀이 없으면 범주도 없다


def test_redmine_no_module_sends_no_field(store):
    tracker, rec = adapter("redmine", store)
    tracker.s["module_field_id"] = 1
    issue = an_issue(store)
    tracker.create(issue.__class__(**{**issue.__dict__, "fields": {**issue.fields, "모듈": "해당 없음"}}))
    assert "custom_fields" not in sent_issue(rec)["issue"]


def test_redmine_missing_team_category(store):
    tracker, rec = adapter("redmine", store)
    rec.data["issue_categories"]["body"]["issue_categories"] = [
        c for c in rec.data["issue_categories"]["body"]["issue_categories"] if c["name"] != "아트"]
    issue = an_issue(store)
    with pytest.raises(TrackerError, match="팀 범주 아트 가 없습니다"):
        tracker.create(issue.__class__(**{**issue.__dict__, "team": "아트"}))
    assert not any(r.method == "POST" for r in rec.requests)   # 보내기 전에 멈춘다
    assert "아트: triage 프로젝트에 같은 이름의 일감 범주가 없습니다" in tracker.check_assignees()


@pytest.mark.parametrize("name, extra, message", [
    ("jira", "module_field_id = 1\n", "모르는 키: module_field_id"),
    ("redmine", 'module_field_id = "1"\n', "module_field_id 의 형식"),
    ("redmine", "module_field_id = 0\n", "1 이상의 필드 번호"),
])
def test_module_field_id_only_for_redmine(tmp_path, name, extra, message):
    from test_bts import write_mapping
    from triage_service import config
    from triage_service.bts import settings
    text = config.MAPPING_PATH.read_text(encoding="utf-8")
    text = text.replace(f"[{name}]\n", f"[{name}]\n{extra}", 1)
    with pytest.raises(settings.SettingsError, match=message):
        settings.load(write_mapping(tmp_path, text), env={})


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_no_team_sends_no_assignee(name, store):
    tracker, rec = adapter(name, store)
    tracker.create(an_issue(store))
    body = sent_issue(rec)
    assert "assignee" not in body.get("fields", {}) and "assigned_to_id" not in body.get("issue", {})


def test_jira_check_assignees(store):
    tracker, _ = adapter("jira", store)
    assert len(tracker.check_assignees()) == len(MAPPING["jira"]["teams"])   # 예시 계정 ID 는 담당자가 될 수 없다
    tracker.s = {**tracker.s, "teams": {"QA": "qa-account-id", "서버": "qa-account-id"}}
    assert tracker.check_assignees() == []


def test_redmine_check_assignees(store):
    tracker, rec = adapter("redmine", store)
    assert tracker.check_assignees() == []   # 사용자(5·6)와 그룹(7~10) 모두 구성원
    tracker.s = {**tracker.s, "teams": {**tracker.s["teams"], "서버": 99}}
    assert tracker.check_assignees() == ["서버: 번호 99 은 triage 프로젝트의 구성원이 아닙니다"]


def test_fake_keeps_team_name(store):
    tracker = FakeTracker(store.path)
    issue = an_issue(store)
    ticket = tracker.create(issue.__class__(**{**issue.__dict__, "team": "아트"}))
    assert tracker.get(ticket.key)["team"] == "아트" and tracker.check_assignees() == []


# ── 등록 흐름 ────────────────────────────────────────────

def test_register_assigns_by_confirmed_fields(store):
    """모델이 아니라 사람이 확정한 8칸으로 정한다 — 모듈을 고치면 고친 모듈의 팀 (assign 설계 의도 1·4)."""
    tracker = FakeTracker(store.path)
    rid = triaged(store)
    values = {**store.get(rid)["model_fields"], "처리": "개발 배정", "모듈": "그래픽·사운드"}
    rec = review.register(store, tracker, rid, values, RULES)
    assert rec["assignment"] == {"suggested": "아트", "team": "아트"}
    assert tracker.get(rec["ticket_key"])["team"] == "아트"


def test_register_keeps_human_choice(store):
    tracker = FakeTracker(store.path)
    rid = triaged(store)
    values = {**store.get(rid)["model_fields"], "처리": "개발 배정", "모듈": "그래픽·사운드"}
    rec = review.register(store, tracker, rid, values, RULES, "기획")
    assert rec["assignment"] == {"suggested": "아트", "team": "기획"}


def test_register_failure_keeps_assignment(store):
    tracker = FlakyTracker(store.path)
    rid = triaged(store)
    values = {**store.get(rid)["model_fields"], "처리": "등록(재현 대기)"}
    rec = review.register(store, tracker, rid, values, RULES)
    assert rec["state"] == flow.FAILED and rec["assignment"]["team"] == "QA"
    assert review.retry(store, Worker(store, client_factory=FakeClient), rid)["assignment"] is None


def test_unknown_team_is_refused_before_bts(store):
    tracker = FakeTracker(store.path)
    rid = triaged(store)
    with pytest.raises(ValueError):
        review.register(store, tracker, rid, store.get(rid)["model_fields"], RULES, "해킹팀")
    assert store.get(rid)["state"] == flow.REVIEW


def test_old_database_gets_assignment_column(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                  "updated_at TEXT NOT NULL, state TEXT NOT NULL, report TEXT NOT NULL, result TEXT, pipeline TEXT, "
                  "routing TEXT, model_fields TEXT, final_fields TEXT, edits TEXT, ticket_key TEXT, ticket_url TEXT, "
                  "error TEXT, failed_step TEXT)")
        c.execute("INSERT INTO reports (created_at, updated_at, state, report) VALUES ('t', 't', '접수', ?)",
                  ('{"track": "A", "title": "옛 리포트", "body": "본문"}',))
    store = Store(path)
    with sqlite3.connect(path) as c:
        assert "assignment" in {r[1] for r in c.execute("PRAGMA table_info(reports)")}
    old = store.get(1)   # 기존 행은 그대로, 배정 기록은 없음
    assert old["report"].title == "옛 리포트" and old["assignment"] is None


def test_linked_existing_ticket_is_marked(store):
    """이미 있는 티켓에 연결하면 담당자를 바꾸지 않았다고 남긴다 (review 설계 의도 7)."""
    tracker = FakeTracker(store.path)
    rid = triaged(store)
    values = store.get(rid)["model_fields"]
    issue = an_issue(store)
    tracker.create(issue.__class__(**{**issue.__dict__, "ref": store.ref(rid)}))   # 끊긴 등록이 실제로는 만들어진 경우
    rec = review.register(store, tracker, rid, values, RULES)
    assert rec["assignment"]["linked_existing"] is True
    assert len(tracker.check_assignees()) == 0


def test_redmine_reads_every_member_page(store):
    """구성원이 100명을 넘어도 끝 페이지까지 읽는다 (redmine 설계 의도 5)."""
    import httpx2

    from triage_service.bts.redmine import RedmineTracker
    people = [{"user": {"id": n}} for n in range(1, 151)]

    def pages(request):
        if request.url.path.endswith("issue_categories.json"):   # 팀 범주 (redmine 설계 의도 8)
            return httpx2.Response(200, json={"issue_categories": [{"id": 1, "name": "QA"}, {"id": 4, "name": "서버"}]})
        off, lim = int(request.url.params["offset"]), int(request.url.params["limit"])
        return httpx2.Response(200, json={"memberships": people[off:off + lim], "total_count": len(people)})
    s = {**MAPPING["redmine"], "teams": {"QA": 5, "서버": 140}}
    http = httpx2.Client(base_url=s["base_url"], transport=httpx2.MockTransport(pages))
    assert RedmineTracker(s, {"REDMINE_API_KEY": "k"}, http).check_assignees() == []
    s["teams"]["서버"] = 151
    assert RedmineTracker(s, {"REDMINE_API_KEY": "k"}, http).check_assignees() == [
        "서버: 번호 151 은 triage 프로젝트의 구성원이 아닙니다"]


def test_redmine_member_list_refused_names_the_permission(store):
    tracker, _ = adapter("redmine", store, fail={"memberships": "reject"})
    with pytest.raises(TrackerError, match="구성원 관리 권한"):
        tracker.check_assignees()


# ── 화면 ────────────────────────────────────────────────

def web_client(store, rules):
    tracker = FakeTracker(store.path)
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=rules)
    return TestClient(app), tracker


def form(values: dict, team: str | None) -> dict:
    data = {f: v for f, v in values.items() if f != "모듈"}
    data["모듈"] = [m.strip() for m in values["모듈"].split("/")]
    if team is not None:
        data["team"] = team
    return data


def test_web_team_select_and_override(store):
    client, tracker = web_client(store, RULES)
    rid = triaged(store)
    values = {**store.get(rid)["model_fields"], "처리": "개발 배정", "모듈": "결제·재화"}
    page = client.get(f"/reports/{rid}").text
    assert 'name="team"' in page and "배정 안 함" in page
    client.post(f"/reports/{rid}/register", data=form(values, "기획"))
    rec = store.get(rid)
    assert rec["state"] == flow.REGISTERED and tracker.get(rec["ticket_key"])["team"] == "기획"
    assert "규칙: 서버 → 사람이 바꿈" in client.get(f"/reports/{rid}").text


def test_web_back_to_review_keeps_chosen_team(store):
    """등록 실패 → 검토로 되돌리기 → 사람이 고른 팀이 다시 선택돼 있다 (review 설계 의도 7)."""
    tracker = FlakyTracker(store.path)
    app = create_app(store, Worker(store, client_factory=FakeClient), tracker, start_worker=False,
                     allowed_hosts=("testserver",), sources=[], assign_rules=RULES)
    client = TestClient(app)
    rid = triaged(store)
    values = {**store.get(rid)["model_fields"], "처리": "개발 배정", "모듈": "결제·재화"}
    client.post(f"/reports/{rid}/register", data=form(values, "기획"))
    assert store.get(rid)["state"] == flow.FAILED
    client.post(f"/reports/{rid}/back-to-review")
    assert '<option value="기획" selected>' in client.get(f"/reports/{rid}").text
    tracker.down = False
    client.post(f"/reports/{rid}/register", data=form(values, "기획"))
    rec = store.get(rid)
    assert rec["state"] == flow.REGISTERED and tracker.get(rec["ticket_key"])["team"] == "기획"


def test_web_unknown_team_shows_problem(store):
    client, _ = web_client(store, RULES)
    rid = triaged(store)
    r = client.post(f"/reports/{rid}/register", data=form(store.get(rid)["model_fields"], "해킹팀"))
    assert r.status_code == 422 and "설정에 없는 팀" in r.text and store.get(rid)["state"] == flow.REVIEW


def test_web_without_rules_has_no_team_select(store):
    client, tracker = web_client(store, None)
    rid = triaged(store)
    assert 'name="team"' not in client.get(f"/reports/{rid}").text
    client.post(f"/reports/{rid}/register", data=form(store.get(rid)["model_fields"], None))
    assert tracker.get(store.get(rid)["ticket_key"])["team"] == ""


# ── 실행 명령 ────────────────────────────────────────────

def test_check_reports_assign_teams(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(config.MAPPING_PATH))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "service.db")
    assert cli.main(["--check"]) == 0
    assert "자동 배정: 팀 QA, 운영, 클라, 서버, 아트, 기획" in capsys.readouterr().out
