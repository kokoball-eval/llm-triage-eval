"""링크 출처 시험 — 설정 검사, 출처 계약(Jira·Redmine·게시판·디스코드), 가져오기 안전장치, 링크 화면 흐름.

실제 서버는 부르지 않는다. Jira·Redmine 은 실제 이슈 조회 응답 녹화(tests/intake_responses/), 게시판은 견본 HTML
(tests/intake_responses/board_post.html), 디스코드는 공식 문서 형식의 응답으로 httpx2 가짜 전송(MockTransport)을 쓴다.
"""

import json
from datetime import datetime
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from conftest import FakeClient
from triage_eval.triage.prompt import build_input
from triage_service import config, flow, intake
from triage_service.bts.fake import FakeTracker
from triage_service.bts.settings import SettingsError
from triage_service.intake import settings
from triage_service.intake.bts import adf_text
from triage_service.intake.fetching import get, get_json
from triage_service.store import Store
from triage_service.web.app import create_app
from triage_service.worker import Worker

RESP = Path(__file__).parent / "intake_responses"
FETCH = {"timeout_sec": 5, "max_bytes": 100_000}
BTS_BASE = {"jira": "https://your-site.atlassian.net", "redmine": "http://127.0.0.1:3000"}
BOARD = {"name": "견본", "domain": "forum.example.com", "board": "공식 홈페이지 > 버그 제보", "path_prefix": "/board/",
         "title": "h1.post-title", "body": "div.post-body", "posted_at": "time.post-date"}
DISCORD_MSG = {"id": "1300000000000000003", "channel_id": "1300000000000000002", "type": 0,
               "content": "길드전 보상 우편이 안 와요\n오늘 길드전 끝나고 30분 지났는데 우편함이 비어 있어요. 길드원 셋 다 같아요.",
               "timestamp": "2026-11-19T12:30:15.123000+00:00", "author": {"id": "1", "username": "player_kim"}}


def transport(routes: dict, seen: list | None = None):
    """경로 → (상태, 본문 bytes|dict, 머리글)."""
    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        status, body, headers = routes.get(request.url.path, (404, {"errorMessages": ["not found"]}, {}))
        if isinstance(body, (dict, list)):
            return httpx2.Response(status, json=body, headers=headers)
        return httpx2.Response(status, content=body, headers=headers)
    return httpx2.MockTransport(handler)


def local(iso: str) -> str:
    """시험이 도는 PC 의 시간대로 바꾼 기대값 (CI 는 UTC, 이 PC 는 KST)."""
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")


def recorded(name: str) -> dict:
    return json.loads((RESP / f"{name}.json").read_text(encoding="utf-8"))


def sources(seen: list | None = None, board_html: bytes | None = None, board_status: int = 200, board_headers=None):
    j, r = recorded("jira"), recorded("redmine")
    html = board_html if board_html is not None else (RESP / "board_post.html").read_bytes()
    clients = {
        "jira": httpx2.Client(base_url=BTS_BASE["jira"], transport=transport(
            {"/rest/api/3/issue/KAN-5": (j["status"], j["body"], {})}, seen)),
        "redmine": httpx2.Client(base_url=BTS_BASE["redmine"], transport=transport(
            {"/issues/2.json": (r["status"], r["body"], {})}, seen)),
        "board": httpx2.Client(transport=transport({"/board/1024": (board_status, html, board_headers or {})}, seen)),
        "discord": httpx2.Client(base_url="https://discord.com/api/v10", transport=transport(
            {"/api/v10/channels/1300000000000000002/messages/1300000000000000003": (200, DISCORD_MSG, {})}, seen)),
    }
    loaded = {"fetch": FETCH, "boards": [BOARD], "off": {},
              "jira": {"base_url": BTS_BASE["jira"], "secrets": {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "SECRET-1"},
                       "reporter": "내부 QA"},
              "redmine": {"base_url": BTS_BASE["redmine"], "secrets": {"REDMINE_API_KEY": "SECRET-1"}, "reporter": "퍼블리셔 QA"},
              "discord": {"board": "디스코드 > #버그-제보", "token": "SECRET-1"}}
    return intake.make_sources(loaded, clients)


