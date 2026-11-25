"""생성 시 자동 배정 — 사람이 확정한 8칸으로 BTS 티켓을 받을 팀을 정한다.

[설계 의도]
1. 모델이 처음 낸 8칸이 아니라 사람이 확정한 8칸으로 정한다. 검토에서 고친 값이 배정에 그대로 반영되고, 모델이 틀려도
   곧바로 엉뚱한 팀에 배정되지 않는다.
2. [처리]가 받을 쪽을 정하고, 개발로 넘기는 건은 [모듈]이 개발팀을 정한다. [처리] 값마다 누가 받는지는 8칸 형식의 뜻이라
   코드에 두고(아래 ROLE), 팀 이름과 모듈 → 팀 표는 회사마다 달라 설정 파일([assign])에 둔다.
   - 등록(재현 대기)·정보 요청 후 보류 → QA팀. 개발에 넘기기 전에 재현하거나 정보를 보완하는 일이 QA 의 일이다.
   - 개발 배정 → 첫 모듈의 개발팀. 모듈이 "해당 없음"이면 QA팀이 받아 나눈다.
   - 긴급 사인 요청 → QA팀(팀장). 판정 기준서 §0 "Critical 은 리드 QA 사인을 거친다" — 리드 QA 가 사인한 뒤 개발팀에
     넘긴다(qa.py 의 사인 동작). S5-4 까지는 개발팀으로 바로 보냈는데 기준서와 어긋나 S5-5a 에서 고쳤다.
   - CS 응대 → CS팀. 설정에 CS팀이 없으면 배정하지 않는다(BTS 밖에서 응대하는 회사도 있다). 폐기 → 배정하지 않는다.
3. 모듈이 여럿이면(판정 기준서 C-9의 병기) 첫 모듈의 팀에 배정한다. 나머지 모듈은 지금처럼 라벨로 티켓에 남는다.
4. 사람은 검토 화면에서 팀을 바꿀 수 있다. "규칙대로"는 등록하는 순간의 확정 8칸으로 다시 계산한다. 검토에서 모듈을
   고친 뒤 화면을 다시 읽지 않아도 고친 모듈의 팀으로 간다. 규칙이 고른 팀과 실제 팀을 함께 남겨, 사람이 바꾼 비율을
   나중에 대시보드에서 센다.
5. 설정에 [assign] 이 없으면 배정하지 않는다(S5 이전과 같은 동작). 자동 배정을 쓰지 않는 회사의 설정 파일을 깨지 않기 위해서다.
6. 티켓의 첫 상태도 [처리]로 정한다(first_status). QA 가 재현·보완할 건은 QA Review, 개발로 넘기는 건은 In Dev 다. 배정
   규칙과 달리 [assign] 이 없어도 정한다 — 상태는 팀 배정과 상관없이 BTS 흐름의 뜻이기 때문이다.
7. CS 응대는 QA 재현이 필요한 건만 QA Review·QA팀으로 보내고, 아니면 Check·CS팀으로 둔다(BTS 에 남기되 개발 대응은 보류).
   CS팀 이름은 설정 [assign] cs 다(예시는 "운영"). 재현이 필요한지는 기본값을 규칙으로 정하고([분류]가 결함이면 필요) 사람이 검토 화면에서 바꾼다. 사람 확인이 줄어도
   같은 규칙으로 동작하게 기본값을 비워 두지 않는다(2026-11-24 경민 결정).
8. 팀은 BTS 그룹이고 담당자는 그 구성원 한 사람이다(contracts 설계 의도 8). 긴급 사인 요청은 QA팀장, 나머지는 구성원 중
   지금 미해결이 가장 적은 사람이다(팀장 포함, 2026-11-25 경민 결정 — 사람 병목을 줄이려 팀장이 나누는 단계를 두지 않고
   사인이 필요한 Critical 에만 팀장을 둔다). 미해결이 같으면 서비스가 가장 오래전에 배정한(또는 한 번도 배정하지 않은) 사람이다.
   shortcut: "최근 배정"은 서비스 등록 기록(리포트 번호 순)만 본다 — 재현 확인·사인으로 넘긴 것은 세지 않고, 늦게 접수했지만
   먼저 등록한 건도 번호로 줄 세운다. 동률이 잦아 한쪽으로 쏠리면 QA 기록에도 담당자를 남기고 등록 시각 순으로 올린다.
10. 긴급 사인 요청은 자동 배정을 쓰는 동안 사람이 팀을 바꿔도 QA팀(팀장)으로 간다(판정 기준서 §0 — 사인 없이 다른 팀에 가지
   않게, 독립 검토에서 발견). 자동 배정을 쓰지 않으면([assign] 없음) 팀장도 없으므로 예전처럼 재현 확인으로 넘긴다(qa.py).
9. 사람은 검토 화면에서 구성원을 바꿀 수 있다. 등록하는 순간 그룹을 다시 읽어 그 팀 구성원인지 확인한다(화면을 연 뒤
   그룹이 바뀌었을 수 있다). 규칙이 고른 사람도 함께 남긴다.
"""

import math

from triage_eval.contracts import NO_MODULE, split_modules
from triage_service.contracts import CHECK, IN_DEV, QA_REVIEW, Member, TrackerError

AUTO = "규칙대로"
URGENT = "긴급 사인 요청"
ROLE = {   # 설계 의도 2 — [처리] 값 → 받는 쪽
    "등록(재현 대기)": "qa",
    "정보 요청 후 보류": "qa",
    "개발 배정": "module",
    URGENT: "lead",
    "CS 응대": "cs",
    "폐기": None,
}


