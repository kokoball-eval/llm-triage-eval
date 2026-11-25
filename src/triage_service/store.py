"""저장소 — SQLite(표준 라이브러리) 한 파일에 리포트와 처리 내역을 남긴다.

[설계 의도]
1. 상태는 move() 로만 바꾸고, 바꿀 때마다 flow.check 로 허용 전이인지 확인한다. 읽기와 쓰기를 한 트랜잭션
   (BEGIN IMMEDIATE)에서 해서, 작업자와 화면이 같은 건을 동시에 바꿔도 허용되지 않은 전이가 끼어들지 않는다.
2. 모델이 낸 8칸(model_fields)과 사람이 확정한 8칸(final_fields), 사람이 고친 칸 목록(edits: 칸·고치기 전·고친 뒤)을
   따로 남긴다. 대시보드의 "사람 수정 비율"과 이후 학습·평가 데이터 후보의 근거가 된다.
3. 트리아지 결과 전체(시도 기록·안전장치 적용 내역)와 처리 구성 지문(pipeline: 프롬프트 버전·지문·방법 등)을 함께 남긴다.
   나중에 구성이 바뀌어도 어떤 구성이 낸 판정인지 알 수 있다.
4. 호출마다 연결을 새로 연다. 작업자 스레드와 웹 요청이 연결을 공유하지 않아 스레드 문제가 없고, 건수가 적어 비용도 작다.
5. BTS 티켓에 남기는 서비스 표식은 "triage-<DB 고유값>-<번호>"다. 번호만 쓰면 DB 를 새로 만들었을 때 예전 DB 의 같은
   번호 티켓을 이 리포트의 티켓으로 잘못 연결한다. DB 고유값은 처음 만들 때 한 번 정해 meta 표에 둔다.
6. 앞 단계에서 만든 DB 는 시작할 때 빠진 칸(failed_step, assignment, bts)을 붙인다. 리포트 기록을 지우지 않고 이어 쓴다.
7. 같은 원문 링크는 한 번만 접수한다. 같은 제보를 두 사람이 따로 넣어 BTS 에 티켓이 둘 생기는 일을 접수 단계에서 막는다.
8. 상태가 바뀔 때마다 같은 트랜잭션 안에서 이력(transitions: 시각, 이전 → 다음 상태)을 한 줄 남긴다. 접수 시각과 마지막
   변경 시각만으로는 "검토 대기에 얼마나 머물렀는지" 같은 처리 시간을 셀 수 없기 때문이다(대시보드, metrics.py).
   허용되지 않은 전이는 되돌리므로 이력도 남지 않는다. 이 표가 생기기 전에 접수한 리포트는 이력이 없다.
9. BTS 에서 읽은 마지막 상태는 reports.bts 에 두고, 동기화 때 상태가 바뀐 것을 발견하면 같은 트랜잭션에서 bts_changes 에
   한 줄 남긴다(처음 읽은 상태도 한 줄). 재오픈률·검증 대기 시간(S5-4)은 이 기록이 있어야 셀 수 있고, 지난 변화는 나중에
   되살릴 수 없어서 지금부터 쌓는다. 시각은 BTS 가 바뀐 때가 아니라 우리가 읽다가 발견한 때다(동기화 사이의 변화는 한 번으로
   합쳐진다). BTS 가 원본이라 이 기록은 리포트 흐름(state·transitions)에 영향을 주지 않는다.
10. 서비스가 BTS 에 쓴 것(qa.py 의 QA 동작, 등록 직후의 첫 상태)은 qa_actions 에 한 줄씩 남긴다: 동작, 쓰기 전·후 BTS 상태,
   바꾼 팀, 코멘트, 오류. 실패하거나 일부만 된 것도 남겨, BTS 와 서비스 화면이 다를 때 무엇을 보냈는지 확인할 수 있게 한다.
   재현 확인으로 바꾼 팀은 이 기록의 마지막 팀이 현재 팀이다(assignment 는 등록 때 배정으로 두어 S5-4c 의 "사람이 바꾼 비율"을 지킨다).
11. 마일스톤(BTS 버전) 목록은 bts_milestones 에, 마일스톤에 속한 BTS 티켓 전체(서비스가 등록하지 않은 것 포함)는 bts_tickets 에
   티켓 키로 둔다(팀은 BTS 팀 표식, 스프린트는 BTS 스프린트 표식 — contracts 설계 의도 7·9). 스프린트 이월(sync 설계 의도 15)은
   티켓 키로 sprint_carries 에 한 줄씩 남긴다(이월 횟수의 근거). 리포트(reports)와는 티켓 키로만 이어진다. 상태가 처음이거나 바뀐 것을 발견하면 bts_ticket_changes 에 한 줄
   남긴다(설계 의도 9 와 같은 방식, 재오픈 수의 근거). 동기화에서 그 마일스톤 검색에 더는 나오지 않는 티켓은 마일스톤을 비운다
   (BTS 에서 다른 버전으로 옮겼거나 버전을 뺀 경우). shortcut: 티켓 하나에 마일스톤 하나 — Jira 는 버전을 여러 개 걸 수 있지만
   마지막에 읽은 마일스톤에만 센다. 한 티켓을 여러 마일스톤에 거는 운영을 하게 되면 키를 (티켓, 마일스톤)으로 올린다.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from triage_service import flow
from triage_service.contracts import Report

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    state TEXT NOT NULL,
    report TEXT NOT NULL,
    result TEXT,
    pipeline TEXT,
    routing TEXT,
    model_fields TEXT,
    final_fields TEXT,
    edits TEXT,
    ticket_key TEXT,
    ticket_url TEXT,
    error TEXT,
    failed_step TEXT,
    assignment TEXT,
    bts TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transitions (
    report_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    src TEXT,
    dst TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bts_changes (
    report_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    src TEXT,
    dst TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bts_milestones (
    name TEXT PRIMARY KEY,
    release TEXT NOT NULL,
    released INTEGER NOT NULL,
    synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bts_tickets (
    key TEXT PRIMARY KEY,
    milestone TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    assignee TEXT NOT NULL,
    updated TEXT NOT NULL,
    synced_at TEXT NOT NULL,
    team TEXT NOT NULL DEFAULT '',
    sprint TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS bts_ticket_changes (
    key TEXT NOT NULL,
    at TEXT NOT NULL,
    src TEXT,
    dst TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sprint_carries (
    key TEXT NOT NULL,
    at TEXT NOT NULL,
    src TEXT NOT NULL,
    dst TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qa_actions (
    report_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    action TEXT NOT NULL,
    before TEXT,
    after TEXT,
    team TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    error TEXT
);
"""
FAILED_STEPS = ("트리아지", "등록")
JSON_COLUMNS = ("result", "pipeline", "routing", "model_fields", "final_fields", "edits",
                "assignment", "bts")   # assignment: {"suggested": 규칙의 팀, "team": 배정한 팀} (assign.py), bts: BTS 에서 읽은 마지막 상태 (sync.py)
