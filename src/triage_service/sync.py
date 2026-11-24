"""BTS 상태 동기화와 QA 보드 — 등록한 티켓의 BTS 상태를 읽어 와 재현 대기·회귀 테스트 대기·보류·팀별 미해결로 보여 준다.

[설계 의도]
1. BTS 가 원본이다. 서비스는 읽어 온 값을 복사해 두고 보여 줄 뿐이다. BTS 에 쓰는 것은 QA 가 누른 동작뿐이다(qa.py).
   개발자는 계속 BTS 에서 일하므로 서비스가 자기 상태를 따로 가지면 같은 버그의 상태가 두 곳에서 달라진다.
2. 읽기는 버튼을 눌렀을 때만 한다(끌어오기). 서비스는 이 PC(127.0.0.1)에서만 열려 있어 BTS 가 변경을 알려 주는 웹훅을 받을
   수 없고, 화면을 여는 것만으로 BTS 를 부르면 화면이 느려지고 GET 이 값을 바꾸게 된다.
3. 상태는 다섯 이름(contracts.STATUSES)으로 나눈다: QA Review → 재현 대기, Resolve → 회귀 테스트 대기, Check → 보류,
   Closed 가 아니면 팀별 미해결. 다섯 이름 밖의 상태를 읽으면 그 이름을 화면에 그대로 보여 BTS 워크플로가 바뀐 것을 알아챈다.
   (S5-2 의 상태 대응 설정 [<bts>.status] 는 상태 이름을 통일하면서 없앴다.)
4. 읽기에 실패한 묶음은 이전에 읽은 값을 그대로 두고 이유를 보여 준다. 일부만 실패해도 성공한 묶음은 갱신한다. BTS 에서
   찾지 못한 티켓(지워졌거나 접근 권한 밖)은 갱신하지 않고 목록으로 알린다.
5. 대기 시간은 "BTS 가 그 티켓을 마지막으로 바꾼 뒤 경과 시간"이다. 상태가 바뀐 시각이 아니라, 회귀 테스트 대기 중 코멘트가
   달려도 줄어든다. 정확한 상태 변경 시각은 store.bts_changes 에 쌓는 기록(우리가 발견한 시각)으로 S5-4 에서 센다.
6. 대상은 등록한 리포트 중 최근 config.SYNC_LIMIT 건이다. 보드도 같은 대상만 센다(동기화하지 않는 오래된 건이 "동기화 전"으로
   섞이지 않게).
7. 팀은 재현 확인으로 바꾼 팀(QA 기록의 마지막 팀)이 있으면 그것, 없으면 등록 때 배정한 팀이다(store 설계 의도 10). 기록의
   팀은 BTS 가 쓰기 요청을 받은 경우에만 채워진다(qa 설계 의도 3). 상태 확인에서만 실패한 기록도 팀은 바뀐 것이라 센다.
"""

from datetime import datetime

from triage_service import config, flow
from triage_service.contracts import CHECK, CLOSED, QA_REVIEW, RESOLVE, STATUSES
from triage_service.store import Store, now


def targets(store: Store) -> list[dict]:
    """설계 의도 6 — 동기화·보드 대상(등록한 리포트 중 최근 SYNC_LIMIT 건, 번호 큰 순)."""
    return [r for r in store.list(flow.REGISTERED) if r["ticket_key"]][:config.SYNC_LIMIT]


def run(store: Store, tracker) -> dict:
    """BTS 에서 상태를 읽어 저장한다. {"synced": 건수, "missing": [BTS 에서 못 찾은 키], "errors": [묶음 실패 이유]}."""
    out = {"synced": 0, "missing": [], "errors": []}
    records, at = targets(store), now()
    for i in range(0, len(records), config.SYNC_CHUNK):
        chunk = records[i:i + config.SYNC_CHUNK]
        try:
            found = tracker.snapshot([r["ticket_key"] for r in chunk])
        except Exception as e:   # 설계 의도 4 — 한 묶음의 실패가 나머지를 막지 않는다
            out["errors"].append(f"{chunk[0]['ticket_key']} 등 {len(chunk)}건: {e}")
            continue
        for r in chunk:
            state = found.get(r["ticket_key"])
            if state is None:
                out["missing"].append(r["ticket_key"])
                continue
            store.save_bts(r["id"], {"status": state.status, "assignee": state.assignee, "updated": state.updated, "synced_at": at})
            out["synced"] += 1
    return out


def ago(minutes: float) -> str:
    return f"{minutes:.0f}분" if minutes < 60 else f"{minutes / 60:.0f}시간" if minutes < 1440 else f"{minutes / 1440:.0f}일"


def team(record: dict, log: list[dict]) -> str:
    """설계 의도 7 — 지금 담당 팀."""
    moved = [a["team"] for a in log if a["team"]]
    return moved[-1] if moved else (record["assignment"] or {}).get("team") or "배정 없음"


def board(records: list[dict], log: dict, at: datetime | None = None) -> dict:
    """QA 보드 — {"repro": [기록], "verify": [(기록, 대기 문자열)] 오래된 순, "hold": [기록], "teams": {팀: 미해결 건수},
    "unsynced": 건수, "synced_at": 가장 최근에 읽은 시각, "unknown": 다섯 상태 밖의 이름들}. log 는 store.qa_log().
    읽어 둔 기록에는 지금 담당 팀(team_now)을 붙인다."""
    at = at or datetime.now().astimezone()
    repro, verify, hold, teams, unsynced, unknown = [], [], [], {}, 0, set()
    for r in records:
        b = r["bts"]
        if b is None:
            unsynced += 1
            continue
        status, r["team_now"] = b["status"], team(r, log.get(r["id"], []))
        if status not in STATUSES:
            unknown.add(status)
        if status == QA_REVIEW:
            repro.append(r)
        elif status == RESOLVE:
            try:   # 시각을 못 읽으면(빈 값·이상한 값) 화면 전체를 막지 않고 0분으로 둔다
                waited = max((at - _aware(b["updated"])).total_seconds() / 60, 0)
            except ValueError:
                waited = 0
            verify.append((waited, r))
        elif status == CHECK:
            hold.append(r)
        if status != CLOSED:
            teams[r["team_now"]] = teams.get(r["team_now"], 0) + 1
    verify.sort(key=lambda w: -w[0])
    synced = [r["bts"]["synced_at"] for r in records if r["bts"]]
    return {"repro": repro, "verify": [(r, ago(w)) for w, r in verify], "hold": hold, "teams": dict(sorted(teams.items())),
            "unsynced": unsynced, "synced_at": max(synced, default=None), "unknown": sorted(unknown)}


def _aware(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.astimezone()   # 시간대가 없는 값(가짜 BTS)은 이 PC 시각으로 본다
