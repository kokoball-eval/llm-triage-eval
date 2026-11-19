"""Jira Cloud 어댑터 — REST API v3.

[설계 의도]
1. 계약(contracts.Tracker)만 구현한다: find(표식) → 기존 티켓, create(Issue) → 새 티켓, status(키) → 상태 이름.
   어느 필드에 무엇을 넣을지(프로젝트·유형·우선순위 이름·라벨)는 설정/bts_매핑.toml 이 정한다.
2. 모듈은 컴포넌트가 아니라 라벨로 붙인다(공백은 _, "해당 없음"은 붙이지 않음). 컴포넌트는 프로젝트마다 관리자가 미리 만들어야 해서, 처음
   연결할 때 실패 지점이 하나 더 생긴다. 서비스 표식(triage-…)도 라벨로 붙여 JQL 로 찾는다. JQL 의 labels = 는
   라벨 전체가 같아야 맞으므로 triage-x-1 이 triage-x-12 에 걸리지 않는다.
3. 본문은 Jira 문서 형식(ADF)이다. v3 는 일반 문자열 본문을 받지 않는다. 줄마다 문단 하나로 넣는다.
4. 인증은 이메일 + API 토큰(기본 인증). 토큰은 httpx2 클라이언트 안에만 있고 오류 메시지에는 나오지 않는다(common.py).
5. 한계: Jira 검색은 등록 직후 바로 반영되지 않을 수 있다(검색 색인 지연). 등록 요청이 끊긴 직후 곧바로 다시 등록하면
   방금 만들어진 티켓을 못 찾아 중복될 수 있으므로, "등록 여부 불확실" 안내대로 Jira 에서 먼저 확인한다.
"""

import httpx2

from triage_eval.contracts import NO_MODULE, split_modules
from triage_service.bts.common import call, description_lines
from triage_service.contracts import Issue, Ticket


class JiraTracker:
    name = "jira"

    def __init__(self, settings: dict, secrets: dict, http: httpx2.Client | None = None):
        self.s = settings
        self.http = http or httpx2.Client(base_url=settings["base_url"], timeout=settings["timeout_sec"],
                                         auth=(secrets["JIRA_EMAIL"], secrets["JIRA_API_TOKEN"]),
                                         headers={"Accept": "application/json"})

    def ticket(self, key: str) -> Ticket:
        return Ticket(key=key, url=f"{self.s['base_url']}/browse/{key}")

    def find(self, ref: str) -> Ticket | None:
        jql = f'project = "{self.s["project_key"]}" AND labels = "{ref}"'
        resp = call(self.http, "GET", "/rest/api/3/search/jql", params={"jql": jql, "fields": "key", "maxResults": 2})
        issues = resp.json().get("issues", [])
        return self.ticket(issues[0]["key"]) if issues else None

    def create(self, issue: Issue) -> Ticket:
        modules = [m.replace(" ", "_") for m in split_modules(issue.fields.get("모듈", "")) if m != NO_MODULE]
        fields = {
            "project": {"key": self.s["project_key"]},
            "issuetype": {"name": self.s["issue_type"]},
            "summary": issue.fields["요약"][:255],
            "description": adf(description_lines(issue)),
            "priority": {"name": self.s["priority"][issue.fields["우선순위"]]},
            "labels": [*self.s["labels"], issue.ref, *(f"모듈_{m}" for m in modules)],
        }
        resp = call(self.http, "POST", "/rest/api/3/issue", json={"fields": fields}, creating=True)
        return self.ticket(resp.json()["key"])

    def status(self, key: str) -> str:
        resp = call(self.http, "GET", f"/rest/api/3/issue/{key}", params={"fields": "status"})
        return resp.json()["fields"]["status"]["name"]


def adf(lines: list[str]) -> dict:
    """설계 의도 3 — 빈 줄은 건너뛴다(빈 문단은 Jira 가 거절한다)."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines if line]}