ADDED_COLUMNS = ("failed_step", "assignment", "bts")   # 앞 단계 DB 에 붙이는 칸 (설계 의도 6)


@contextmanager
def connect(path: Path):
    """연결을 열고 끝나면 닫는다. 자동 커밋 모드라 문장 하나가 곧 트랜잭션 하나다(여러 문장은 BEGIN 으로 묶는다)."""
    c = sqlite3.connect(path, isolation_level=None)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with connect(self.path) as c:
            c.executescript(SCHEMA)
            columns = {r["name"] for r in c.execute("PRAGMA table_info(reports)")}
            for name in ADDED_COLUMNS:   # 설계 의도 6
                if name not in columns:
                    c.execute(f"ALTER TABLE reports ADD COLUMN {name} TEXT")
            ticket_columns = {r["name"] for r in c.execute("PRAGMA table_info(bts_tickets)")}
            for name in ("team", "sprint"):   # S5-4b·S5-5c 에서 만든 DB
                if name not in ticket_columns:
                    c.execute(f"ALTER TABLE bts_tickets ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('instance', ?)", (uuid.uuid4().hex[:8],))
            self.instance = c.execute("SELECT value FROM meta WHERE key = 'instance'").fetchone()["value"]

    def ref(self, rid: int) -> str:
        """BTS 티켓에 남길 서비스 표식 (설계 의도 5)."""
        return f"triage-{self.instance}-{rid}"

    def add(self, report: Report) -> int:
        t = now()
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                rid = c.execute("INSERT INTO reports (created_at, updated_at, state, report) VALUES (?, ?, ?, ?)",
                                (t, t, flow.RECEIVED, json.dumps(asdict(report), ensure_ascii=False))).lastrowid
                c.execute("INSERT INTO transitions (report_id, at, src, dst) VALUES (?, ?, NULL, ?)",
                          (rid, t, flow.RECEIVED))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            return rid

    def get(self, rid: int) -> dict | None:
        with connect(self.path) as c:
            row = c.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
        return _decode(row) if row else None

    def find_by_url(self, url: str) -> dict | None:
        """같은 원문 링크로 이미 접수한 리포트 (설계 의도 7)."""
        if not url:
            return None
        with connect(self.path) as c:
            row = c.execute("SELECT * FROM reports WHERE json_extract(report, '$.url') = ? ORDER BY id LIMIT 1",
                            (url,)).fetchone()
        return _decode(row) if row else None

    def history(self) -> dict[int, list[tuple[str, str | None, str]]]:
        """리포트 번호 → [(시각, 이전 상태, 다음 상태)] 시간순 (설계 의도 8)."""
        out: dict[int, list] = {}
        with connect(self.path) as c:
            for r in c.execute("SELECT report_id, at, src, dst FROM transitions ORDER BY rowid"):
                out.setdefault(r["report_id"], []).append((r["at"], r["src"], r["dst"]))
        return out

    def list(self, state: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM reports", ()
        if state:
            sql, args = sql + " WHERE state = ?", (state,)
        with connect(self.path) as c:
            return [_decode(r) for r in c.execute(sql + " ORDER BY id DESC", args)]

    def save_bts(self, rid: int, bts: dict) -> None:
        """BTS 에서 읽은 상태를 남기고, 상태가 처음이거나 바뀌었으면 변화 한 줄을 더한다 (설계 의도 9)."""
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT bts FROM reports WHERE id = ?", (rid,)).fetchone()
                if row is None:
                    raise KeyError(rid)
                old = json.loads(row["bts"]) if row["bts"] else None
                if old is None or old["status"] != bts["status"]:
                    c.execute("INSERT INTO bts_changes (report_id, at, src, dst) VALUES (?, ?, ?, ?)",
                              (rid, bts["synced_at"], old and old["status"], bts["status"]))
                c.execute("UPDATE reports SET bts = ? WHERE id = ?", (json.dumps(bts, ensure_ascii=False), rid))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def bts_history(self) -> dict:   # list 이름을 이 클래스의 list() 가 가려 타입을 적지 않는다
        """리포트 번호 → [(발견한 시각, 이전 BTS 상태, 다음 BTS 상태)] 시간순 (설계 의도 9)."""
        out: dict = {}
        with connect(self.path) as c:
            for r in c.execute("SELECT report_id, at, src, dst FROM bts_changes ORDER BY rowid"):
                out.setdefault(r["report_id"], []).append((r["at"], r["src"], r["dst"]))
        return out

    def save_milestones(self, milestones, at: str) -> None:
        """BTS 버전 목록을 통째로 바꾼다(BTS 에서 지운 버전은 사라진다, 설계 의도 11)."""
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("DELETE FROM bts_milestones")
                c.executemany("INSERT INTO bts_milestones (name, release, released, synced_at) VALUES (?, ?, ?, ?)",
                              [(m.name, m.release, int(m.released), at) for m in milestones])
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def milestones(self):   # list 이름을 이 클래스의 list() 가 가려 타입을 적지 않는다
        """출시일 순(출시일 없는 버전은 뒤로)."""
        with connect(self.path) as c:
            rows = c.execute("SELECT * FROM bts_milestones ORDER BY release = '', release, name").fetchall()
        return [{**dict(r), "released": bool(r["released"])} for r in rows]

    def save_tickets(self, milestone: str, tickets, at: str) -> None:
        """마일스톤 하나의 BTS 티켓 전체를 저장하고 상태 변화를 남긴다 (설계 의도 11)."""
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                for t in tickets:
                    self._ticket_status(c, t.key, t.state.status, at)
                    c.execute("INSERT INTO bts_tickets (key, milestone, title, status, assignee, updated, synced_at, team, sprint) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET milestone = excluded.milestone, "
                              "title = excluded.title, status = excluded.status, assignee = excluded.assignee, "
                              "updated = excluded.updated, synced_at = excluded.synced_at, team = excluded.team, sprint = excluded.sprint",
                              (t.key, milestone, t.title, t.state.status, t.state.assignee, t.state.updated, at, t.team, t.sprint))
                keys = [t.key for t in tickets]
                c.execute(f"UPDATE bts_tickets SET milestone = '' WHERE milestone = ? AND key NOT IN ({','.join('?' * len(keys))})",
                          (milestone, *keys))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    def ticket_status(self, key: str, status: str, at: str) -> None:
        """서비스가 쓴 뒤 읽은 상태를 마일스톤 티켓에도 반영한다(그 티켓이 있을 때만, qa.py)."""
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                if c.execute("SELECT 1 FROM bts_tickets WHERE key = ?", (key,)).fetchone():
                    self._ticket_status(c, key, status, at)
                    c.execute("UPDATE bts_tickets SET status = ?, synced_at = ? WHERE key = ?", (status, at, key))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    @staticmethod
    def _ticket_status(c, key: str, status: str, at: str) -> None:
        row = c.execute("SELECT status FROM bts_tickets WHERE key = ?", (key,)).fetchone()
        if row is None or row["status"] != status:
            c.execute("INSERT INTO bts_ticket_changes (key, at, src, dst) VALUES (?, ?, ?, ?)", (key, at, row and row["status"], status))

    def tickets(self, milestone: str):
        with connect(self.path) as c:
            return [dict(r) for r in c.execute("SELECT * FROM bts_tickets WHERE milestone = ? ORDER BY key", (milestone,))]

    def save_carry(self, key: str, src: str, dst: str, at: str) -> None:
        """스프린트 이월 한 줄과 저장해 둔 티켓의 스프린트 (sync 설계 의도 15)."""
        with connect(self.path) as c:
            c.execute("INSERT INTO sprint_carries (key, at, src, dst) VALUES (?, ?, ?, ?)", (key, at, src, dst))
            c.execute("UPDATE bts_tickets SET sprint = ? WHERE key = ?", (dst, key))

    def carries(self) -> dict:
        """티켓 키 → [(이월한 시각, 이전 스프린트, 다음 스프린트)] 시간순 (sync 설계 의도 15)."""
        out: dict = {}
        with connect(self.path) as c:
            for r in c.execute("SELECT key, at, src, dst FROM sprint_carries ORDER BY rowid"):
                out.setdefault(r["key"], []).append((r["at"], r["src"], r["dst"]))
        return out

    def ticket_changes(self) -> dict:
        """티켓 키 → [(발견한 시각, 이전 상태, 다음 상태)] 시간순 (설계 의도 11)."""
        out: dict = {}
        with connect(self.path) as c:
            for r in c.execute("SELECT key, at, src, dst FROM bts_ticket_changes ORDER BY rowid"):
                out.setdefault(r["key"], []).append((r["at"], r["src"], r["dst"]))
        return out

    def save_qa(self, rid: int, action: str, before: str | None, after: str | None, team: str = "", comment: str = "",
                error: str | None = None) -> None:
        """BTS 에 쓴 것 한 줄 (설계 의도 10)."""
        with connect(self.path) as c:
            c.execute("INSERT INTO qa_actions (report_id, at, action, before, after, team, comment, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      (rid, now(), action, before, after, team, comment, error))

    def qa_log(self) -> dict:
        """리포트 번호 → [{at, action, before, after, team, comment, error}] 시간순 (설계 의도 10)."""
        out: dict = {}
        with connect(self.path) as c:
            for r in c.execute("SELECT * FROM qa_actions ORDER BY rowid"):
                out.setdefault(r["report_id"], []).append({k: r[k] for k in r.keys() if k != "report_id"})
        return out

    def move(self, rid: int, dst: str, **columns) -> dict:
        """상태를 dst 로 바꾸고 columns 를 함께 쓴다. 허용되지 않은 전이면 flow.FlowError (설계 의도 1)."""
        unknown = set(columns) - set(JSON_COLUMNS) - {"ticket_key", "ticket_url", "error", "failed_step"}
        if unknown:
            raise ValueError(f"모르는 칸: {sorted(unknown)}")
        values = {k: json.dumps(v, ensure_ascii=False) if k in JSON_COLUMNS else v for k, v in columns.items()}
        sets = ", ".join(f"{k} = ?" for k in ["state", "updated_at", *values])
        t = now()
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT state FROM reports WHERE id = ?", (rid,)).fetchone()
                if row is None:
                    raise KeyError(rid)
                flow.check(row["state"], dst)
                c.execute(f"UPDATE reports SET {sets} WHERE id = ?", (dst, t, *values.values(), rid))
                c.execute("INSERT INTO transitions (report_id, at, src, dst) VALUES (?, ?, ?, ?)",
                          (rid, t, row["state"], dst))   # 설계 의도 8
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        return self.get(rid)


def _decode(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in JSON_COLUMNS:
        d[k] = json.loads(d[k]) if d[k] is not None else None
    rep = json.loads(d["report"])
    d["report"] = Report(**{**rep, "known_issues": tuple(rep.get("known_issues", ()))})
    return d
