"""운영 지표 — 저장소의 리포트와 상태 이력으로 대시보드 숫자를 계산한다. 화면 코드는 없다(web/app.py 가 보여 준다).

[설계 의도]
1. 비율은 언제나 분자·분모를 함께 돌려준다(ratio). 분모 없는 퍼센트는 표본이 3건인지 300건인지 알 수 없다.
   분모가 0이면 퍼센트는 None 이고 화면은 "데이터 없음"을 보인다(0으로 나누지 않는다).
2. 사람 수정 비율은 등록한 건 기준이다. 등록한 건만 사람이 8칸을 확정했기 때문이다. 칸별로도 세어, 모델이 어느 칸을
   자주 틀리는지 운영 중에 볼 수 있게 한다. 측정에서 가장 무거웠던 Critical 누락은 "사람이 Critical 로 올린 건"으로 따로 센다.
3. 처리 시간은 상태 이력(transitions)으로 센다: 트리아지 시간(모델 응답 시간), 검토 대기 시간, 접수→등록 시간.
   - 검토 대기는 리포트 한 건의 값이다. BTS 등록 실패 → 검토로 되돌리기처럼 검토 대기에 여러 번 들어갔으면 머문 시간을
     모두 더하고(실패 상태에 있던 시간은 빼고), 검토 대기를 마친(나간) 리포트만 센다.
   - 지금 검토 대기 중인 리포트는 아직 끝난 값이 없어 시간 지표에서 빠진다. 오래 쌓인 건이 p95 에서 안 보이는 것을
     막기 위해 그 건수(waiting_now)를 함께 돌려주고 화면에 적는다.
   - 접수 이력이 없는 옛 리포트(이력 표가 생기기 전에 접수)는 접수→등록 시간을 셀 수 없어 빼고, 그 건수(no_receipt)를 적는다.
7. 실패 횟수(events)는 기간 안에 접수한 리포트에서 일어난 실패 전체다(실패한 시각이 기간 밖이어도 센다). 설계 의도 6과 같은 기준이다.
4. 백분위는 가장 가까운 순위 방식(nearest-rank)이다: 정렬한 값에서 ceil(p/100 × n)번째. 표본이 적을 때도 실제로 나온
   값 중 하나가 되어 설명하기 쉽다.
5. 자동 처리율은 0%다(라우팅 미채택 — routing.py). 숫자로 함께 보여 줘 "모든 건 사람 확인"이 지켜지는지 드러낸다.
6. 기간 필터는 접수 시각 기준이다. 기간 안에 접수한 리포트만 센다.
8. QA 지표(qa)는 기간이 아니라 마일스톤 단위다(QA 성과의 단위, sync 설계 의도 9). 그 마일스톤의 BTS 티켓 전체와 상태 변화
   기록(store.bts_ticket_changes)으로 센다. 변화 시각은 BTS 가 바꾼 때가 아니라 동기화에서 발견한 때다.
   - 회귀 테스트 대기는 Resolve 에 들어간 것을 본 때부터 나간 것을 본 때까지다. 처음 읽었을 때 이미 Resolve 였던 티켓은 들어간
     시각을 몰라 시간에서 빼고 그 건수(unknown_entry)를 적는다(검토 대기 시간의 no_receipt 와 같은 방식).
   - 재오픈률 = Resolve → In Dev 를 본 티켓 ÷ Resolve 에 있는 것을 본 티켓.
   - 팀별 미해결은 Closed 가 아닌 티켓을 BTS 팀 표식으로 센다(contracts 설계 의도 7). 표식이 없으면 "팀 없음".
   - 사람이 바꾼 배정 비율은 이 마일스톤 티켓 중 서비스가 등록하고 자동 배정을 쓴 건만 센다(규칙의 팀이 있어야 비교할 수
     있다). 이미 있던 티켓에 연결해 배정을 적용하지 않은 건은 뺀다. 재현 확인 뒤 팀을 옮긴 것(QA 기록)은 사람이 배정을 고친
     것이 아니라 업무 흐름이라 세지 않는다.
   - shortcut: 상태 변화는 티켓 단위라 다른 마일스톤에서 옮겨 온 티켓은 옮기기 전의 Resolve 체류·재오픈도 지금 마일스톤에
     들어간다. 마일스톤을 자주 옮기는 운영이면 변화 기록에 마일스톤을 함께 남기도록 올린다.
"""

