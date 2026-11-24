"""Redmine 어댑터 — REST API (JSON).

[설계 의도]
1. 계약(contracts.Tracker)만 구현한다. 프로젝트·유형·우선순위 번호는 설정/bts_매핑.toml 이 정한다.
2. 8칸과 원문은 설명(description)에 넣고, 우선순위는 Redmine 필드로 옮긴다. 팀·모듈은 설계 의도 8·9.
3. 서비스 표식은 설명 마지막 줄에 넣고, 등록하기 전에 설명 검색(description ~ 표식)으로 기존 티켓을 찾는다.
   "~"는 포함 검색이라 triage-x-1 로 찾으면 triage-x-12 도 걸린다. 그래서 돌려받은 티켓마다 설명의 표식 줄이 정확히
   같은지 다시 확인하고, 같은 것만 같은 리포트의 티켓으로 본다. 필터(f[])를 주면 Redmine 은 상태 조건을 붙이지 않아
   닫힌 티켓도 함께 찾는다.
4. 인증은 API 키 머리글(X-Redmine-API-Key). 키는 httpx2 클라이언트 안에만 있고 오류 메시지에는 나오지 않는다(common.py).
5. 자동 배정(assign.py)한 팀은 담당자 번호(assigned_to_id, [redmine.teams])로 넣는다. 사용자 번호도, 그룹 번호도 된다
   (그룹은 Redmine 관리 설정 "그룹에 일감 할당 허용"이 켜져 있어야 한다). 담당자는 프로젝트 구성원이어야 한다.
   연결 확인은 구성원 목록을 끝 페이지까지 읽어 구성원인지만 본다. 목록을 읽으려면 API 키에 구성원 관리 권한이 필요하고,
   그 구성원의 역할이 "일감 할당 가능"인지는 보지 않는다(shortcut: 역할 확인 없음, 실제 Redmine 으로 다시 확인할 때 올림).
6. 상태 읽기(snapshot)는 목록 한 번(issue_id 쉼표 목록)으로 가져온다. status_id=* 를 주지 않으면 닫힌 티켓이 빠진다.
   응답 모양은 실제 Redmine 6.0(로컬 Docker, 2026-11-24 다시 띄움)에서 녹화한 응답으로 확인한다.
7. 쓰기(update)는 PUT 한 번에 상태·담당자·코멘트(notes)를 함께 보낸다. Redmine 은 상태를 번호로 받으므로 상태 목록
   (/issue_statuses.json)에서 이름으로 번호를 찾는다. 상태 이름은 관리자가 만든 그대로라 번역되지 않는다.
8. Redmine 에는 라벨이 없어 Jira 의 "팀_<이름>" 라벨 대신 팀 이름과 같은 일감 범주(category)를 넣는다. 그래야 Redmine 에서도
   팀별로 걸러 볼 수 있다(두 BTS 의 차이 줄이기, S5-4a). 범주 번호는 이름으로 찾아서 설정이 늘지 않는다. 범주가 없으면
   등록·재배정이 실패하므로 연결 확인(check_assignees)이 먼저 본다.
9. 모듈도 Jira 의 "모듈_" 라벨 대신 여러 값을 받는 사용자 정의 필드에 넣는다. 필드는 관리자가 만들어야 해서 번호를 설정
   (module_field_id)에 두고, 없으면 지금처럼 설명에만 남긴다. 필드의 선택지는 8칸 모듈 이름과 같아야 한다(선택지에 없는 값은
   Redmine 이 등록을 거절해 실패로 드러난다). 번호가 틀렸거나 그 트래커에서 쓰지 않는 필드면 Redmine 이 조용히 무시해 모듈이
   설명에만 남는다. 필드 정의는 관리자 권한으로만 읽을 수 있어 --check 가 확인하지 않는다(shortcut: 번호는 사람이 맞춘다,
   여러 Redmine 에 붙일 때 관리자 키로 확인하는 검사를 올림).
"""

import httpx2

