"""게시판 글 링크 출처 — HTML 을 받아 설정의 CSS 선택자로 제목·본문·작성 시각을 꺼낸다.

[설계 의도]
1. 사이트마다 HTML 구조가 달라 어디에 제목·본문이 있는지는 설정/출처.toml 의 선택자로 정한다. 새 게시판은 설정 한 덩어리를
   추가하면 붙고 코드는 고치지 않는다.
2. 선택자로 찾지 못하면 "구조가 설정과 다르다"고 실패한다. 페이지 전체 글자 같은 대체 내용을 만들지 않는다. 사이트가
   개편되면 엉뚱한 글(광고·댓글)을 본문으로 가져오기보다 멈추는 편이 낫다. 가져온 내용은 미리보기에서 사람이 한 번 더 본다.
3. 링크는 설정한 호스트[:포트]와 경로 앞부분(path_prefix)이 맞을 때만 맡는다. 경로에 ".." 이나 인코딩한 점·빗금(%2e·%2f)이
   있으면 거절한다. HTTP 라이브러리가 ".."을 정리해 경로 앞부분 밖으로 요청이 나가는 것을 막기 위해서다. 받을 때는
   리다이렉트를 따라가지 않는다(fetching.py). 작성자 닉네임은 꺼내지 않는다.
4. 본문 글자는 문단(p·div·li·br 등)에서만 줄을 나눈다. 굵게·링크 같은 글자 안 태그마다 줄을 나누면 측정한 입력에 없던
   모양("Game\\ncrashes\\non")이 모델에 들어간다.
5. 작성 시각은 datetime 속성(ISO 8601)을 현지 시각으로 바꿔 쓴다. 속성이 없거나 읽을 수 없으면 가져온 시각을 쓴다.
"""

import re
from datetime import datetime
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx2

from triage_service.contracts import Report
from triage_service.intake.fetching import IntakeError, get, local_time

BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "pre", "blockquote", "section",
              "article", "table"}


class BoardSource:
    name = "board"

    def __init__(self, cfg: dict, fetch: dict, http: httpx2.Client | None = None):
        self.cfg, self.max_bytes = cfg, fetch["max_bytes"]
        self.http = http or httpx2.Client(timeout=fetch["timeout_sec"], headers={"Accept": "text/html"})

    def matches(self, url: str) -> bool:
        u = urlsplit(url)
        if u.scheme.lower() not in ("http", "https") or u.netloc.lower() != self.cfg["domain"].lower():
            return False
        decoded = unquote(u.path)
        if ".." in decoded.split("/") or "%2f" in u.path.lower() or "%2e" in u.path.lower() or "\\" in decoded:
            return False   # 설계 의도 3
        return u.path.startswith(self.cfg["path_prefix"])

    def canonical(self, url: str) -> str:
        """스킴·호스트는 소문자로, 끝의 #조각은 버린다. 게시판은 쿼리로 글을 고르기도 해서(?no=1024) 쿼리는 남긴다."""
        u = urlsplit(url)
        return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path, u.query, ""))

    def fetch(self, url: str) -> Report:
        from bs4 import BeautifulSoup   # 게시판 출처를 쓸 때만 필요하다

        soup = BeautifulSoup(get(self.http, self.canonical(url), self.max_bytes), "html.parser")

        def pick(key: str):
            el = soup.select_one(self.cfg[key]) if self.cfg.get(key) else None
            if el is None and key != "posted_at":
                raise IntakeError(f"게시판 구조가 설정과 다릅니다: {key} 선택자({self.cfg[key]})로 찾지 못함")
            return el

        title, body, posted = pick("title"), pick("body"), pick("posted_at")
        when = local_time(posted.get("datetime", "")) if posted is not None else ""
        return Report(track="A", title=" ".join(text_of(title).split()), body=text_of(body), board=self.cfg["board"],
                      posted_at=when or datetime.now().strftime("%Y-%m-%d %H:%M"), source=self.name,
                      url=self.canonical(url))


def text_of(el) -> str:
    """설계 의도 4 — 문단 단위로만 줄을 나눈 글자."""
    for br in el.find_all("br"):
        br.replace_with("\n")
    for block in el.find_all(BLOCK_TAGS):
        block.insert_after("\n")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in el.get_text().split("\n")]
    return "\n".join(line for line in lines if line)
