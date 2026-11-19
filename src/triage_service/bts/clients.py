"""BTS 접속 — Jira·Redmine HTTP 클라이언트를 만드는 곳은 여기 하나다.

[설계 의도]
1. 같은 BTS 에 등록(bts/jira.py·redmine.py)과 이슈 가져오기(intake/bts.py)가 함께 접속한다. 인증·머리글을 만드는 코드가
   두 벌이면 토큰을 다루는 곳도 둘이 되고, 한쪽만 바뀌어 어긋나기 쉽다. 그래서 토큰은 이 파일에서만 머리글로 바뀐다.
2. 클라이언트에는 주소·시간 제한·인증만 넣는다. 리다이렉트 처리·응답 크기 상한 같은 규칙은 호출하는 쪽의 것이다
   (등록: bts/common.py, 가져오기: intake/fetching.py).
"""

import httpx2


def jira(base_url: str, secrets: dict, timeout: float) -> httpx2.Client:
    return httpx2.Client(base_url=base_url, timeout=timeout, auth=(secrets["JIRA_EMAIL"], secrets["JIRA_API_TOKEN"]),
                         headers={"Accept": "application/json"})


def redmine(base_url: str, secrets: dict, timeout: float) -> httpx2.Client:
    return httpx2.Client(base_url=base_url, timeout=timeout,
                         headers={"X-Redmine-API-Key": secrets["REDMINE_API_KEY"], "Accept": "application/json"})
