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
   재현 확인으로 바꾼 팀은 이 기록의 마지막 팀이 현재 팀이다(assignment 는 등록 때 배정으로 두어 S5-4 의 "사람이 바꾼 비율"을 지킨다).
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
