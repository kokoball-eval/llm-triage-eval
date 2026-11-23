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
"""

from triage_eval.contracts import NO_MODULE, split_modules

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


def suggest(rules: dict | None, fields: dict[str, str]) -> str:
    """규칙이 고르는 팀. 배정하지 않으면 빈 문자열 (설계 의도 2·3·5)."""
    if not rules:
        return ""
    role = ROLE.get(fields.get("처리", ""))
    if role == "module":
        modules = [m for m in split_modules(fields.get("모듈", "")) if m != NO_MODULE]
        return rules["modules"].get(modules[0], rules["qa"]) if modules else rules["qa"]
    return {"qa": rules["qa"], "cs": rules.get("cs", "")}.get(role, "")


def resolve(rules: dict | None, choice: str, fields: dict[str, str]) -> dict:
    """검토 화면의 선택 → {"suggested": 규칙의 팀, "team": 실제로 배정할 팀}. 선택지에 없는 팀이면 ValueError (설계 의도 4)."""
    suggested = suggest(rules, fields)
    if choice == AUTO or not rules:
        return {"suggested": suggested, "team": suggested}
    if choice and choice not in teams(rules):
        raise ValueError([f"설정에 없는 팀입니다: {choice!r}"])
    return {"suggested": suggested, "team": choice}
