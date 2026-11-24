"""QA 동작 — 서비스 화면에서 BTS 티켓의 상태·담당 팀·코멘트를 바꾼다(재현 확인, 회귀 테스트 통과·실패, 보류, 코멘트).

[설계 의도]
1. BTS 가 원본이다. 서비스는 QA 가 누른 동작을 BTS 에 쓰고, 쓴 결과는 BTS 에서 다시 읽어 저장한다. 서비스가 상태를
   따로 정하지 않는다(sync.py 설계 의도 1).
2. 동작마다 할 수 있는 BTS 상태가 정해져 있다(ACTIONS). 쓰기 직전에 BTS 상태를 다시 읽고, 그 사이 개발자가 바꿔 허용
   상태가 아니면 쓰지 않는다. 화면은 마지막 동기화 값을 보여 주므로 오래된 화면에서 누른 동작을 여기서 막는다. 상태를 바꾸는
   동작은 두 번 눌러도 두 번째가 이미 바뀐 상태에 걸린다(코멘트만 하는 동작은 상태가 그대로라 두 번 달린다). 서버 프로세스
   안에서는 잠금 하나로 동작을 한 번에 하나씩 한다. 다시 읽기에 실패하면 보내지 않는다.
3. 쓴 뒤에 상태를 다시 읽어 목표 상태가 됐는지 확인한다. Redmine 은 워크플로가 허용하지 않는 상태 변경을 오류 없이
   무시할 수 있고, Jira 는 상태·코멘트를 따로 보내 일부만 될 수 있다. 결과가 목표와 다르거나 도중에 실패하면 그대로
   "실패"로 기록하고 지금 BTS 상태와 함께 보인다(대체 처리 없음). 확인 읽기는 단건 조회(tracker.status)로 한다 — Jira 검색
   (snapshot)은 색인이라 방금 쓴 값이 늦게 보일 수 있다. 쓰기 요청이 도중에 실패하면 팀이 바뀌었는지 알 수 없으므로 기록의
   팀은 비우고 오류 문구에 BTS 에서 확인하라고 적는다(보드는 기록의 팀을 지금 팀으로 센다, sync 설계 의도 7).
4. 사유가 필요한 동작(재현 확인·회귀 실패·보류·코멘트)은 코멘트 없이 보내지 않는다. 보류(Check)는 사유를 코멘트로 남긴다는
   팀 규칙(2026-11-24)을 따른다. 코멘트 앞에는 "[트리아지 서비스 · 동작]"을 붙여 BTS 에서 서비스가 쓴 것임을 알린다.
5. 재현 확인은 In Dev 로 넘기면서 담당을 개발팀으로 바꾼다. 기본 팀은 확정한 8칸의 첫 모듈 팀(assign.dev_team)이고 QA 가
   바꿀 수 있다. 자동 배정을 쓰지 않으면 상태만 바꾼다. 개발로 넘기는 동작이라 QA팀으로는 보내지 않는다 — [모듈]이 "해당 없음"
   이라 규칙이 QA팀을 고르면 사람이 팀을 골라야 한다.
6. 등록 직후 첫 상태(assign.first_status)도 같은 길(write)로 쓴다. 새 티켓은 BTS 가 정한 첫 상태(QA Review)로 생기므로 다를
   때만 바꾼다. 실패해도 티켓은 이미 있으니 등록은 성공으로 두고 기록에 실패를 남긴다(review.py).
"""

import threading

from triage_service import assign
from triage_service.contracts import CHECK, CLOSED, IN_DEV, QA_REVIEW, RESOLVE
from triage_service.store import Store, now

OPEN = (QA_REVIEW, IN_DEV, RESOLVE, CHECK)
# 동작 → (화면 이름, 할 수 있는 BTS 상태, 목표 상태, 코멘트 필수) — 설계 의도 2·4
ACTIONS = {
    "repro": ("재현 확인", (QA_REVIEW,), IN_DEV, True),
    "pass": ("회귀 테스트 통과", (RESOLVE,), CLOSED, False),
    "fail": ("회귀 테스트 실패", (RESOLVE,), IN_DEV, True),
    "hold": ("보류", (QA_REVIEW, IN_DEV, RESOLVE), CHECK, True),
    "comment": ("코멘트", OPEN, "", True),
}
FIRST = "첫 상태"   # 등록 직후 첫 상태를 쓴 기록의 동작 이름 (설계 의도 6)
_lock = threading.Lock()


