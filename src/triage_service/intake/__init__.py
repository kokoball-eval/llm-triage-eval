"""출처 어댑터 — 링크를 받아 Report 를 만든다(contracts.Source). 링크가 없는 리포트는 붙여넣기(paste.py)로 넣는다.

    bts.py      Jira·Redmine 이슈 링크 (입력 종류 B · QA 이슈)
    board.py    게시판 글 링크 (입력 종류 A, 사이트별 선택자는 설정/출처.toml)
    discord.py  디스코드 메시지 링크 (입력 종류 A, 회사 봇이 들어간 서버만)
    fetching.py 허용한 곳만·정해 둔 크기 안에서 받기(리다이렉트 안 따라감)
    settings.py 설정/출처.toml 읽기·검사
"""

from triage_service.intake.board import BoardSource
from triage_service.intake.bts import JiraSource, RedmineSource
from triage_service.intake.discord import DiscordSource
from triage_service.intake.fetching import IntakeError, clean_link
from triage_service.intake.paste import SOURCE_NAMES


def make_sources(loaded: dict, http: dict | None = None) -> list:
    """loaded: settings.load() 결과. http 는 시험에서 출처 이름 → 가짜 전송 클라이언트를 넣을 때 쓴다."""
    http = http or {}
    f = loaded["fetch"]
    sources = []
    if loaded["jira"]:
        j = loaded["jira"]
        sources.append(JiraSource(j["base_url"], j["secrets"], j["reporter"], f, http.get("jira")))
    if loaded["redmine"]:
        r = loaded["redmine"]
        sources.append(RedmineSource(r["base_url"], r["secrets"], r["reporter"], f, http.get("redmine")))
    sources += [BoardSource(b, f, http.get("board")) for b in loaded["boards"]]
    if loaded["discord"]:
        sources.append(DiscordSource(loaded["discord"]["board"], loaded["discord"]["token"], f, http.get("discord")))
    return sources


def find_source(sources: list, url: str):
    """링크를 맡을 출처. 없으면 IntakeError — 설정에 없는 곳은 부르지 않는다(fetching 설계 의도 1)."""
    url = clean_link(url)
    for s in sources:
        if s.matches(url):
            return s
    raise IntakeError("등록되지 않은 출처의 링크입니다(설정/출처.toml 에 있는 사이트만 가져옵니다). 본문을 붙여 넣어 주세요")


def identify(sources: list, url: str) -> tuple:
    """(출처, 하나로 맞춘 링크). 중복 접수는 맞춘 링크로 확인한다(fetching 설계 의도 4)."""
    url = clean_link(url)
    source = find_source(sources, url)
    return source, source.canonical(url)


__all__ = ["IntakeError", "SOURCE_NAMES", "make_sources", "find_source", "identify"]
