"""BTS 연결 시험 — 설정 검사, 어댑터 계약(가짜·Jira·Redmine), 중복 등록 방지, 검토로 되돌리기, 외부 요청 차단.

실제 서버는 부르지 않는다. Jira·Redmine 은 tests/bts_responses/ 의 응답(출처는 각 파일의 "source")을 돌려주는
httpx2 가짜 전송(MockTransport)으로 시험한다.
"""

import json
import sqlite3
import tomllib
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from test_service import DEV_ITEMS, report_from_item, triaged
from triage_eval.contracts import ENUMS
from triage_service import config, flow, review
from triage_service.bts import make_tracker, settings
from triage_service.bts.common import UNCERTAIN
from triage_service.bts.fake import FakeTracker
from triage_service.contracts import Issue, TrackerError
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker

RESPONSES = Path(__file__).parent / "bts_responses"
MAPPING = tomllib.loads(config.MAPPING_PATH.read_text(encoding="utf-8"))
SECRETS = {"jira": {"JIRA_EMAIL": "qa@example.com", "JIRA_API_TOKEN": "SECRET-TOKEN-123"},
           "redmine": {"REDMINE_API_KEY": "SECRET-TOKEN-123"}}
ROUTES = {   # (방법, 경로) → 녹화 응답 이름. {key} 는 녹화한 티켓 키
    "jira": {("POST", "/rest/api/3/issue"): "create", ("GET", "/rest/api/3/search/jql"): "find",
             ("GET", "/rest/api/3/issue/{key}"): "status", ("GET", "/rest/api/3/user/assignable/search"): "assignable"},
    "redmine": {("POST", "/issues.json"): "create", ("GET", "/issues.json"): "find",
                ("GET", "/issues/{key}.json"): "status", ("GET", "/projects/triage/memberships.json"): "memberships"},
}


def write_mapping(tmp_path, text: str) -> Path:
    p = tmp_path / "mapping.toml"
    p.write_text(text, encoding="utf-8")
    return p


# ── 설정 검사 ─────────────────────────────────────────────

def test_committed_mapping_loads():
    loaded = settings.load(config.MAPPING_PATH, env={})
    assert loaded["use"] in settings.TRACKERS


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_committed_mapping_covers_every_priority(name):
    assert set(MAPPING[name]["priority"]) == set(ENUMS["우선순위"])


def test_selected_tracker_needs_its_tokens(tmp_path):
    text = config.MAPPING_PATH.read_text(encoding="utf-8").replace('use = "fake"', 'use = "jira"')
    with pytest.raises(settings.SettingsError, match="JIRA_API_TOKEN"):
        settings.load(write_mapping(tmp_path, text), env={"JIRA_EMAIL": "a@b.c"})
    loaded = settings.load(write_mapping(tmp_path, text), env=SECRETS["jira"])
    assert loaded["use"] == "jira" and loaded["secrets"]["JIRA_API_TOKEN"] == "SECRET-TOKEN-123"


@pytest.mark.parametrize("old, new, message", [
    ('use = "fake"', 'use = "trello"', "use 는"),
    ('project_key = "TRI"', 'projekt_key = "TRI"', "projekt_key"),
    ('Critical = "Highest"\n', "", "대응이 없는 우선순위: Critical"),
    ('Critical = 5', 'Critical = "5"', "Critical 의 값 형식"),
    ('base_url = "http://127.0.0.1:3000"', 'base_url = "127.0.0.1:3000"', "http://"),
    ('tracker_id = 1', 'tracker_id = true', "tracker_id 의 형식"),
    ('project_key = "TRI"', 'project_key = " "', "project_key 가 비어"),
    ('timeout_sec = 10\n\n[jira.priority]', 'timeout_sec = 0\n\n[jira.priority]', "0보다 커야"),
    ('labels = ["triage"]', 'labels = ["a b"]', "labels 는"),
    ('base_url = "http://127.0.0.1:3000"', 'base_url = "http://"', "주소여야"),
    ('[bts]\nuse = "fake"', 'bts = "fake"\n[x]\nuse = "fake"', "섹션"),
])
def test_bad_mapping_is_refused(tmp_path, old, new, message):
    text = config.MAPPING_PATH.read_text(encoding="utf-8")
    assert old in text
    with pytest.raises(settings.SettingsError, match=message):
        settings.load(write_mapping(tmp_path, text.replace(old, new, 1)), env={})


