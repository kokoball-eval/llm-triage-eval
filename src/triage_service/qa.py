"""QA 동작 — 서비스 화면에서 BTS 티켓의 상태·담당 팀·코멘트를 바꾼다(재현 확인, 회귀 테스트 통과·실패, 보류, 코멘트).

[설계 의도]
1. BTS 가 원본이다. 서비스는 QA 가 누른 동작을 BTS 에 쓰고, 쓴 결과는 BTS 에서 다시 읽어 저장한다. 서비스가 상태를
   따로 정하지 않는다(sync.py 설계 의도 1).
2. 동작마다 할 수 있는 BTS 상태가 정해져 있다(ACTIONS). 쓰기 직전에 BTS 상태를 다시 읽고, 그 사이 개발자가 바꿔 허용
   상태가 아니면 쓰지 않는다. 화면은 마지막 동기화 값을 보여 주므로 오래된 화면에서 누른 동작을 여기서 막는다. 상태를 바꾸는
   동작은 두 번 눌러도 두 번째가 이미 바뀐 상태에 걸린다(코멘트만 하는 동작은 상태가 그대로라 두 번 달린다). 서버 프로세스
   안에서는 동기화 잠금(sync.Syncer.hold) 안에서 동작을 한 번에 하나씩 한다. 다시 읽기에 실패하면 보내지 않는다.
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
7. 긴급 사인 요청 건은 재현 확인 대신 [사인 후 개발 전달]로 넘긴다(판정 기준서 §0). QA팀장(설정 [<bts>.leads] 의 QA)만 할 수
   있고, 누르는 사람은 BTS 토큰 주인(tracker.me)으로 확인한다 — 서비스는 각자 PC 에서 자기 토큰으로 쓰기 때문이다. 넘길 때의
   팀·담당자 규칙은 재현 확인과 같다(설계 의도 5, 담당자는 개발팀 그룹 구성원 중 미해결이 가장 적은 사람). 넘긴 담당자 이름은
   BTS 코멘트 끝에 붙인다. 자동 배정을 쓰지 않으면([assign] 없음) 팀장이 없으므로 긴급 사인 요청 건도 재현 확인으로 넘긴다.
6. 등록 직후 첫 상태(assign.first_status)도 같은 길(write)로 쓴다. 새 티켓은 BTS 가 정한 첫 상태(QA Review)로 생기므로 다를
   때만 바꾼다. 실패해도 티켓은 이미 있으니 등록은 성공으로 두고 기록에 실패를 남긴다(review.py).
8. 회귀 테스트 실패는 In Dev 로 돌리면서 담당을 지금 담당 팀(team_now)의 구성원 중 미해결이 가장 적은 사람으로 바꾼다. 바로 개발로
   배정했다가 Resolve 접수(sync 설계 의도 14)로 QA 구성원이 담당이 된 건은 실패해도 담당이 QA 에 남아, 개발은 알림을 못 받고 QA 화면에서도
   사라지는 구멍이 있었다. QA 팀이었던 건은 [모듈] 규칙의 팀으로 가고, 그것도 QA팀이면(모듈 없음) 담당은 두고 상태만 바꾼다. 기존 이슈에 연결한 건도 담당은 건드리지 않는다. 자동 배정을 쓰지 않으면 상태만 바꾼다. 넘길 팀을 사람이 고르는
   입력은 두지 않는다(실패는 원래 팀으로 돌려주는 동작이라).
9. 사람별 로그인이면 누른 사람(actor)을 받는다: 긴급 사인은 누른 사람이 QA팀장인지 보고(없으면 예전처럼 BTS 토큰 주인), BTS 코멘트
   끝에 "(실행: 이름)"을 붙이고 QA 동작 기록에 남긴다. 동기화가 하는 쓰기(Resolve 접수·이월)는 사람이 없어 비워 둔다.
10. 리더(auth 설계 의도 6)는 긴급 사인을 할 수 있고(팀장 부재 시 파트장 대행 — QA팀장 확인을 건너뛴다), [담당 바꾸기]로 BTS 담당자를
   팀 구성원 중 한 사람으로 바꾼다(상태는 그대로, 코멘트 "(담당: 이름)", 기록에 남김). 누가 리더인지, 고른 사람이 실제 구성원인지는
   화면 쪽(web/app.py)이 로그인한 사람과 동기화로 읽은 구성원 표로 확인해 넘긴다.
"""

