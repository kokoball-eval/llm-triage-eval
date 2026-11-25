"""디스코드 메시지 링크 출처 — 회사 봇이 들어가 있는 서버의 메시지를 봇 API 로 읽는다.

[설계 의도]
1. 링크는 discord.com/channels/<서버>/<채널>/<메시지> 모양만 받는다. 메시지는 봇 API(GET /channels/<채널>/messages/<메시지>)로
   읽으므로, 봇이 들어가 있지 않은 서버의 메시지는 디스코드가 거절한다(회사 봇이 설치된 서버만 읽는다 — 서비스_설계 §5).
   요청은 고정한 API 주소에 링크에서 뽑은 숫자만 붙여 보낸다.
2. 디스코드 메시지에는 제목이 없다. 첫 줄을 제목으로 쓰고(100자까지), 본문은 메시지 전체다. 입력 종류는 측정 때의
   A(커뮤니티 원문)이고, 게시판 이름은 설정의 board 값이다. 작성자 닉네임은 저장하지 않는다(측정 입력에도 없었다).
3. 봇 토큰은 .env 의 DISCORD_BOT_TOKEN 에만 있고 오류 메시지에 나오지 않는다(fetching.py).
4. ptb·canary 하위 도메인 링크도 같은 메시지이므로 discord.com 링크 하나로 맞춰 접수한다(fetching 설계 의도 4).
   게시 시각(UTC)은 현지 시각으로 바꾼다(fetching 설계 의도 5).
"""

import re
from urllib.parse import urlsplit

import httpx2

from triage_service.contracts import Report
from triage_service.intake.fetching import IntakeError, get_json, local_time

HOSTS = {"discord.com", "www.discord.com", "ptb.discord.com", "canary.discord.com"}
PATH = re.compile(r"^/channels/(\d+)/(\d+)/(\d+)/?$")
API = "https://discord.com/api/v10"


def _ids(url: str):
    u = urlsplit(url)
    if u.scheme.lower() != "https" or u.netloc.lower() not in HOSTS:
        return None
    m = PATH.match(u.path)
    return m.groups() if m else None


class DiscordSource:
    name = "discord"

    def __init__(self, board: str, token: str, fetch: dict, http: httpx2.Client | None = None):
        self.board, self.max_bytes = board, fetch["max_bytes"]
        self.http = http or httpx2.Client(base_url=API, timeout=fetch["timeout_sec"],
                                          headers={"Authorization": f"Bot {token}", "Accept": "application/json"})

    def matches(self, url: str) -> bool:
        return _ids(url) is not None

    def canonical(self, url: str) -> str:
        return "https://discord.com/channels/" + "/".join(_ids(url))

    def fetch(self, url: str) -> Report:
        _guild, channel, message = _ids(url)
        msg = get_json(self.http, f"/channels/{channel}/messages/{message}", self.max_bytes)
        content = (msg.get("content") if isinstance(msg.get("content"), str) else "").replace("\r\n", "\n").strip()
        if not content:
            raise IntakeError("메시지에 글이 없습니다(이미지·첨부만 있는 메시지는 붙여넣기로 넣어 주세요)")
        return Report(track="A", title=content.split("\n", 1)[0][:100], body=content, board=self.board,
                      posted_at=local_time(msg.get("timestamp") or ""), source=self.name, url=self.canonical(url))