import math
from collections import Counter
from datetime import datetime, timedelta

from triage_eval.contracts import ENUMS, split_modules
from triage_service import flow
from triage_service.contracts import CLOSED, IN_DEV, RESOLVE

PERIODS = {"7": 7, "30": 30, "all": None}


def ratio(n: int, d: int) -> dict:
    return {"n": n, "d": d, "pct": round(n / d * 100, 1) if d else None}


def percentile(values: list[float], p: float) -> float | None:
    """설계 의도 4 — nearest-rank."""
    if not values:
        return None
    s = sorted(values)
    return s[max(math.ceil(p / 100 * len(s)) - 1, 0)]


def summary(values: list[float]) -> dict:
    return {"n": len(values), "p50": percentile(values, 50), "p95": percentile(values, 95)}


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text)


def compute(records: list[dict], history: dict, period: str = "30", now: datetime | None = None) -> dict:
    now = now or datetime.now()
    days = PERIODS.get(period, 30)
    since = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0) if days else None
    recs = [r for r in records if since is None or _t(r["created_at"]) >= since]   # 설계 의도 6
    registered = [r for r in recs if r["state"] == flow.REGISTERED]

    # 일자별 유입 (출처별)
    first = since.date() if since else (min((_t(r["created_at"]).date() for r in recs), default=now.date()))
    dates = [first + timedelta(days=i) for i in range((now.date() - first).days + 1)]
    by_day = {d.isoformat(): Counter() for d in dates}
    for r in recs:
        by_day.setdefault(_t(r["created_at"]).date().isoformat(), Counter())[r["report"].source] += 1

    # 분포 (등록한 건의 확정 8칸)
    dist = {f: Counter() for f in ("우선순위", "분류", "처리", "모듈")}
    for r in registered:
        fields = r["final_fields"] or {}
        for f in ("우선순위", "분류", "처리"):
            dist[f][fields.get(f, "")] += 1
        for m in split_modules(fields.get("모듈", "")):
            dist["모듈"][m] += 1
    order = {f: ENUMS[f] for f in ("우선순위", "분류", "처리")}

    # 사람 수정 (설계 의도 2)
    edited = [r for r in registered if r["edits"]]
    by_field = Counter(e["field"] for r in registered for e in (r["edits"] or []))
    up = sum(1 for r in registered for e in (r["edits"] or [])
             if e["field"] == "우선순위" and e["before"] != "Critical" and e["after"] == "Critical")
    down = sum(1 for r in registered for e in (r["edits"] or [])
               if e["field"] == "우선순위" and e["before"] == "Critical" and e["after"] != "Critical")

    # 처리 시간 (설계 의도 3)
    triage_sec, review_min, total_min, no_receipt = [], [], [], 0
    waiting_now = sum(1 for r in recs if r["state"] == flow.REVIEW)
    for r in recs:
        if r["result"] and r["result"].get("success") and r["result"].get("elapsed_sec") is not None:
            triage_sec.append(float(r["result"]["elapsed_sec"]))
        events = history.get(r["id"], [])
        if not events or events[0][1] is not None or events[0][2] != flow.RECEIVED:
            no_receipt += 1
        entered, stayed, closed = None, 0.0, False
        for at, _src, dst in events:
            if dst == flow.REVIEW:
                entered = _t(at)
            elif entered is not None:
                stayed += (_t(at) - entered).total_seconds() / 60
                entered, closed = None, True
        if closed and r["state"] != flow.REVIEW:   # 설계 의도 3 — 검토를 마친 리포트, 머문 시간 합
            review_min.append(stayed)
        done = [at for at, _s, dst in events if dst == flow.REGISTERED]
        if done and events and events[0][1] is None and events[0][2] == flow.RECEIVED:
            total_min.append((_t(done[-1]) - _t(events[0][0])).total_seconds() / 60)

    # 실패
    failures = Counter()
    for r in recs:
        for _at, _src, dst in history.get(r["id"], []):
            if dst == flow.FAILED:
                failures["이벤트"] += 1
        if r["state"] == flow.FAILED:
            failures[r["failed_step"] or "알 수 없음"] += 1
    decided = sum(1 for r in recs if r["state"] in (flow.REGISTERED, flow.DISCARDED))

    return {
        "period": period if period in PERIODS else "30", "since": since.date().isoformat() if since else None,
        "total": len(recs),
        "states": {s: sum(1 for r in recs if r["state"] == s) for s in flow.STATES},
        "intake": [{"date": d, "total": sum(c.values()), "by_source": dict(c)} for d, c in sorted(by_day.items())],
        "intake_rows": [(d[5:], sum(c.values()), " · ".join(f"{k} {v}" for k, v in sorted(c.items())))
                        for d, c in sorted(by_day.items())],
        "sources": dict(Counter(r["report"].source for r in recs)),
        "distribution": {f: [(k, dist[f][k]) for k in (order.get(f) or sorted(dist[f], key=lambda x: -dist[f][x]))
                             if dist[f][k]] for f in dist},
        "registered": len(registered),
        "edited": ratio(len(edited), len(registered)),
        "edited_by_field": {f: ratio(by_field[f], len(registered)) for f in
                            ("요약", "분류", "모듈", "우선순위", "재현 정보", "발생 빈도", "처리", "누락 정보 및 권장 조치")},
        "critical_up": ratio(up, len(registered)), "critical_down": ratio(down, len(registered)),
        "auto": ratio(0, decided),   # 설계 의도 5
        "times": {"triage_sec": summary(triage_sec), "review_min": summary(review_min),
                  "total_min": summary(total_min), "no_receipt": no_receipt, "waiting_now": waiting_now},
        "failures": {"events": failures["이벤트"], "now_triage": failures["트리아지"], "now_register": failures["등록"],
                     "now_unknown": failures["알 수 없음"], "rate": ratio(sum(1 for r in recs if r["state"] == flow.FAILED),
                                                                     len(recs))},
    }