LINKS = {
    "jira": "https://your-site.atlassian.net/browse/KAN-5",
    "redmine": "http://127.0.0.1:3000/issues/2",
    "board": "https://forum.example.com/board/1024",
    "discord": "https://discord.com/channels/1300000000000000001/1300000000000000002/1300000000000000003",
}


# ── 설정 검사 ─────────────────────────────────────────────

def test_committed_sources_settings_load():
    loaded = settings.load(config.SOURCES_PATH, config.MAPPING_PATH, env={})
    assert loaded["off"] == {"jira": "토큰 없음(JIRA_EMAIL, JIRA_API_TOKEN)", "redmine": "토큰 없음(REDMINE_API_KEY)",
                             "discord": "설정에서 꺼 둠"}
    assert [b["name"] for b in loaded["boards"]] == ["견본 게시판"]


def test_sources_use_bts_addresses_and_tokens():
    env = {"JIRA_EMAIL": "a@b.c", "JIRA_API_TOKEN": "t", "REDMINE_API_KEY": "k"}
    loaded = settings.load(config.SOURCES_PATH, config.MAPPING_PATH, env=env)
    assert loaded["jira"]["base_url"] == "https://your-site.atlassian.net" and loaded["redmine"]["reporter"] == "내부 QA"
    assert {s.name for s in intake.make_sources(loaded)} == {"jira", "redmine", "board"}


@pytest.mark.parametrize("old, new, message", [
    ('reporter = "내부 QA"\n\n[redmine]', 'reporter = "QA팀"\n\n[redmine]', "reporter 는"),
    ('domain = "127.0.0.1:8765"', 'domain = "http://127.0.0.1:8765"', "호스트"),
    ('path_prefix = "/board/"', 'path_prefix = "board/"', "path_prefix"),
    ('max_bytes = 1000000', 'max_bytes = 0', "0보다 커야"),
    ('title = "h1.post-title"', 'titel = "h1.post-title"', "titel"),
    ('[discord]\nenabled = false', '[discord]\nenabled = "no"', "enabled"),
    ('body = "div.post-body"', 'body = "div[[["', "선택자 오류"),
])
def test_bad_sources_settings_are_refused(tmp_path, old, new, message):
    text = config.SOURCES_PATH.read_text(encoding="utf-8")
    assert old in text
    p = tmp_path / "s.toml"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(SettingsError, match=message):
        settings.load(p, config.MAPPING_PATH, env={})


# ── 출처 계약 ─────────────────────────────────────────────

@pytest.mark.parametrize("name", list(LINKS))
def test_contract_matches_and_fetches(name):
    """모든 출처가 같은 약속을 지킨다: 자기 링크만 맡고, 가져오면 측정한 입력 모양의 Report (contracts.Source)."""
    srcs = sources()
    src = intake.find_source(srcs, LINKS[name])
    assert src.name == name
    assert [s.name for s in srcs if s.matches(LINKS[name])] == [name]
    rep = src.fetch(LINKS[name])
    assert rep.source == name and rep.url == LINKS[name] and rep.title and rep.body
    item = rep.to_item()
    assert set(item["input"]) == ({"board", "posted_at", "title", "body", "context"} if rep.track == "A"
                                  else {"reporter", "title", "body", "context"})
    assert "[제목] " + rep.title in build_input(item)


@pytest.mark.parametrize("name", ["jira", "redmine"])
def test_bts_issue_becomes_qa_issue(name):
    rep = intake.find_source(sources(), LINKS[name]).fetch(LINKS[name])
    exp = recorded(name)["expect"]
    assert (rep.track, rep.title, rep.body) == ("B", exp["title"], exp["body"])
    assert rep.reporter == {"jira": "내부 QA", "redmine": "퍼블리셔 QA"}[name]
    assert "\r" not in rep.body