from triage_service import assign, flow
from triage_service.contracts import CHECK, CLOSED, IN_DEV, OPEN, QA_REVIEW, RESOLVE
from triage_service.store import Store, now

INTAKE = "회귀 접수"   # 동기화가 하는 유일한 BTS 쓰기의 기록 이름 (sync 설계 의도 14)
# 동작 → (화면 이름, 할 수 있는 BTS 상태, 목표 상태, 코멘트 필수) — 설계 의도 2·4
ACTIONS = {
    "repro": ("재현 확인", (QA_REVIEW,), IN_DEV, True),
    "sign": ("사인 후 개발 전달", (QA_REVIEW,), IN_DEV, False),   # 설계 의도 7 — 긴급 사인 요청 건만
    "pass": ("회귀 테스트 통과", (RESOLVE,), CLOSED, False),
    "fail": ("회귀 테스트 실패", (RESOLVE,), IN_DEV, True),
    "hold": ("보류", (QA_REVIEW, IN_DEV, RESOLVE), CHECK, True),
    "comment": ("코멘트", OPEN, "", True),
    "reassign": ("담당 바꾸기", OPEN, "", False),   # 설계 의도 10 — 리더만(화면이 따로 그린다)
}
FIRST = "첫 상태"   # 등록 직후 첫 상태를 쓴 기록의 동작 이름 (설계 의도 6)


class Conflict(Exception):
    """BTS 상태가 그 동작을 할 수 없는 상태다 (설계 의도 2)."""


def allowed(status: str | None, urgent: bool = False) -> list[str]:
    """이 BTS 상태에서 할 수 있는 동작 (화면이 버튼을 고를 때). urgent: 긴급 사인 요청 건(설계 의도 7)."""
    skip = "repro" if urgent else "sign"
    return [a for a, (_, ok, _, _) in ACTIONS.items() if status in ok and a not in (skip, "reassign")]


def team_now(rec: dict, log: list[dict]) -> str:
    """지금 담당 팀(설계 의도 8) — 재현 확인으로 바꾼 팀이 있으면 그것, 없으면 등록 때 배정한 팀, 없으면 빈 문자열."""
    moved = [a["team"] for a in log if a["team"]]
    return moved[-1] if moved else (rec["assignment"] or {}).get("team") or ""


def urgent(rec: dict) -> bool:
    return (rec.get("final_fields") or {}).get("처리") == assign.URGENT