def test_mapping_path_prefers_env_then_local(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_MAPPING_PATH", tmp_path / "local.toml")
    monkeypatch.delenv("TRIAGE_BTS_MAPPING", raising=False)
    assert config.mapping_path() == config.MAPPING_PATH
    (tmp_path / "local.toml").write_text("", encoding="utf-8")
    assert config.mapping_path() == tmp_path / "local.toml"
    monkeypatch.setenv("TRIAGE_BTS_MAPPING", str(tmp_path / "other.toml"))
    assert config.mapping_path() == tmp_path / "other.toml"


def test_env_file_does_not_override_real_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# 주석\nTRIAGE_TEST_A="from-file"\nTRIAGE_TEST_B=from-file\n', encoding="utf-8")
    monkeypatch.setenv("TRIAGE_TEST_B", "real")
    monkeypatch.delenv("TRIAGE_TEST_A", raising=False)
    config.load_env(env)
    import os
    assert os.environ["TRIAGE_TEST_A"] == "from-file" and os.environ["TRIAGE_TEST_B"] == "real"
    monkeypatch.delenv("TRIAGE_TEST_A")


# ── 어댑터 계약 ────────────────────────────────────────────

class Recorder:
    """녹화 응답을 돌려주는 가짜 전송. 받은 요청을 남긴다. fail 은 (응답 이름 → 'timeout'|'connect'|'reject')."""

    def __init__(self, name: str, found: bool = False, fail: dict | None = None):
        self.name, self.found, self.fail = name, found, fail or {}
        self.data = json.loads((RESPONSES / f"{name}.json").read_text(encoding="utf-8"))
        self.requests: list[httpx2.Request] = []
        self.created: dict | None = None   # 이 시험에서 보낸 등록 요청 본문

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        routes = {(m, p.format(key=self.data["ticket_key"])): v for (m, p), v in ROUTES[self.name].items()}
        what = routes[(request.method, request.url.path)]
        if what == "find" and ("issue_id" in request.url.params or "status" in request.url.params.get("fields", "")):
            what = "snapshot"   # 같은 검색 주소로 여러 티켓의 상태를 읽는 요청 (jira.py 설계 의도 7, redmine.py 설계 의도 6)
        mode = self.fail.get(what)
        if mode == "timeout":
            raise httpx2.ReadTimeout("timed out", request=request)
        if mode == "connect":
            raise httpx2.ConnectError("refused", request=request)
        if what == "create" and mode is None:
            self.created = json.loads(request.content)
        key = "rejected" if mode == "reject" else ("find_hit" if self.found else "find_none") if what == "find" else what
        if what == "assignable":   # 녹화한 계정(qa-account-id)만 담당자가 될 수 있다
            key = "assignable_hit" if request.url.params.get("accountId") == "qa-account-id" else "assignable_none"
        body = json.loads(json.dumps(self.data[key]["body"]))
        if key == "find_hit" and self.name == "redmine" and self.created:
            # 녹화한 검색 응답의 모양 그대로, 설명만 이 시험에서 등록한 내용으로 (Redmine 은 설명의 표식 줄을 비교한다)
            body["issues"][0]["description"] = self.created["issue"]["description"].replace("\n", "\r\n")
        return httpx2.Response(self.data[key]["status"], json=body)


def adapter(name: str, store: Store, **kw):
    if name == "fake":
        return FakeTracker(store.path), None
    rec = Recorder(name, **kw)
    s = {**MAPPING[name], "base_url": rec.data["base_url"]}
    http = httpx2.Client(base_url=s["base_url"], transport=httpx2.MockTransport(rec),
                        headers={"Authorization": "Bearer SECRET-TOKEN-123"})
    return make_tracker({"use": name, "settings": s, "secrets": SECRETS[name]}, store, http), rec


def an_issue(store: Store, priority: str = "Critical") -> Issue:
    fields = {"요약": "길드 상점 강화석 미지급", "분류": "결함", "모듈": "결제·재화 / UI·텍스트", "우선순위": priority,
              "재현 정보": "충분", "발생 빈도": "항상", "처리": "긴급 사인 요청", "누락 정보 및 권장 조치": "구매 로그 확인"}
    return Issue(report=report_from_item(DEV_ITEMS[0]), fields=fields, ref=store.ref(7))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "service.db")