def test_board_post_is_parsed_by_selectors():
    rep = intake.find_source(sources(), LINKS["board"]).fetch(LINKS["board"])
    assert rep.title == "길드 상점 강화석 묶음 구매 후 미지급" and rep.posted_at == local("2026-11-19T21:05:00+09:00")
    assert rep.board == "공식 홈페이지 > 버그 제보" and "광고" not in rep.body and "댓글" not in rep.body
    assert "닉네임_길드장" not in rep.body + rep.title     # 작성자 닉네임은 가져오지 않는다


def test_board_structure_change_fails_instead_of_guessing():
    srcs = sources(board_html="<html><body><h2>바뀐 구조</h2><p>본문</p></body></html>".encode())
    with pytest.raises(intake.IntakeError, match="구조가 설정과 다릅니다"):
        intake.find_source(srcs, LINKS["board"]).fetch(LINKS["board"])


def test_discord_message_first_line_is_title():
    rep = intake.find_source(sources(), LINKS["discord"]).fetch(LINKS["discord"])
    assert (rep.track, rep.title) == ("A", "길드전 보상 우편이 안 와요")
    assert rep.posted_at == local("2026-11-19T12:30:15.123000+00:00")     # UTC → 현지 시각
    assert "player_kim" not in rep.body + rep.title


def test_board_inline_tags_do_not_split_lines():
    html = (RESP / "board_post.html").read_text(encoding="utf-8").replace(
        "<p>코인만 빠지고", "<p>게임이 <b>강제 종료</b>되고 <a href='#'>로그</a>가 남아요.<br>두 번째 줄</p><p>코인만 빠지고", 1)
    rep = intake.find_source(sources(board_html=html.encode()), LINKS["board"]).fetch(LINKS["board"])
    assert "게임이 강제 종료되고 로그가 남아요.\n두 번째 줄\n" in rep.body


def test_board_time_without_timezone_falls_back_to_now():
    html = (RESP / "board_post.html").read_text(encoding="utf-8").replace(
        'datetime="2026-11-19T21:05:00+09:00"', 'datetime="3시간 전"')
    rep = intake.find_source(sources(board_html=html.encode()), LINKS["board"]).fetch(LINKS["board"])
    assert rep.posted_at[:10] == datetime.now().strftime("%Y-%m-%d")


def test_adf_lists_mentions_and_tables():
    doc = {"type": "doc", "content": [
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "가"}]}]},
            {"type": "listItem", "content": [{"type": "paragraph", "content": [
                {"type": "mention", "attrs": {"text": "@리드QA"}}, {"type": "text", "text": " 확인"}]}]}]},
        {"type": "table", "content": [{"type": "tableRow", "content": [
            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "빌드"}]}]},
            {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "2.7"}]}]}]}]}]}
    text = adf_text(doc)
    assert text.startswith("가\n@리드QA 확인\n") and "빌드" in text and "2.7" in text and "\n\n\n" not in text


@pytest.mark.parametrize("variant", [
    "https://your-site.atlassian.net/browse/KAN-5/",
    "https://your-site.atlassian.net/browse/KAN-5?focusedCommentId=1",
    "HTTPS://YOUR-SITE.atlassian.net/browse/kan-5",
])
def test_link_variants_map_to_one_canonical(variant):
    src, canonical = intake.identify(sources(), variant)
    assert (src.name, canonical) == ("jira", LINKS["jira"])


def test_discord_subdomains_map_to_one_canonical():
    ptb = LINKS["discord"].replace("https://discord.com", "https://ptb.discord.com")
    assert intake.identify(sources(), ptb)[1] == LINKS["discord"]


