"""Redmine 어댑터 — REST API (JSON).

[설계 의도]
1. 계약(contracts.Tracker)만 구현한다. 프로젝트·유형·우선순위 번호는 설정/bts_매핑.toml 이 정한다.
2. 8칸과 원문은 설명(description)에 넣고, 우선순위만 Redmine 필드로 옮긴다. 모듈 같은 값을 사용자 정의 필드로 옮기려면
   Redmine 관리자가 필드를 먼저 만들어야 해서, 처음 연결할 때는 설명만으로 시작한다.
3. 서비스 표식은 설명 마지막 줄에 넣고, 등록하기 전에 설명 검색(description ~ 표식)으로 기존 티켓을 찾는다.
   "~"는 포함 검색이라 triage-x-1 로 찾으면 triage-x-12 도 걸린다. 그래서 돌려받은 티켓마다 설명의 표식 줄이 정확히
   같은지 다시 확인하고, 같은 것만 같은 리포트의 티켓으로 본다. 필터(f[])를 주면 Redmine 은 상태 조건을 붙이지 않아
   닫힌 티켓도 함께 찾는다.
4. 인증은 API 키 머리글(X-Redmine-API-Key). 키는 httpx2 클라이언트 안에만 있고 오류 메시지에는 나오지 않는다(common.py).
"""

import httpx2

from triage_service.bts import clients
from triage_service.bts.common import call, description_lines, marker_line
from triage_service.contracts import Issue, Ticket


class RedmineTracker:
    name = "redmine"

    def __init__(self, settings: dict, secrets: dict, http: httpx2.Client | None = None):
        self.s = settings
        self.http = http or clients.redmine(settings["base_url"], secrets, settings["timeout_sec"])

    def ticket(self, issue_id) -> Ticket:
        return Ticket(key=str(issue_id), url=f"{self.s['base_url']}/issues/{issue_id}")

    def find(self, ref: str) -> Ticket | None:
        params = [("project_id", self.s["project_id"]), ("set_filter", "1"),
                  ("f[]", "description"), ("op[description]", "~"), ("v[description][]", ref), ("limit", "100")]
        issues = call(self.http, "GET", "/issues.json", params=params).json().get("issues", [])
        line = marker_line(ref)
        exact = [i for i in issues if line in (i.get("description") or "").splitlines()]
        return self.ticket(exact[0]["id"]) if exact else None

    def create(self, issue: Issue) -> Ticket:
        body = {"issue": {
            "project_id": self.s["project_id"],
            "tracker_id": self.s["tracker_id"],
            "subject": issue.fields["요약"][:255],
            "description": "\n".join(description_lines(issue)),
            "priority_id": self.s["priority"][issue.fields["우선순위"]],
        }}
        resp = call(self.http, "POST", "/issues.json", json=body, creating=True)
        return self.ticket(resp.json()["issue"]["id"])

    def status(self, key: str) -> str:
        return call(self.http, "GET", f"/issues/{key}.json").json()["issue"]["status"]["name"]