@pytest.mark.parametrize("name", ["fake", "jira", "redmine"])
def test_contract_create_find_status(name, store):
    """모든 어댑터가 같은 약속을 지킨다: 만들면 키·링크, 표식으로 다시 찾음, 상태 이름 (contracts.Tracker)."""
    tracker, rec = adapter(name, store)
    issue = an_issue(store)
    assert tracker.find(issue.ref) is None
    ticket = tracker.create(issue)
    assert ticket.key and ticket.url
    if rec:
        rec.found = True
    assert tracker.find(issue.ref) == ticket
    assert isinstance(tracker.status(ticket.key), str)


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_contract_sends_mapped_fields(name, store):
    tracker, rec = adapter(name, store)
    issue = an_issue(store, priority="Minor")
    tracker.create(issue)
    sent = json.loads(next(r for r in rec.requests if r.method == "POST").content)
    if name == "jira":
        f = sent["fields"]
        assert f["priority"] == {"name": MAPPING["jira"]["priority"]["Minor"]}
        assert f["project"] == {"key": MAPPING["jira"]["project_key"]} and f["summary"] == issue.fields["요약"]
        assert issue.ref in f["labels"] and "모듈_결제·재화" in f["labels"] and "모듈_UI·텍스트" in f["labels"]
        assert not any(" " in label for label in f["labels"])
        text = json.dumps(f["description"], ensure_ascii=False)
    else:
        i = sent["issue"]
        assert i["priority_id"] == MAPPING["redmine"]["priority"]["Minor"]
        assert i["project_id"] == MAPPING["redmine"]["project_id"] and i["subject"] == issue.fields["요약"]
        text = i["description"]
    assert issue.ref in text and "[처리]: 긴급 사인 요청" in text and issue.report.body.split("\n")[0] in text


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_contract_find_searches_by_marker(name, store):
    """표식으로 찾는 조건이 맞다 — Jira 는 라벨 전체 일치, Redmine 은 설명 검색."""
    tracker, rec = adapter(name, store)
    tracker.find("triage-abcd1234-7")
    params = rec.requests[-1].url.params
    if name == "jira":
        assert 'labels = "triage-abcd1234-7"' in params["jql"]
    else:
        assert (params["f[]"], params["op[description]"], params["v[description][]"]) == \
            ("description", "~", "triage-abcd1234-7")


def test_redmine_find_ignores_longer_marker(store):
    """Redmine 설명 검색은 포함 검색이다 — triage-x-1 로 찾았는데 triage-x-12 티켓이 돌아오면 같은 리포트가 아니다."""
    tracker, rec = adapter("redmine", store, found=True)
    recorded = rec.data["find_hit"]["body"]["issues"][0]["description"].splitlines()[-1].split(": ")[1]
    assert tracker.find(recorded) is not None
    assert tracker.find(recorded[:-1]) is None          # 녹화 표식 triage-xxxx-1 의 앞부분 triage-xxxx-


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_contract_rejection_message_without_token(name, store):
    tracker, rec = adapter(name, store, fail={"create": "reject"})
    with pytest.raises(TrackerError) as e:
        tracker.create(an_issue(store))
    assert rec.data["rejected_text"] in str(e.value) and "SECRET-TOKEN-123" not in str(e.value)


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_contract_create_timeout_is_uncertain(name, store):
    """등록 요청이 끊기면 '등록 여부 불확실'로 알리고 다시 보내지 않는다."""
    tracker, rec = adapter(name, store, fail={"create": "timeout"})
    with pytest.raises(TrackerError, match="등록됐는지 알 수 없습니다"):
        tracker.create(an_issue(store))
    assert sum(r.method == "POST" for r in rec.requests) == 1


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_contract_connection_errors(name, store):
    tracker, rec = adapter(name, store, fail={"find": "timeout", "status": "connect"})
    with pytest.raises(TrackerError) as e:
        tracker.find("x")
    assert UNCERTAIN not in str(e.value)
    with pytest.raises(TrackerError, match="연결할 수 없습니다"):
        tracker.status(rec.data["ticket_key"])


# ── 중복 등록 방지와 검토로 되돌리기 ───────────────────────────

class CountingFake(FakeTracker):
    def __init__(self, path):
        super().__init__(path)
        self.created = 0

    def create(self, issue):
        self.created += 1
        return super().create(issue)