def test_bts_base_url_with_subpath():
    loaded = {"fetch": FETCH, "boards": [], "off": {}, "discord": None, "jira": None,
              "redmine": {"base_url": "https://corp.example/redmine", "secrets": {"REDMINE_API_KEY": "k"},
                          "reporter": "내부 QA"}}
    (src,) = intake.make_sources(loaded)
    assert src.matches("https://corp.example/redmine/issues/7") and not src.matches("https://corp.example/issues/7")
    assert src.canonical("https://corp.example/redmine/issues/007/") == "https://corp.example/redmine/issues/7"


# ── 가져오기 안전장치 ─────────────────────────────────────────

@pytest.mark.parametrize("link", [
    "https://evil.example/board/1024",                   # 설정에 없는 호스트
    "https://forum.example.com/admin/1",                 # 허용 호스트지만 경로 밖
    "http://127.0.0.1:3000/admin",                       # BTS 호스트지만 이슈 링크가 아님
    "https://your-site.atlassian.net.evil.example/browse/KAN-5",
    "file:///C:/Windows/win.ini",
    "http://169.254.169.254/latest/api/token",
    "https://forum.example.com/board/../admin",          # 경로 앞부분 밖으로 나가는 점 경로
    "https://forum.example.com/board/%2e%2e/admin",
    "https://forum.example.com/board/..%2fadmin",
    "https://forum.example.com/board/1024\tx",          # 제어 문자
    "https://your-site.atlassian.net\\@evil.example/browse/KAN-5",
])
def test_unlisted_links_are_never_fetched(link):
    seen = []
    with pytest.raises(intake.IntakeError):
        intake.find_source(sources(seen), link)
    assert seen == []


def test_redirects_are_not_followed():
    seen = []
    srcs = sources(seen, board_status=302, board_headers={"location": "http://169.254.169.254/"})
    with pytest.raises(intake.IntakeError, match="따라가지 않습니다"):
        intake.find_source(srcs, LINKS["board"]).fetch(LINKS["board"])
    assert [r.url.host for r in seen] == ["forum.example.com"]


def test_non_object_json_is_an_intake_error():
    client = httpx2.Client(transport=transport({"/x": (200, [1, 2], {})}))
    with pytest.raises(intake.IntakeError, match="형식"):
        get_json(client, "https://forum.example.com/x", max_bytes=1000)


def test_response_size_is_capped():
    client = httpx2.Client(transport=transport({"/big": (200, b"x" * 5000, {})}))
    with pytest.raises(intake.IntakeError, match="너무 큽니다"):
        get(client, "https://forum.example.com/big", max_bytes=1000)


def test_fetch_errors_do_not_leak_tokens():
    srcs = sources()
    with pytest.raises(intake.IntakeError) as e:
        intake.find_source(srcs, "https://your-site.atlassian.net/browse/KAN-999").fetch(
            "https://your-site.atlassian.net/browse/KAN-999")
    assert "404" in str(e.value) and "SECRET" not in str(e.value)


# ── 화면: 가져오기 → 미리보기 → 접수 ───────────────────────────────

@pytest.fixture
def web(tmp_path):
    store = Store(tmp_path / "service.db")
    worker = Worker(store, client_factory=FakeClient)
    app = create_app(store, worker, FakeTracker(store.path), start_worker=False, allowed_hosts=("testserver",),
                     sources=sources(), sources_off={"discord": "토큰 없음(DISCORD_BOT_TOKEN)"})
    with TestClient(app) as c:
        c.store = store
        yield c


def test_fetch_shows_preview_then_creates_report(web):
    page = web.post("/fetch", data={"link": LINKS["redmine"]}).text
    exp = recorded("redmine")["expect"]
    assert "redmine 에서 가져왔습니다" in page and exp["title"].replace("'", "&#39;") in page
    assert web.store.list() == []                       # 미리보기 단계에서는 접수하지 않는다
    form = {"track": "B", "reporter": "퍼블리셔 QA", "title": exp["title"], "body": exp["body"],
            "source": "redmine", "url": LINKS["redmine"]}
    r = web.post("/reports", data=form, follow_redirects=False)
    assert r.status_code == 303
    rec = web.store.list()[0]
    assert (rec["report"].source, rec["report"].url) == ("redmine", LINKS["redmine"])
    assert "원문 링크" in web.get(r.headers["location"]).text


