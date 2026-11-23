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
6. 자동 배정(assign.py)한 팀은 담당자(계정 ID, [jira.teams])로 넣고 "팀_<이름>" 라벨도 붙인다. 팀 관리형 공간에는
   컴포넌트가 없어 컴포넌트 기본 담당자를 쓸 수 없다(2026-11-23 KAN 공간에서 확인). 라벨로 BTS 에서 팀별로 걸러 본다.
7. 상태 읽기(snapshot)는 검색 한 번으로 여러 티켓을 가져온다. 예전 /search 는 삭제돼(HTTP 410) /search/jql 만 쓴다(2026-11-23 KAN
   에서 확인). 키는 따옴표로 감싸고, 한 번에 SYNC_CHUNK 건 이하만 받는다. 검색에 없는 키는 오류 없이 결과에서 빠진다
   (존재하지 않는 KAN-999 를 섞어 실제 KAN 에서 확인: HTTP 200, 그 키만 빠짐).
"""

import httpx2

from triage_eval.contracts import NO_MODULE, split_modules
from triage_service.bts import clients
from triage_service.bts.common import call, description_lines
from triage_service.contracts import BtsState, Issue, Ticket


class JiraTracker:
    name = "jira"

    def __init__(self, settings: dict, secrets: dict, http: httpx2.Client | None = None):
        self.s = settings
        self.http = http or clients.jira(settings["base_url"], secrets, settings["timeout_sec"])

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
        if issue.team:   # 설계 의도 6
            fields["assignee"] = {"accountId": self.s["teams"][issue.team]}
            fields["labels"].append("팀_" + issue.team.replace(" ", "_"))
        resp = call(self.http, "POST", "/rest/api/3/issue", json={"fields": fields}, creating=True)
        return self.ticket(resp.json()["key"])

    def status(self, key: str) -> str:
        resp = call(self.http, "GET", f"/rest/api/3/issue/{key}", params={"fields": "status"})
        return resp.json()["fields"]["status"]["name"]

    def snapshot(self, keys: list[str]) -> dict[str, BtsState]:   # 설계 의도 7
        listed = ", ".join(f'"{k}"' for k in keys)
        resp = call(self.http, "GET", "/rest/api/3/search/jql", params={
            "jql": f'project = "{self.s["project_key"]}" AND key in ({listed})',
            "fields": "status,assignee,updated", "maxResults": len(keys)})
        return {i["key"]: BtsState(i["fields"]["status"]["name"], (i["fields"].get("assignee") or {}).get("displayName", ""),
                                   i["fields"].get("updated", "")) for i in resp.json().get("issues", [])}

    def check_assignees(self) -> list[str]:
        """[jira.teams] 의 계정이 이 프로젝트의 담당자가 될 수 있는지 (연결 확인 --check)."""
        out = []
        for team, account in self.s.get("teams", {}).items():
            found = call(self.http, "GET", "/rest/api/3/user/assignable/search",
                         params={"project": self.s["project_key"], "accountId": account}).json()
            if not any(u.get("accountId") == account for u in found):
                out.append(f"{team}: 계정 {account} 은 {self.s['project_key']} 의 담당자가 될 수 없습니다")
        return out


def adf(lines: list[str]) -> dict:
    """설계 의도 3 — 빈 줄은 건너뛴다(빈 문단은 Jira 가 거절한다)."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines if line]}
