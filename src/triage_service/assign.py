"""생성 시 자동 배정 — 사람이 확정한 8칸으로 BTS 티켓을 받을 팀을 정한다.

[설계 의도]
1. 모델이 처음 낸 8칸이 아니라 사람이 확정한 8칸으로 정한다. 검토에서 고친 값이 배정에 그대로 반영되고, 모델이 틀려도
   곧바로 엉뚱한 팀에 배정되지 않는다.
2. [처리]가 받을 쪽을 정하고, 개발로 넘기는 건은 [모듈]이 개발팀을 정한다. [처리] 값마다 누가 받는지는 8칸 형식의 뜻이라
   코드에 두고(아래 ROLE), 팀 이름과 모듈 → 팀 표는 회사마다 달라 설정 파일([assign])에 둔다.
   - 등록(재현 대기)·정보 요청 후 보류 → QA팀. 개발에 넘기기 전에 재현하거나 정보를 보완하는 일이 QA 의 일이다.
   - 개발 배정·긴급 사인 요청 → 첫 모듈의 개발팀. 모듈이 "해당 없음"이면 QA팀이 받아 나눈다.
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
"""

from triage_eval.contracts import NO_MODULE, split_modules
from triage_service.contracts import CHECK, IN_DEV, QA_REVIEW

AUTO = "규칙대로"
ROLE = {   # 설계 의도 2 — [처리] 값 → 받는 쪽
    "등록(재현 대기)": "qa",
    "정보 요청 후 보류": "qa",
    "개발 배정": "module",
    "긴급 사인 요청": "module",
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
    return {"qa": rules["qa"], "cs": rules.get("cs", "")}.get(role, "")


def resolve(rules: dict | None, choice: str, fields: dict[str, str], repro: bool = False) -> dict:
    """검토 화면의 선택 → {"suggested": 규칙의 팀, "team": 실제로 배정할 팀}. 선택지에 없는 팀이면 ValueError (설계 의도 4)."""
    suggested = suggest(rules, fields, repro)
    if choice == AUTO or not rules:
        return {"suggested": suggested, "team": suggested}
    if choice and choice not in teams(rules):
        raise ValueError([f"설정에 없는 팀입니다: {choice!r}"])
    return {"suggested": suggested, "team": choice}