def qa(tickets: list[dict], changes: dict, records: list[dict], now: datetime | None = None) -> dict:
    """설계 의도 8 — 마일스톤 하나의 QA 지표. tickets 는 store.tickets(이름), changes 는 store.ticket_changes(),
    records 는 등록한 리포트(store.list(flow.REGISTERED)). 시간은 시간(h) 단위."""
    now = now or datetime.now()
    stays, waiting, unknown, reached, reopened = [], [], 0, 0, 0
    for t in tickets:
        seen = changes.get(t["key"], [])
        entered = None
        for at, src, dst in seen:
            if entered is not None:
                stays.append((_t(at) - entered).total_seconds() / 3600)
                entered = None
            if dst == RESOLVE:
                if src is None:
                    unknown += 1
                else:
                    entered = _t(at)
        if t["status"] == RESOLVE and entered is not None:
            waiting.append((now - entered).total_seconds() / 3600)
        reached += any(dst == RESOLVE for _, _, dst in seen)
        reopened += any(src == RESOLVE and dst == IN_DEV for _, src, dst in seen)
    teams = Counter(t["team"] or "팀 없음" for t in tickets if t["status"] != CLOSED)
    keys = {t["key"] for t in tickets}
    assigned = [r["assignment"] for r in records if r["ticket_key"] in keys and r["assignment"]
                and r["assignment"].get("suggested") and not r["assignment"].get("linked_existing")]
    return {"total": len(tickets),
            "resolve_now": sum(t["status"] == RESOLVE for t in tickets),
            "resolve_wait": summary(stays), "oldest_wait": max(waiting, default=None), "unknown_entry": unknown,
            "reopen": ratio(reopened, reached),
            "teams": sorted(teams.items(), key=lambda x: (-x[1], x[0])),
            "assign_changed": ratio(sum(a["team"] != a["suggested"] for a in assigned), len(assigned))}
