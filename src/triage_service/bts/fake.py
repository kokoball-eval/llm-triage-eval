"""가짜 BTS — 같은 SQLite 파일의 별도 표에 티켓을 저장한다. 실제 BTS 없이 등록 흐름을 끝까지 돌리고 시험하는 용도다.

[설계 의도]
1. Tracker 계약(contracts.py)을 실제 어댑터와 똑같이 지킨다: 등록하면 티켓 번호·링크를 돌려주고, 실패하면 예외를 올린다.
   S2 의 Jira·Redmine 어댑터도 같은 계약 시험을 통과해야 한다.
2. 등록 내용(8칸과 원문)을 그대로 저장해, 화면에서 "BTS 에 실제로 무엇이 들어갔는지" 확인할 수 있게 한다.
3. set_status 는 BTS 쪽에서 상태가 바뀐 것을 흉내 낸다(시험·화면 확인용). 마지막 변경 시각은 등록 내용 JSON 의 updated 에 둔다 —
   표에 칸을 더하면 이미 만든 DB 를 고쳐야 해서다.
4. 새 티켓은 QA Review 로 만든다(실제 Jira·Redmine 프로젝트의 첫 상태와 같다). update 는 상태·팀을 바꾸고 코멘트를 등록
   내용 JSON 의 comments 목록에 쌓는다. 전환 규칙은 없다(실제 BTS 의 워크플로도 다섯 상태끼리 모두 오갈 수 있게 뒀다).
5. 버전(마일스톤)은 별도 표 fake_versions 에 두고 add_version 으로 만든다(시험·화면 확인용). 티켓의 마일스톤은 등록 내용 JSON 의
   milestone 이다.
6. 팀 그룹은 팀마다 "<팀>팀장"·"<팀>팀원" 두 사람이다(구성원 ID = 이름). 토큰 주인(me)은 QA팀장이다. 미해결 수는 그 사람이
   담당인 Closed 아닌 티켓 수다. 자동 배정(미해결이 적은 사람)과 팀장 사인을 실제 BTS 없이 끝까지 돌려 보는 용도다.
"""

import json
from dataclasses import asdict
from pathlib import Path

from triage_service.contracts import CLOSED, QA_REVIEW, BtsState, BtsTicket, Issue, Member, Milestone, Ticket, TrackerError
from triage_service.store import connect, now


class FakeTracker:
    name = "fake"

    def __init__(self, path: Path):
        self.path = Path(path)
        with connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS fake_tickets (n INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "created_at TEXT NOT NULL, status TEXT NOT NULL, issue TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS fake_versions (name TEXT PRIMARY KEY, release TEXT NOT NULL, released INTEGER NOT NULL)")

    def create(self, issue: Issue) -> Ticket:
        if issue.milestone and issue.milestone not in {v.name for v in self.versions()}:
            raise TrackerError(f"버전이 없습니다: {issue.milestone}")
        payload = json.dumps({"report": asdict(issue.report), "fields": issue.fields, "ref": issue.ref, "team": issue.team,
                              "milestone": issue.milestone, "assignee": issue.assignee, "sprint": issue.sprint}, ensure_ascii=False)
        with connect(self.path) as c:
            n = c.execute("INSERT INTO fake_tickets (created_at, status, issue) VALUES (?, ?, ?)",
                          (now(), QA_REVIEW, payload)).lastrowid
        key = f"FAKE-{n}"
        return Ticket(key=key, url=f"/fake-bts/{key}")

    def find(self, ref: str) -> Ticket | None:
        if not ref:
            return None
        with connect(self.path) as c:
            rows = c.execute("SELECT n, issue FROM fake_tickets ORDER BY n").fetchall()
        n = next((r["n"] for r in rows if json.loads(r["issue"]).get("ref") == ref), None)
        return Ticket(key=f"FAKE-{n}", url=f"/fake-bts/FAKE-{n}") if n else None

    def status(self, key: str) -> str:
        return self.get(key)["status"]

    def check_assignees(self) -> list[str]:
        return []   # 설계 의도 6 — 그룹이 늘 있다

    def members(self, team: str) -> list[Member]:   # 설계 의도 6
        return [Member(f"{team}팀장", f"{team}팀장"), Member(f"{team}팀원", f"{team}팀원")]

    def lead(self, team: str) -> str:
        return f"{team}팀장"

    def open_count(self, member: str) -> int:
        with connect(self.path) as c:
            rows = c.execute("SELECT status, issue FROM fake_tickets").fetchall()
        return sum(r["status"] != CLOSED and json.loads(r["issue"]).get("assignee") == member for r in rows)

    def me(self) -> str:
        return "QA팀장"

    def snapshot(self, keys: list[str]) -> dict[str, BtsState]:
        out = {}
        for key in keys:
            try:
                t = self.get(key)
            except TrackerError:   # 지워진 티켓은 돌려주지 않는다(실제 BTS 의 검색과 같다)
                continue
            out[key] = BtsState(t["status"], t.get("assignee") or t.get("team", ""), t.get("updated", t["created_at"]))
        return out

    def set_status(self, key: str, status: str) -> None:   # 설계 의도 3
        self.update(key, status)

    def update(self, key: str, status: str = "", team: str = "", comment: str = "", assignee: str = "") -> None:   # 설계 의도 4
        t = self.get(key)
        issue = {k: v for k, v in t.items() if k not in ("key", "created_at", "status")} | {"updated": now()}
        if team:
            issue["team"] = team
        if assignee:
            issue["assignee"] = assignee
        if comment:
            issue["comments"] = [*t.get("comments", []), comment]
        with connect(self.path) as c:
            c.execute("UPDATE fake_tickets SET status = ?, issue = ? WHERE n = ?",
                      (status or t["status"], json.dumps(issue, ensure_ascii=False), int(key.removeprefix("FAKE-"))))

    def check_statuses(self) -> list[str]:
        return []

    def add_version(self, name: str, release: str = "", released: bool = False) -> None:   # 설계 의도 5
        with connect(self.path) as c:
            c.execute("INSERT OR REPLACE INTO fake_versions (name, release, released) VALUES (?, ?, ?)", (name, release, int(released)))

    def versions(self) -> list[Milestone]:
        with connect(self.path) as c:
            return [Milestone(r["name"], r["release"], bool(r["released"])) for r in c.execute("SELECT * FROM fake_versions ORDER BY release")]

    def milestone_tickets(self, name: str) -> list[BtsTicket]:
        with connect(self.path) as c:
            keys = [f"FAKE-{r['n']}" for r in c.execute("SELECT n, issue FROM fake_tickets ORDER BY n") if json.loads(r["issue"]).get("milestone") == name]
        out = []
        for key in keys:
            t = self.get(key)
            out.append(BtsTicket(key, t["fields"].get("요약", ""), BtsState(t["status"], t.get("assignee") or t.get("team", ""), t.get("updated", t["created_at"])), t.get("team", ""), t.get("sprint", "")))
        return out

    def get(self, key: str) -> dict:
        n = int(key.removeprefix("FAKE-")) if key.startswith("FAKE-") and key[5:].isdigit() else -1
        with connect(self.path) as c:
            row = c.execute("SELECT * FROM fake_tickets WHERE n = ?", (n,)).fetchone()
        if row is None:
            raise TrackerError(f"티켓이 없습니다: {key}")
        return {"key": key, "created_at": row["created_at"], "status": row["status"], **json.loads(row["issue"])}
