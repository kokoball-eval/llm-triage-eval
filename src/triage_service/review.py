"""사람 검토 — 확인·수정한 8칸을 검사해 BTS 에 등록하거나, 폐기하거나, 실패한 건을 다시 시도한다.

[설계 의도]
1. 사람이 고친 값도 모델 출력과 같은 형식 규칙(R1~R7, triage_eval.contracts)을 통과해야 등록된다. 허용 값 밖의
   우선순위나 여러 줄짜리 요약이 BTS 에 들어가면 BTS 쪽 필터·통계가 깨진다. 같은 규칙을 쓰므로 평가와 서비스의
   "올바른 티켓" 정의가 하나다.
2. 등록은 한 번만 일어나야 한다. BTS 를 부르기 전에 상태가 "검토 대기"인지 확인하고, 확인부터 상태 변경까지를
   잠금 하나로 묶어 같은 건을 두 번 눌러도 티켓이 두 개 생기지 않게 한다(서버 프로세스는 하나다). 폐기·다시 시도도
   같은 잠금을 써서, BTS 등록 도중에 폐기되어 BTS 에만 티켓이 남는 일을 막는다.
3. 사람이 고친 칸은 고치기 전 값과 함께 남긴다(store 설계 의도 2).
4. BTS 등록이 실패하면 "실패"로 남기고 사람이 확정한 값도 함께 저장한다. 대체 티켓을 만들지 않는다.
"""

import threading

from triage_eval.contracts import FIELDS, FREE_TEXT_FIELDS, check_format, enum_errors, language_errors
from triage_service import flow
from triage_service.contracts import Issue
from triage_service.store import Store

_decision_lock = threading.Lock()   # 등록·폐기·다시 시도를 한 번에 하나씩 (설계 의도 2)


def to_text(fields: dict[str, str]) -> str:
    """8칸을 BTS 에 들어갈 줄 형식으로 — 모델 출력 형식과 같다."""
    return "\n".join(f"[{f}]: {fields.get(f, '')}" for f in FIELDS)


def problems(fields: dict[str, str]) -> list[str]:
    """등록을 막는 문제 목록 (설계 의도 1). 비어 있으면 등록할 수 있다."""
    out = []
    for f in FREE_TEXT_FIELDS:
        v = fields.get(f, "")
        if not v.strip():
            out.append(f"[{f}] 칸이 비어 있습니다")
        elif "\n" in v or "\r" in v:
            out.append(f"[{f}] 칸은 한 줄로 써야 합니다")
    out += [f"[{f}] 허용 값이 아닙니다: {v!r}" for f, v in enum_errors(fields)]
    out += [f"[{f}] 출력 언어 밖 문자: {chars}" for f, chars in language_errors(fields)]
    if not out and not check_format(to_text(fields))["strict_pass"]:   # 위 검사를 빠져나간 형식 위반에 대한 마지막 확인
        out.append("8칸 형식 규칙(R1~R7)을 통과하지 못했습니다")
    return out


def edits(model_fields: dict | None, final: dict[str, str]) -> list[dict]:
    model_fields = model_fields or {}
    return [{"field": f, "before": model_fields.get(f), "after": final[f]}
            for f in FIELDS if model_fields.get(f) != final.get(f)]


def register(store: Store, tracker, rid: int, fields: dict[str, str]) -> dict:
    """검토 대기 → 등록 완료. 입력 문제가 있으면 ValueError(문제 목록), 상태가 맞지 않으면 flow.FlowError."""
    found = problems(fields)
    if found:
        raise ValueError(found)
    with _decision_lock:
        rec = store.get(rid)
        flow.check(rec["state"], flow.REGISTERED)
        final = {f: fields[f].strip() for f in FIELDS}
        changed = edits(rec["model_fields"], final)
        try:
            ticket = tracker.create(Issue(report=rec["report"], fields=final))
        except Exception as e:   # 설계 의도 4
            return store.move(rid, flow.FAILED, final_fields=final, edits=changed, error=f"BTS 등록 실패: {e}")
        return store.move(rid, flow.REGISTERED, final_fields=final, edits=changed,
                          ticket_key=ticket.key, ticket_url=ticket.url, error=None)


def discard(store: Store, rid: int) -> dict:
    with _decision_lock:   # BTS 등록 도중 폐기되어 티켓만 남는 일을 막는다 (설계 의도 2)
        return store.move(rid, flow.DISCARDED)


def retry(store: Store, worker, rid: int) -> dict:
    """실패 → 접수. 이전 처리 결과를 비우고 처음부터 다시 트리아지한다(지난 결과가 새 결과처럼 보이지 않게)."""
    with _decision_lock:
        rec = store.move(rid, flow.RECEIVED, error=None, result=None, pipeline=None, routing=None,
                         model_fields=None, final_fields=None, edits=None)
    worker.submit(rid)
    return rec