def teams(rules: dict | None) -> list[str]:
    """설정에 나오는 팀 이름 전부(겹치지 않게, 처음 나온 순서). 검토 화면의 선택지와 BTS 계정 대응 검사에 쓴다."""
    if not rules:
        return []
    names = [rules["qa"], rules.get("cs", ""), *rules["modules"].values()]
    return [n for n in dict.fromkeys(names) if n]


def cs_repro(fields: dict[str, str], choice: str = AUTO) -> bool:
    """CS 응대 건에 QA 재현이 필요한가 (설계 의도 7). choice: 검토 화면의 선택(REPRO_CHOICES 의 키 또는 AUTO)."""
    return REPRO_CHOICES.get(choice, fields.get("분류") == "결함")


REPRO_CHOICES = {"필요": True, "필요 없음": False}


def first_status(fields: dict[str, str], repro: bool) -> str:
    """새 티켓의 BTS 상태 (설계 의도 6·7). 폐기는 등록하지 않으므로 오지 않는다."""
    role = ROLE.get(fields.get("처리", ""))
    return IN_DEV if role == "module" else CHECK if role == "cs" and not repro else QA_REVIEW


def dev_team(rules: dict | None, fields: dict[str, str]) -> str:
    """재현을 확인해 개발로 넘길 때의 팀 — [처리]를 개발 배정으로 본 규칙의 팀(qa.py)."""
    return suggest(rules, {**fields, "처리": "개발 배정"})


def suggest(rules: dict | None, fields: dict[str, str], repro: bool = False) -> str:
    """규칙이 고르는 팀. 배정하지 않으면 빈 문자열 (설계 의도 2·3·5·7). repro: CS 응대 건의 QA 재현 필요 여부."""
    if not rules:
        return ""
    role = ROLE.get(fields.get("처리", ""))
    if role == "cs" and repro:
        role = "qa"
    if role == "module":
        modules = [m for m in split_modules(fields.get("모듈", "")) if m != NO_MODULE]
        return rules["modules"].get(modules[0], rules["qa"]) if modules else rules["qa"]
    return {"qa": rules["qa"], "lead": rules["qa"], "cs": rules.get("cs", "")}.get(role, "")


def resolve(rules: dict | None, choice: str, fields: dict[str, str], repro: bool = False) -> dict:
    """검토 화면의 선택 → {"suggested": 규칙의 팀, "team": 실제로 배정할 팀}. 선택지에 없는 팀이면 ValueError (설계 의도 4)."""
    suggested = suggest(rules, fields, repro)
    if choice == AUTO or not rules:
        return {"suggested": suggested, "team": suggested}
    if choice and choice not in teams(rules):
        raise ValueError([f"설정에 없는 팀입니다: {choice!r}"])
    if fields.get("처리") == URGENT:   # 설계 의도 10 — 고른 팀이 있는 팀이어도 리드 QA 에게
        return {"suggested": suggested, "team": suggested}
    return {"suggested": suggested, "team": choice}


class BadChoice(ValueError):
    """검토 화면에서 고른 구성원이 그 팀 그룹에 없다 — 입력 문제(검토 대기에 둔다). BTS 응답 해석 오류(ValueError)와 구분한다."""


def recent(records: list[dict]) -> list[str]:
    """설계 의도 8 — 서비스가 배정한 구성원 ID, 최근 순. records 는 등록한 리포트(store.list(flow.REGISTERED), 번호 큰 순)."""
    return [r["assignment"]["member"]["id"] for r in records if r["assignment"] and r["assignment"].get("member")]


def candidates(tracker, team: str) -> list[tuple[Member, int]]:
    """설계 의도 8 — 팀 그룹의 구성원과 각자의 미해결 수. 구성원이 없으면 TrackerError."""
    members = tracker.members(team)
    if not members:
        raise TrackerError(f"{team} 팀 그룹에 구성원이 없습니다")
    return [(m, tracker.open_count(m.id)) for m in members]


def least_busy(cands: list[tuple[Member, int]], recent: list[str]) -> Member:
    """설계 의도 8 — 미해결이 가장 적은 사람, 같으면 가장 오래전에 배정한 사람. recent: 서비스가 배정한 구성원 ID(최근 순)."""
    last = {}
    for i, mid in enumerate(recent):
        last.setdefault(mid, i)   # 작을수록 최근
    return min(cands, key=lambda c: (c[1], -last.get(c[0].id, math.inf)))[0]


def assignee(tracker, rules: dict | None, team: str, fields: dict[str, str], recent: list[str], choice: str = AUTO) -> dict:
    """설계 의도 8·9 — {"id", "name", "rule": 규칙이 고른 ID}. 팀이 없으면 {}. 고른 사람이 팀 구성원이 아니면 ValueError.
    긴급 사인 요청이 QA팀으로 가면 QA팀장으로 고정한다(사람이 고른 구성원은 쓰지 않는다)."""
    if not team or not rules:
        return {}
    if fields.get("처리") == URGENT and team == rules["qa"]:
        lead = tracker.lead(team)
        name = next((m.name for m in tracker.members(team) if m.id == lead), lead)
        return {"id": lead, "name": name, "rule": lead}
    cands = candidates(tracker, team)
    rule = least_busy(cands, recent)
    if choice == AUTO:
        return {"id": rule.id, "name": rule.name, "rule": rule.id}
    chosen = next((m for m, _ in cands if m.id == choice), None)
    if chosen is None:
        raise BadChoice([f"{team} 팀 구성원이 아닙니다: {choice!r} (검토 화면을 다시 열어 고르세요)"])
    return {"id": chosen.id, "name": chosen.name, "rule": rule.id}