def act(store: Store, tracker, rid: int, action: str, comment: str = "", team: str = assign.AUTO,
        rules: dict | None = None, actor_bts: str = "", actor_name: str = "", actor_leader: bool = False,
        assignee: str = "", assignee_name: str = "") -> dict:
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
    if action == "reassign" and not assignee:   # 설계 의도 10
        raise ValueError(["누구에게 맡길지 고르세요"])
    signing = urgent(rec) and bool(rules)   # 설계 의도 7
    if action in ("repro", "sign") and action not in allowed(QA_REVIEW, signing):
        raise ValueError(["긴급 사인 요청 건은 [사인 후 개발 전달]로 넘깁니다" if signing
                          else "[사인 후 개발 전달]은 자동 배정을 쓸 때 긴급 사인 요청 건에만 씁니다"])
    new_team = ""
    if action in ("repro", "sign") and rules:   # 설계 의도 5
        new_team = assign.dev_team(rules, rec["final_fields"]) if team == assign.AUTO else team
        if new_team not in assign.teams(rules):
            raise ValueError([f"설정에 없는 팀입니다: {new_team!r}"])
        if new_team == rules["qa"]:
            raise ValueError([f"{label}은(는) 개발팀으로 넘기는 동작입니다. [모듈]로 팀을 정할 수 없으니 넘길 팀을 고르세요"])
    if action == "fail" and rules and not (rec["assignment"] or {}).get("linked_existing"):   # 설계 의도 8 — 기존 이슈에 연결한 건은 담당을 건드리지 않는다
        new_team = team_now(rec, store.qa_log().get(rid, []))
        if new_team not in assign.teams(rules) or new_team == rules["qa"]:   # QA팀·모르는 팀에 있던 건은 모듈 규칙의 팀으로
            new_team = assign.dev_team(rules, rec["final_fields"])
        if new_team == rules["qa"]:   # [모듈]이 없어 규칙도 QA팀이면 담당은 두고 상태만 바꾼다(QA 로 되돌리지 않는다)
            new_team = ""
    who = {}
    try:
        if action == "sign" and not actor_leader and (actor_bts or tracker.me()) != tracker.lead(rules["qa"]):   # 설계 의도 9 — 누른 사람, 없으면 토큰 주인
            raise assign.BadChoice([f"{rules['qa']}팀장만 사인할 수 있습니다" + ("" if actor_bts else " (BTS 토큰 주인이 설정의 팀장이 아님)")])
        if action == "reassign":   # 설계 의도 10
            who = {"id": assignee, "name": assignee_name or assignee}
        if new_team:   # 설계 의도 5·7 — 개발팀 그룹에서 한 사람
            who = assign.assignee(tracker, rules, new_team, {**rec["final_fields"], "처리": "개발 배정"},
                                  assign.recent(store.list(flow.REGISTERED)))
    except assign.BadChoice:
        raise
    except Exception as e:
        raise Conflict(f"BTS 계정·그룹을 읽지 못해 보내지 않았습니다: {e}")
    try:   # 설계 의도 2
        before = tracker.status(rec["ticket_key"])
    except Exception as e:
        raise Conflict(f"BTS 상태를 읽지 못해 보내지 않았습니다: {e}")
    if before not in ok:
        _remember(store, tracker, rid)
        raise Conflict(f"BTS 상태가 지금 {before} 라 {label}을(를) 할 수 없습니다. 홈에서 [지금 동기화]를 누른 뒤 다시 확인하세요")
    note = f"[트리아지 서비스 · {label}] {comment}".rstrip() if comment or who or actor_name else ""
    if who:
        note += f" (담당: {who['name']})"
    if actor_name:   # 설계 의도 9
        note += f" (실행: {actor_name})"
    return write(store, tracker, rid, label, before, target, new_team, note, who.get("id", ""), actor_name)

def write(store: Store, tracker, rid: int, label: str, before: str | None, target: str, team: str, note: str,
          assignee: str = "", actor: str = "") -> dict:
    """BTS 에 쓰고 다시 읽어 확인한 뒤 기록한다 (설계 의도 3). target 이 비면 상태는 그대로 두는 동작이다."""
    key = store.get(rid)["ticket_key"]
    error = None
    try:
        tracker.update(key, status=target, team=team, comment=note, assignee=assignee)
    except Exception as e:   # 설계 의도 3 — 도중에 실패하면 팀이 바뀌었는지 모른다
        error = f"BTS 쓰기 실패: {e}" + (f" (담당 팀 {team} 변경 여부는 BTS 에서 확인하세요)" if team else "")
        team = ""
    after = _remember(store, tracker, rid)
    if error is None and target and after != target:
        error = f"BTS 가 바꾸지 않았습니다 (목표 {target}, 지금 {after or '읽지 못함'}) — BTS 워크플로를 확인하세요"
    store.save_qa(rid, label, before, after, team, note, error, actor)
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
