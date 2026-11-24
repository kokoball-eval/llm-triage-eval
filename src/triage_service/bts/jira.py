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
8. Jira 는 공용 상태 "Closed" 를 계정 언어로 번역해 돌려준다(한국어 계정에서 "종료", 2026-11-24 KAN 에서 확인). 한국어 화면을
   유지하려고 이름 대신 상태 범주를 본다: 완료 범주(statusCategory "done")인 상태는 CLOSED 로 읽고, CLOSED 로 보낼 때도 완료
   범주로 가는 전환을 고른다. 그래서 워크플로에 완료 범주 상태는 하나만 둬야 한다(--check 가 확인). 나머지 넷은 직접 만든
   상태라 번역되지 않는다.
9. 쓰기(update)는 담당자 → 상태 → 코멘트 순서로 요청을 나눠 보낸다(Jira 는 한 요청으로 묶을 수 없다). 상태를 바꾼 뒤 코멘트가
   실패하면 상태만 바뀐 채 남는다. 이를 숨기지 않도록 qa.py 가 쓴 뒤 상태를 다시 읽어 화면에 보인다. 상태는 전환 목록에서
   목표 상태로 가는 전환을 찾아 보낸다(전환 번호는 워크플로마다 달라 설정에 두지 않는다). 팀을 바꿀 때는 다른 팀 라벨을 지우고
   새 팀 라벨을 붙인다(설계 의도 6).
10. Jira 검색은 색인이라 방금 바뀐 티켓이 늦게 보일 수 있다. 검색에 reconcileIssues(이슈 숫자 ID)를 주면 그 이슈는 최신 값으로
   읽는다(2026-11-24 KAN 에서 받아들여짐을 확인). Redmine 은 목록 조회가 DB 를 바로 읽어 지연이 없으므로, 이것으로 두 BTS 의
   읽기가 같아진다. 숫자 ID 는 등록·단건 조회·검색 응답에서 얻어 어댑터 안에 기억한다(표에 칸을 더하지 않는다). 서버를 다시
   켜면 비지만, 다음 동기화 검색 결과로 다시 채워진다.
"""

import httpx2

from triage_eval.contracts import NO_MODULE, split_modules
from triage_service.bts import clients
from triage_service.bts.common import call, description_lines
from triage_service.contracts import CLOSED, STATUSES, BtsState, Issue, Ticket, TrackerError


class JiraTracker:
    name = "jira"

    def __init__(self, settings: dict, secrets: dict, http: httpx2.Client | None = None):
        self.s = settings
        self.http = http or clients.jira(settings["base_url"], secrets, settings["timeout_sec"])
        self.ids: dict[str, str] = {}   # 티켓 키 → 숫자 ID (설계 의도 10)

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
            fields["labels"].append(team_label(issue.team))
        resp = call(self.http, "POST", "/rest/api/3/issue", json={"fields": fields}, creating=True).json()
        self.ids[resp["key"]] = resp["id"]
        return self.ticket(resp["key"])

    def status(self, key: str) -> str:
        resp = call(self.http, "GET", f"/rest/api/3/issue/{key}", params={"fields": "status"}).json()
        self.ids[key] = resp["id"]
        return name(resp["fields"]["status"])

    def snapshot(self, keys: list[str]) -> dict[str, BtsState]:   # 설계 의도 7
        listed = ", ".join(f'"{k}"' for k in keys)
        known = [self.ids[k] for k in keys if k in self.ids]   # 설계 의도 10
        resp = call(self.http, "GET", "/rest/api/3/search/jql", params={
            "jql": f'project = "{self.s["project_key"]}" AND key in ({listed})',
            "fields": "status,assignee,updated", "maxResults": len(keys), **({"reconcileIssues": known} if known else {})})
        issues = resp.json().get("issues", [])
        self.ids.update({i["key"]: i["id"] for i in issues})
        return {i["key"]: BtsState(name(i["fields"]["status"]), (i["fields"].get("assignee") or {}).get("displayName", ""),
                                   i["fields"].get("updated", "")) for i in issues}

    def update(self, key: str, status: str = "", team: str = "", comment: str = "") -> None:   # 설계 의도 9
        if team:
            others = [{"remove": team_label(t)} for t in self.s["teams"] if t != team]
            call(self.http, "PUT", f"/rest/api/3/issue/{key}", json={
                "fields": {"assignee": {"accountId": self.s["teams"][team]}},
                "update": {"labels": [*others, {"add": team_label(team)}]}})
        if status:
            moves = call(self.http, "GET", f"/rest/api/3/issue/{key}/transitions").json()["transitions"]
            move = next((t for t in moves if name(t["to"]) == status), None)
            if move is None:
                raise TrackerError(f"{key} 에서 {status} 로 가는 전환이 없습니다 (Jira 워크플로를 확인하세요)")
            call(self.http, "POST", f"/rest/api/3/issue/{key}/transitions", json={"transition": {"id": move["id"]}})
        if comment:
            call(self.http, "POST", f"/rest/api/3/issue/{key}/comment", json={"body": adf(comment.splitlines())})

    def check_statuses(self) -> list[str]:
        """설계 의도 8 — 프로젝트 워크플로에 다섯 상태가 있고 완료 범주는 하나인지.
        shortcut: 이슈 유형별이 아니라 모든 유형의 상태를 합쳐 본다(유형 이름도 언어에 따라 달라서). 유형마다 워크플로가 다른 공간을 쓰면 올림."""
        types = call(self.http, "GET", f"/rest/api/3/project/{self.s['project_key']}/statuses").json()
        found = {s["id"]: s for t in types for s in t["statuses"]}.values()
        out = [f"Jira 에 {s} 상태가 없습니다" for s in STATUSES if s not in {name(x) for x in found}]
        done = [x["name"] for x in found if name(x) == CLOSED]
        if len(done) > 1:
            out.append(f"완료 범주 상태가 둘 이상입니다: {', '.join(done)} (Closed 하나만 둡니다)")
        return out

    def check_assignees(self) -> list[str]:
        """[jira.teams] 의 계정이 이 프로젝트의 담당자가 될 수 있는지 (연결 확인 --check)."""
        out = []
        for team, account in self.s.get("teams", {}).items():
            found = call(self.http, "GET", "/rest/api/3/user/assignable/search",
                         params={"project": self.s["project_key"], "accountId": account}).json()
            if not any(u.get("accountId") == account for u in found):
                out.append(f"{team}: 계정 {account} 은 {self.s['project_key']} 의 담당자가 될 수 없습니다")
        return out


def name(status: dict) -> str:
    """설계 의도 8 — 완료 범주는 CLOSED, 나머지는 이름 그대로."""
    return CLOSED if (status.get("statusCategory") or {}).get("key") == "done" else status["name"]


def team_label(team: str) -> str:
    return "팀_" + team.replace(" ", "_")


def adf(lines: list[str]) -> dict:
    """설계 의도 3 — 빈 줄은 건너뛴다(빈 문단은 Jira 가 거절한다)."""
    return {"type": "doc", "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines if line]}