class Conflict(Exception):
    """BTS 상태가 그 동작을 할 수 없는 상태다 (설계 의도 2)."""


def allowed(status: str | None) -> list[str]:
    """이 BTS 상태에서 할 수 있는 동작 (화면이 버튼을 고를 때)."""
    return [a for a, (_, ok, _, _) in ACTIONS.items() if status in ok]


def act(store: Store, tracker, rid: int, action: str, comment: str = "", team: str = assign.AUTO,
        rules: dict | None = None) -> dict:
    """QA 동작 하나. 입력 문제는 ValueError(문제 목록), 상태가 맞지 않으면 Conflict. 결과 {"ok", "after", "error"}."""
    rec = store.get(rid)
    if action not in ACTIONS:
        raise ValueError([f"모르는 동작입니다: {action!r}"])
    label, ok, target, needs_comment = ACTIONS[action]
    if not rec or not rec["ticket_key"]:
        raise ValueError(["BTS 티켓이 없는 리포트입니다"])
    comment = comment.strip()
    if needs_comment and not comment:
        raise ValueError([f"{label}에는 코멘트(사유)가 필요합니다"])
    new_team = ""
    if action == "repro" and rules:   # 설계 의도 5
        new_team = assign.dev_team(rules, rec["final_fields"]) if team == assign.AUTO else team
        if new_team not in assign.teams(rules):
            raise ValueError([f"설정에 없는 팀입니다: {new_team!r}"])
        if new_team == rules["qa"]:
            raise ValueError(["재현 확인은 개발팀으로 넘기는 동작입니다. [모듈]로 팀을 정할 수 없으니 넘길 팀을 고르세요"])
    with _lock:
        try:   # 설계 의도 2
            before = tracker.status(rec["ticket_key"])
        except Exception as e:
            raise Conflict(f"BTS 상태를 읽지 못해 보내지 않았습니다: {e}")
        if before not in ok:
            _remember(store, tracker, rid)
            raise Conflict(f"BTS 상태가 지금 {before} 라 {label}을(를) 할 수 없습니다. QA 화면에서 동기화한 뒤 다시 확인하세요")
        note = f"[트리아지 서비스 · {label}] {comment}" if comment else ""
        return write(store, tracker, rid, label, before, target, new_team, note)


def write(store: Store, tracker, rid: int, label: str, before: str | None, target: str, team: str, note: str) -> dict:
    """BTS 에 쓰고 다시 읽어 확인한 뒤 기록한다 (설계 의도 3). target 이 비면 상태는 그대로 두는 동작이다."""
    key = store.get(rid)["ticket_key"]
    error = None
    try:
        tracker.update(key, status=target, team=team, comment=note)
    except Exception as e:   # 설계 의도 3 — 도중에 실패하면 팀이 바뀌었는지 모른다
        error = f"BTS 쓰기 실패: {e}" + (f" (담당 팀 {team} 변경 여부는 BTS 에서 확인하세요)" if team else "")
        team = ""
    after = _remember(store, tracker, rid)
    if error is None and target and after != target:
        error = f"BTS 가 바꾸지 않았습니다 (목표 {target}, 지금 {after or '읽지 못함'}) — BTS 워크플로를 확인하세요"
    store.save_qa(rid, label, before, after, team, note, error)
    return {"ok": error is None, "after": after, "error": error}


def _remember(store: Store, tracker, rid: int) -> str | None:
    """지금 BTS 상태를 단건 조회로 읽어 저장한다(sync 와 같은 기록, 설계 의도 3). 읽지 못하면 None.
    담당자·변경 시각은 검색(snapshot)으로 읽고, 못 읽으면 이전 값을 둔다."""
    rec = store.get(rid)
    key, old = rec["ticket_key"], rec["bts"] or {}
    try:
        status = tracker.status(key)
    except Exception:
        return None
    try:
        state = tracker.snapshot([key]).get(key)
    except Exception:
        state = None
    at = now()
    store.save_bts(rid, {"status": status, "assignee": state.assignee if state else old.get("assignee", ""),
                         "updated": state.updated if state else old.get("updated", ""), "synced_at": at})
    store.ticket_status(key, status, at)   # 마일스톤 성과도 다음 동기화를 기다리지 않는다 (store 설계 의도 11)
    return status
