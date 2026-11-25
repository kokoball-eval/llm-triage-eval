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
5. 등록하기 전에 서비스 표식(store.ref)으로 BTS 에서 같은 리포트의 티켓을 먼저 찾는다. 이전 등록 요청이 응답 없이
   끊겼지만 실제로는 만들어졌다면, 새로 만들지 않고 그 티켓에 연결한다(티켓 중복 방지).
6. BTS 등록에서 실패한 건은 [검토로 되돌리기]로 확정한 8칸을 그대로 둔 채 다시 등록할 수 있다. 모델을 다시 돌리지 않는다.
7. 배정 팀(assign.py)은 확정한 8칸과 함께 정하고, 등록이 실패해도 고른 팀을 남긴다(검토로 되돌리면 화면이 그 팀을 다시
   보인다). 이미 있는 티켓에 연결할 때(설계 의도 5)는 그 티켓의 담당자를 바꾸지 않고, 배정이 적용되지 않았다고
   (linked_existing) 남긴다. 그래야 사람이 바꾼 비율 같은 지표에 실제로 배정하지 않은 건이 섞이지 않는다.
8. 새로 만든 티켓은 확정한 [처리]에 맞는 첫 상태(assign.first_status)로 바꾼다(qa.py 설계 의도 6). 바꾸지 못해도 티켓은
   이미 있어 등록 완료로 두고, 실패는 QA 기록에 남겨 상세 화면에 보인다. 이미 있던 티켓에 연결할 때는 상태도 바꾸지 않는다.
9. 마일스톤은 마지막 동기화로 읽어 둔 BTS 버전 중 출시 전인 것만 받는다(화면 입력은 신뢰 경계라 여기서 확인한다). 고른
   마일스톤은 배정 기록(assignment)에 함께 남긴다. 이미 있던 티켓에 연결할 때는 그 티켓의 버전을 바꾸지 않는다.
10. 담당자는 팀 그룹의 구성원 한 사람이다(assign 설계 의도 8). 등록할 때 BTS 에서 그룹을 읽어 정하고, 배정 기록에 담당자
   (member)·규칙이 고른 사람(member_rule)·QA 담당(qa_owner: QA팀으로 간 건의 담당자)을 남긴다. QA 담당은 이슈가 개발로 넘어가도
   그대로라, Resolve 가 되면 그 사람의 회귀 테스트 차례다(S5-5b). 그룹을 읽지 못하면 BTS 등록 실패와 같이 다룬다.
11. 목표 스프린트(sprint.py)는 검토 화면에서 고른 업데이트 날짜다(기본값은 우선순위 규칙). 마일스톤처럼 배정 기록에 남기고,
   이미 있던 티켓에 연결할 때는 그 티켓의 스프린트를 바꾸지 않는다.