from triage_eval.contracts import NO_MODULE, split_modules
from triage_service.bts import clients
from triage_service.bts.common import call, description_lines, marker_line
from triage_service.contracts import STATUSES, BtsState, Issue, Ticket, TrackerError


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
        if issue.team:   # 설계 의도 5·8
            body["issue"]["assigned_to_id"] = self.s["teams"][issue.team]
            body["issue"]["category_id"] = self.category(issue.team)
        modules = [m for m in split_modules(issue.fields.get("모듈", "")) if m != NO_MODULE]
        if modules and self.s.get("module_field_id"):   # 설계 의도 9
            body["issue"]["custom_fields"] = [{"id": self.s["module_field_id"], "value": modules}]
        resp = call(self.http, "POST", "/issues.json", json=body, creating=True)
        return self.ticket(resp.json()["issue"]["id"])

    def status(self, key: str) -> str:
        return call(self.http, "GET", f"/issues/{key}.json").json()["issue"]["status"]["name"]

    def snapshot(self, keys: list[str]) -> dict[str, BtsState]:   # 설계 의도 6
        resp = call(self.http, "GET", "/issues.json", params={"issue_id": ",".join(keys), "status_id": "*", "limit": len(keys)})
        return {str(i["id"]): BtsState(i["status"]["name"], (i.get("assigned_to") or {}).get("name", ""), i.get("updated_on", ""))
                for i in resp.json().get("issues", [])}

    def statuses(self) -> dict[str, int]:
        return {s["name"]: s["id"] for s in call(self.http, "GET", "/issue_statuses.json").json()["issue_statuses"]}

    def update(self, key: str, status: str = "", team: str = "", comment: str = "") -> None:   # 설계 의도 7
        body = {}
        if status:
            ids = self.statuses()
            if status not in ids:
                raise TrackerError(f"Redmine 에 {status} 상태가 없습니다")
            body["status_id"] = ids[status]
        if team:
            body["assigned_to_id"] = self.s["teams"][team]
            body["category_id"] = self.category(team)
        if comment:
            body["notes"] = comment
        call(self.http, "PUT", f"/issues/{key}.json", json={"issue": body})

    def categories(self) -> dict[str, int]:
        resp = call(self.http, "GET", f"/projects/{self.s['project_id']}/issue_categories.json")
        return {c["name"]: c["id"] for c in resp.json()["issue_categories"]}

    def category(self, team: str) -> int:
        """설계 의도 8 — 팀 이름과 같은 일감 범주 번호."""
        found = self.categories().get(team)
        if found is None:
            raise TrackerError(f"Redmine {self.s['project_id']} 프로젝트에 팀 범주 {team} 가 없습니다")
        return found

    def check_statuses(self) -> list[str]:
        have = self.statuses()
        return [f"Redmine 에 {s} 상태가 없습니다" for s in STATUSES if s not in have]

    def check_assignees(self) -> list[str]:
        """[redmine.teams] 의 번호가 이 프로젝트의 구성원(사용자·그룹)인지 (연결 확인 --check, 설계 의도 5)."""
        teams = self.s.get("teams", {})
        if not teams:
            return []
        members, offset, total = set(), 0, 1
        while offset < total:   # 설계 의도 5 — 한 번에 100명까지라 끝 페이지까지 읽는다
            try:
                page = call(self.http, "GET", f"/projects/{self.s['project_id']}/memberships.json",
                            params={"limit": 100, "offset": offset}).json()
            except TrackerError as e:
                raise TrackerError(f"구성원 목록을 읽을 수 없습니다(API 키에 구성원 관리 권한이 필요): {e}") from e
            batch = page.get("memberships", [])
            members |= {m[k]["id"] for m in batch for k in ("user", "group") if k in m}
            offset, total = offset + len(batch), page.get("total_count", 0) if batch else 0
        have = self.categories()   # 설계 의도 8
        return ([f"{team}: 번호 {n} 은 {self.s['project_id']} 프로젝트의 구성원이 아닙니다"
                 for team, n in teams.items() if n not in members]
                + [f"{team}: {self.s['project_id']} 프로젝트에 같은 이름의 일감 범주가 없습니다" for team in teams if team not in have])
