"""가짜 BTS — 같은 SQLite 파일의 별도 표에 티켓을 저장한다. 실제 BTS 없이 등록 흐름을 끝까지 돌리고 시험하는 용도다.

[설계 의도]
1. Tracker 계약(contracts.py)을 실제 어댑터와 똑같이 지킨다: 등록하면 티켓 번호·링크를 돌려주고, 실패하면 예외를 올린다.
   S2 의 Jira·Redmine 어댑터도 같은 계약 시험을 통과해야 한다.
2. 등록 내용(8칸과 원문)을 그대로 저장해, 화면에서 "BTS 에 실제로 무엇이 들어갔는지" 확인할 수 있게 한다.
3. set_status 는 BTS 쪽에서 상태가 바뀐 것을 흉내 낸다(시험·화면 확인용). 마지막 변경 시각은 등록 내용 JSON 의 updated 에 둔다 —
   표에 칸을 더하면 이미 만든 DB 를 고쳐야 해서다. 상태 이름은 settings.FAKE_STATUS 가 정한다.
"""

import json
from dataclasses import asdict
from pathlib import Path

from triage_service.contracts import BtsState, Issue, Ticket, TrackerError
from triage_service.store import connect, now


class FakeTracker:
    name = "fake"

    def __init__(self, path: Path):
        self.path = Path(path)
        with connect(self.path) as c:
            c.execute("CREATE TABLE IF NOT EXISTS fake_tickets (n INTEGER PRIMARY KEY AUTOINCREMENT, "
                      "created_at TEXT NOT NULL, status TEXT NOT NULL, issue TEXT NOT NULL)")

    def create(self, issue: Issue) -> Ticket:
        payload = json.dumps({"report": asdict(issue.report), "fields": issue.fields, "ref": issue.ref, "team": issue.team},
                             ensure_ascii=False)
        with connect(self.path) as c:
            n = c.execute("INSERT INTO fake_tickets (created_at, status, issue) VALUES (?, ?, ?)",
                          (now(), "신규", payload)).lastrowid
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
        return []   # 계정이 없다. 팀 이름을 그대로 저장한다

    def snapshot(self, keys: list[str]) -> dict[str, BtsState]:
        out = {}
        for key in keys:
            try:
                t = self.get(key)
            except TrackerError:   # 지워진 티켓은 돌려주지 않는다(실제 BTS 의 검색과 같다)
                continue
            out[key] = BtsState(t["status"], t.get("team", ""), t.get("updated", t["created_at"]))
        return out

    def set_status(self, key: str, status: str) -> None:   # 설계 의도 3
        t = self.get(key)
        issue = {k: v for k, v in t.items() if k not in ("key", "created_at", "status")} | {"updated": now()}
        with connect(self.path) as c:
            c.execute("UPDATE fake_tickets SET status = ?, issue = ? WHERE n = ?",
                      (status, json.dumps(issue, ensure_ascii=False), int(key.removeprefix("FAKE-"))))

    def get(self, key: str) -> dict:
        n = int(key.removeprefix("FAKE-")) if key.startswith("FAKE-") and key[5:].isdigit() else -1
        with connect(self.path) as c:
            row = c.execute("SELECT * FROM fake_tickets WHERE n = ?", (n,)).fetchone()
        if row is None:
            raise TrackerError(f"티켓이 없습니다: {key}")
        return {"key": key, "created_at": row["created_at"], "status": row["status"], **json.loads(row["issue"])}