"""

import threading
from datetime import date

from triage_eval.contracts import FIELDS, FREE_TEXT_FIELDS, check_format, enum_errors, language_errors
from triage_service import assign, flow, qa, sprint
from triage_service.contracts import CHECK, Issue
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


def register(store: Store, tracker, rid: int, fields: dict[str, str], rules: dict | None = None,
             team: str = assign.AUTO, repro: str = assign.AUTO, milestone: str = "", member: str = assign.AUTO,
             sprint_: str = "") -> dict:
    """검토 대기 → 등록 완료. 입력 문제가 있으면 ValueError(문제 목록), 상태가 맞지 않으면 flow.FlowError.
    rules·team 은 자동 배정 규칙과 검토 화면에서 고른 팀(설계 의도 7), repro 는 CS 응대 건의 QA 재현 필요 선택(assign 설계 의도 7),
    member 는 고른 구성원 ID(assign 설계 의도 9), sprint_ 은 목표 스프린트(업데이트 날짜, 빈 문자열이면 없음, 설계 의도 11)."""
    found = problems(fields)
    if found:
        raise ValueError(found)
    if milestone and milestone not in {m["name"] for m in store.milestones() if not m["released"]}:   # 설계 의도 9
        raise ValueError([f"출시 전 마일스톤이 아닙니다: {milestone!r} (QA 화면에서 동기화하면 목록이 새로 읽힙니다)"])
    if sprint_ and not sprint.valid(sprint_):   # 설계 의도 11 — 화면 입력은 신뢰 경계
        raise ValueError([f"스프린트는 업데이트 요일의 날짜여야 합니다: {sprint_!r}"])
    if sprint_ and sprint_ < sprint.current(date.today()).isoformat():
        raise ValueError([f"이미 지난 업데이트입니다: {sprint.label(sprint_)} (진행 중이거나 그 뒤의 업데이트를 고르세요)"])
    final = {f: fields[f].strip() for f in FIELDS}
    needs_repro = assign.cs_repro(final, repro)
    assignment = {**assign.resolve(rules, team, final, needs_repro), "milestone": milestone, "sprint": sprint_}
    with _decision_lock:
        rec = store.get(rid)
        flow.check(rec["state"], flow.REGISTERED)
        changed = edits(rec["model_fields"], final)
        ref = store.ref(rid)
        try:   # 설계 의도 5 — 같은 리포트의 티켓이 이미 있으면 새로 만들지 않는다
            ticket = tracker.find(ref)
            if ticket:   # 설계 의도 7·9 — 그 티켓의 담당자·버전은 바꾸지 않으니 그룹도 읽지 않는다
                assignment = {**assignment, "linked_existing": True, "milestone": "", "sprint": "", "member": None, "qa_owner": None}
            else:
                who = assign.assignee(tracker, rules, assignment["team"], final,   # 설계 의도 10
                                      assign.recent(store.list(flow.REGISTERED)), member)
                assignment = {**assignment, "member": {"id": who["id"], "name": who["name"]} if who else None,
                              "member_rule": who.get("rule", ""),
                              "qa_owner": {"id": who["id"], "name": who["name"]} if who and assignment["team"] == rules["qa"] else None}
                ticket = tracker.create(Issue(report=rec["report"], fields=final, ref=ref, team=assignment["team"],
                                              milestone=milestone, assignee=who.get("id", ""), sprint=sprint_))
        except assign.BadChoice:   # 고른 구성원이 그룹에 없다 — 입력 문제라 검토 대기에 둔다
            raise
        except Exception as e:   # 설계 의도 4
            return store.move(rid, flow.FAILED, final_fields=final, edits=changed, assignment=assignment,
                              failed_step="등록", error=f"BTS 등록 실패: {e}")
        rec = store.move(rid, flow.REGISTERED, final_fields=final, edits=changed, assignment=assignment,
                         ticket_key=ticket.key, ticket_url=ticket.url, error=None, failed_step=None)
        if not assignment.get("linked_existing"):
            first_status(store, tracker, rid, assign.first_status(final, needs_repro))
        return rec


def first_status(store: Store, tracker, rid: int, target: str) -> None:
    """설계 의도 8 — 새 티켓의 상태가 target 이 아니면 바꾼다. 보류(Check)는 사유 코멘트가 필요해 CS 응대라고 남긴다."""
    try:
        before = tracker.status(store.get(rid)["ticket_key"])
    except Exception as e:
        store.save_qa(rid, qa.FIRST, None, None, error=f"BTS 상태를 읽지 못해 첫 상태({target})로 바꾸지 못했습니다: {e}")
        return
    if before != target:
        note = "[트리아지 서비스 · 첫 상태] CS 응대 건 — QA 재현이 필요 없어 개발 대응은 보류합니다" if target == CHECK else ""
        qa.write(store, tracker, rid, qa.FIRST, before, target, "", note)


def discard(store: Store, rid: int) -> dict:
    with _decision_lock:   # BTS 등록 도중 폐기되어 티켓만 남는 일을 막는다 (설계 의도 2)
        return store.move(rid, flow.DISCARDED)


def back_to_review(store: Store, rid: int) -> dict:
    """실패 → 검토 대기. BTS 등록에서 실패한 건만 — 사람이 확정한 8칸을 그대로 두고 다시 등록하게 한다 (설계 의도 6)."""
    with _decision_lock:
        rec = store.get(rid)
        if rec["state"] != flow.FAILED or rec["failed_step"] != "등록":
            raise flow.FlowError("BTS 등록 단계에서 실패한 건만 검토로 되돌릴 수 있습니다")
        return store.move(rid, flow.REVIEW, error=None, failed_step=None)


def retry(store: Store, worker, rid: int) -> dict:
    """실패 → 접수. 이전 처리 결과를 비우고 처음부터 다시 트리아지한다(지난 결과가 새 결과처럼 보이지 않게)."""
    with _decision_lock:
        rec = store.move(rid, flow.RECEIVED, error=None, failed_step=None, result=None, pipeline=None, routing=None,
                         model_fields=None, final_fields=None, edits=None, assignment=None)
    worker.submit(rid)
    return rec

