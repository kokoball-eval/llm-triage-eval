"""저장소 — SQLite(표준 라이브러리) 한 파일에 리포트와 처리 내역을 남긴다.

[설계 의도]
1. 상태는 move() 로만 바꾸고, 바꿀 때마다 flow.check 로 허용 전이인지 확인한다. 읽기와 쓰기를 한 트랜잭션
   (BEGIN IMMEDIATE)에서 해서, 작업자와 화면이 같은 건을 동시에 바꿔도 허용되지 않은 전이가 끼어들지 않는다.
2. 모델이 낸 8칸(model_fields)과 사람이 확정한 8칸(final_fields), 사람이 고친 칸 목록(edits: 칸·고치기 전·고친 뒤)을
   따로 남긴다. 대시보드의 "사람 수정 비율"과 이후 학습·평가 데이터 후보의 근거가 된다.
3. 트리아지 결과 전체(시도 기록·안전장치 적용 내역)와 처리 구성 지문(pipeline: 프롬프트 버전·지문·방법 등)을 함께 남긴다.
   나중에 구성이 바뀌어도 어떤 구성이 낸 판정인지 알 수 있다.
4. 호출마다 연결을 새로 연다. 작업자 스레드와 웹 요청이 연결을 공유하지 않아 스레드 문제가 없고, 건수가 적어 비용도 작다.
"""

import json
import sqlite3
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
    error TEXT
);
"""
JSON_COLUMNS = ("result", "pipeline", "routing", "model_fields", "final_fields", "edits")


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

    def add(self, report: Report) -> int:
        t = now()
        with connect(self.path) as c:
            cur = c.execute("INSERT INTO reports (created_at, updated_at, state, report) VALUES (?, ?, ?, ?)",
                            (t, t, flow.RECEIVED, json.dumps(asdict(report), ensure_ascii=False)))
            return cur.lastrowid

    def get(self, rid: int) -> dict | None:
        with connect(self.path) as c:
            row = c.execute("SELECT * FROM reports WHERE id = ?", (rid,)).fetchone()
        return _decode(row) if row else None

    def list(self, state: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM reports", ()
        if state:
            sql, args = sql + " WHERE state = ?", (state,)
        with connect(self.path) as c:
            return [_decode(r) for r in c.execute(sql + " ORDER BY id DESC", args)]

    def move(self, rid: int, dst: str, **columns) -> dict:
        """상태를 dst 로 바꾸고 columns 를 함께 쓴다. 허용되지 않은 전이면 flow.FlowError (설계 의도 1)."""
        unknown = set(columns) - set(JSON_COLUMNS) - {"ticket_key", "ticket_url", "error"}
        if unknown:
            raise ValueError(f"모르는 칸: {sorted(unknown)}")
        values = {k: json.dumps(v, ensure_ascii=False) if k in JSON_COLUMNS else v for k, v in columns.items()}
        sets = ", ".join(f"{k} = ?" for k in ["state", "updated_at", *values])
        with connect(self.path) as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT state FROM reports WHERE id = ?", (rid,)).fetchone()
                if row is None:
                    raise KeyError(rid)
                flow.check(row["state"], dst)
                c.execute(f"UPDATE reports SET {sets} WHERE id = ?", (dst, now(), *values.values(), rid))
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