def test_same_link_is_received_once(web):
    form = {"track": "A", "title": "t", "body": "b", "source": "board", "url": LINKS["board"]}
    first = web.post("/reports", data=form, follow_redirects=False).headers["location"]
    again = web.post("/fetch", data={"link": LINKS["board"]})
    assert again.status_code == 409 and f'href="{first}"' in again.text
    assert web.post("/reports", data=form).status_code == 409 and len(web.store.list()) == 1


def test_link_variant_counts_as_same_report(web):
    """끝의 / 나 쿼리만 다른 링크도 같은 원문 — 두 번째는 기존 리포트로 안내한다."""
    exp = recorded("jira")["expect"]
    web.post("/reports", data={"track": "B", "reporter": "내부 QA", "title": exp["title"], "body": exp["body"][:900],
                               "source": "jira", "url": LINKS["jira"]})
    r = web.post("/fetch", data={"link": LINKS["jira"] + "?focusedCommentId=1"})
    assert r.status_code == 409 and len(web.store.list()) == 1


@pytest.mark.parametrize("source, url", [
    ("board", LINKS["jira"]),                                   # 다른 출처의 링크라고 속임
    ("jira", LINKS["jira"] + "/"),                              # 맞춘 링크가 아님(중복 확인 회피)
    ("board", "https://evil.example/board/1"),                  # 설정에 없는 곳
])
def test_hidden_source_fields_must_match_real_source(web, source, url):
    r = web.post("/reports", data={"track": "A", "title": "t", "body": "b", "source": source, "url": url})
    assert r.status_code == 422 and web.store.list() == []


def test_fetch_failure_falls_back_to_paste(web):
    r = web.post("/fetch", data={"link": "https://evil.example/x"})
    assert r.status_code == 422 and "등록되지 않은 출처" in r.text and 'name="body"' in r.text
    assert "discord 꺼짐(토큰 없음(DISCORD_BOT_TOKEN))" in r.text


def test_too_long_fetched_body_must_be_trimmed(web):
    long_html = (RESP / "board_post.html").read_text(encoding="utf-8").replace(
        "</div>\n    <section", "가" * 1200 + "</div>\n    <section", 1).encode()
    srcs = sources(board_html=long_html)
    store = web.store
    app = create_app(store, Worker(store, client_factory=FakeClient), FakeTracker(store.path), start_worker=False,
                     allowed_hosts=("testserver",), sources=srcs)
    with TestClient(app) as c:
        page = c.post("/fetch", data={"link": LINKS["board"]}).text
        assert "줄여 주세요" in page
        rep = srcs[2].fetch(LINKS["board"])
        r = c.post("/reports", data={"track": "A", "title": rep.title, "body": rep.body, "source": "board",
                                     "url": LINKS["board"]})
        assert r.status_code == 422 and store.list() == []


def test_forged_source_fields_are_refused(web):
    base = {"track": "A", "title": "t", "body": "b"}
    for extra in ({"source": "ftp", "url": "https://x/y"}, {"source": "board", "url": ""},
                  {"source": "paste", "url": "https://x/y"}, {"source": "board", "url": "javascript:alert(1)"}):
        assert web.post("/reports", data={**base, **extra}).status_code == 422


def test_received_link_report_flows_to_review(web):
    """링크로 접수한 리포트도 같은 작업자·검토 흐름을 타고, 모델에는 측정한 입력 모양으로 들어간다."""
    client = FakeClient()
    rep = intake.find_source(sources(), LINKS["discord"]).fetch(LINKS["discord"])
    rid = web.store.add(rep)
    Worker(web.store, client_factory=lambda: client).process(rid)
    assert web.store.get(rid)["state"] == flow.REVIEW
    assert client.prompts[0].endswith(build_input(rep.to_item()))