def test_register_links_existing_ticket_instead_of_duplicating(store):
    """앞선 등록 요청이 끊겼지만 실제로는 만들어진 경우 — 다시 등록하면 새로 만들지 않고 그 티켓에 연결한다."""
    rid = triaged(store)
    tracker = CountingFake(store.path)
    fields = store.get(rid)["model_fields"]
    tracker.create(Issue(report=store.get(rid)["report"], fields=fields, ref=store.ref(rid)))   # 끊긴 요청이 남긴 티켓
    rec = review.register(store, tracker, rid, fields)
    assert rec["ticket_key"] == "FAKE-1" and tracker.created == 1


def test_markers_differ_between_databases(tmp_path):
    a, b = Store(tmp_path / "a.db"), Store(tmp_path / "b.db")
    assert a.ref(1) != b.ref(1) and a.ref(1) == Store(tmp_path / "a.db").ref(1)


class FlakyTracker(FakeTracker):
    def __init__(self, path):
        super().__init__(path)
        self.down = True

    def create(self, issue):
        if self.down:
            raise TrackerError(UNCERTAIN)
        return super().create(issue)


def test_register_failure_goes_back_to_review_with_values(store):
    rid = triaged(store)
    tracker = FlakyTracker(store.path)
    fields = {**store.get(rid)["model_fields"], "누락 정보 및 권장 조치": "구매 로그 확인"}
    rec = review.register(store, tracker, rid, fields)
    assert rec["state"] == flow.FAILED and rec["failed_step"] == "등록"
    rec = review.back_to_review(store, rid)
    assert rec["state"] == flow.REVIEW and rec["final_fields"]["누락 정보 및 권장 조치"] == "구매 로그 확인"
    tracker.down = False
    assert review.register(store, tracker, rid, rec["final_fields"])["state"] == flow.REGISTERED


def test_triage_failure_cannot_go_back_to_review(store):
    from test_service import BrokenClient
    rid = triaged(store, client=BrokenClient())
    assert store.get(rid)["failed_step"] == "트리아지"
    with pytest.raises(flow.FlowError):
        review.back_to_review(store, rid)


def test_s1_database_is_upgraded(tmp_path):
    """S1 에서 만든 DB(실패 단계 칸 없음)를 지우지 않고 이어 쓴다."""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
                  "updated_at TEXT NOT NULL, state TEXT NOT NULL, report TEXT NOT NULL, result TEXT, pipeline TEXT, "
                  "routing TEXT, model_fields TEXT, final_fields TEXT, edits TEXT, ticket_key TEXT, ticket_url TEXT, error TEXT)")
    c.close()
    store = Store(path)
    rid = store.add(report_from_item(DEV_ITEMS[0]))
    assert store.get(rid)["failed_step"] is None


# ── 화면: 외부 요청 차단, 되돌리기, BTS 상태 ─────────────────────

@pytest.fixture
def web(store):
    tracker = FlakyTracker(store.path)
    worker = Worker(store, client_factory=FakeClient)
    app = create_app(store, worker, tracker, start_worker=False, allowed_hosts=("testserver", "127.0.0.1"), sources=[])
    with TestClient(app) as c:
        c.tracker = tracker
        yield c


def test_cross_site_post_is_refused(web, store):
    rid = triaged(store)
    r = web.post(f"/reports/{rid}/discard", headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403 and store.get(rid)["state"] == flow.REVIEW
    r = web.post(f"/reports/{rid}/discard", headers={"Referer": "https://evil.example/page"}, follow_redirects=False)
    assert r.status_code == 403
    r = web.post(f"/reports/{rid}/discard", headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and store.get(rid)["state"] == flow.DISCARDED


def test_unknown_host_is_refused(web):
    assert web.get("/", headers={"Host": "attacker.example"}).status_code == 400


def test_web_back_to_review_and_status(web, store):
    rid = triaged(store)
    values = store.get(rid)["model_fields"]
    data = {f: v for f, v in values.items() if f != "모듈"}
    data["모듈"] = [m.strip() for m in values["모듈"].split("/")]
    web.post(f"/reports/{rid}/register", data=data)
    page = web.get(f"/reports/{rid}").text
    assert store.get(rid)["state"] == flow.FAILED and "검토로 되돌리기" in page
    assert web.post(f"/reports/{rid}/back-to-review", follow_redirects=False).status_code == 303
    web.tracker.down = False
    web.post(f"/reports/{rid}/register", data=data)
    rec = store.get(rid)
    assert rec["state"] == flow.REGISTERED
    assert "신규" in web.get(f"/reports/{rid}/bts-status").text
