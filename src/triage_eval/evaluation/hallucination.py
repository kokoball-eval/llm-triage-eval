"""날조 탐지기 — 응답에 입력 리포트에 없는 구체 사실(기기·OS·버전·수치·조작)이 있는지 규칙으로 찾는다.

7B·8B 모델이 단문 리포트 "크래시남"에 "Windows 10, GTX 1660 Ti, 게임 시작 후 10분 이내"처럼 없는 조건을 지어냈는데,
형식은 정상이라 회귀 게이트를 통과한 일이 있었다. 이 탐지기는 그 공백을 메운다.

[설계 의도]
1. LLM 이 아니라 규칙으로 판정한다. 판정이 매번 같아야 회귀 게이트의 기준으로 쓸 수 있다.
   (LLM 채점관은 v1.5에서 사람 판정과의 일치율을 재 보았으나 기준 미달로 채택하지 않았다.)
2. "구체 사실"만 본다. 문장 의미의 날조는 보지 않는다. 의미 비교는 정상적인 바꿔 말하기까지 날조로 오판하기 쉽다.
   한계: 요약 내용 자체를 지어낸 경우는 잡지 못한다(새 최종 세트에서 사람이 본 날조 23건 중 0건을 잡았다).
3. 근거 확인은 "구성 토큰이 모두 입력에 있는가"로 한다("갤럭시 기종(S21)" → "갤럭시 S21"은 근거 있음).
   수치는 "5초"와 "15초"를 구분해야 하므로 붙여 쓴 형태가 그대로 있어야 한다.
   횟수는 단위가 달라도 같은 수량이면 근거로 본다("100번" → "100회", "한 번" → "1회").
4. 요청·예시 문맥("OS: Windows 10/11 중 택1 확인 필요")은 따로 기록(hedged)하되 날조로 세지 않는다.
   정보를 되묻는 것은 트리아지 응답의 정상 동작이다.
"""

import re

from triage_eval.contracts import parse_lines

SCANNED_FIELDS = ("요약", "누락 정보 및 권장 조치")

ENTITY_PATTERNS = {
    "os": r"(?:windows|win)\s?\d+(?:\.\d+)?|ios\s?\d+(?:\.\d+)*|(?:android|안드로이드)\s?\d+(?:\.\d+)?|macos(?:\s?\d+)?",
    "hardware": r"nvidia|geforce|(?:gtx|rtx|rx)\s?\d{3,4}(?:\s?ti)?|radeon|intel|core\s?i\d|ryzen(?:\s?\d)?"
                r"|snapdragon|스냅드래곤|exynos|엑시노스",
    "device": r"(?:galaxy|갤럭시)\s?[a-z]{0,2}\d+|(?:iphone|아이폰)\s?\d+|ipad|아이패드|pixel\s?\d+",
    "version": r"v\d+(?:\.\d+)+|(?<![\d.])\d+\.\d+\.\d+(?![\d.])",
    # 앞에 숫자·마침표가 붙으면 버전 번호의 일부이므로 제외 ("v2.3.1 회귀"의 "3.1 회"),
    # "회귀"의 "회"는 횟수 단위가 아니므로 제외
    "quantity": r"(?<![\d.])\d+(?:\.\d+)?\s?(?:초|분|시간|프레임|fps|mb|gb|회(?!귀)|번째)",
    "input": r"wasd|(?:ctrl|alt|shift|esc|space|스페이스(?:바)?|f\d{1,2})\s?키?|[a-z]\s?키|마우스\s?(?:좌|우)?클릭",
}
COMPILED = {cat: re.compile(p) for cat, p in ENTITY_PATTERNS.items()}

# 요청·예시 문맥 표지 (설계 의도 4)
HEDGE_BEFORE = re.compile(r"(?:예:|예\)|\(예|예를 들|예시|e\.g\.)[^.\n]{0,15}$")
HEDGE_AFTER = re.compile(r"^[^.\n]{0,20}(?:중 택|중 하나|중에서|또는|여부 확인|인지 확인)")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _tokens(entity: str) -> list[str]:
    return re.findall(r"[a-z]+|\d+(?:\.\d+)*|[가-힣]+", entity.lower())


# 횟수 표현 (설계 의도 3) — 숫자 + 횟수 단위, 고유어 수사 + 번/차례
COUNT_DIGIT = re.compile(r"(?<![\d.])(\d+)\s?(?:번(?!째|호|지)|회(?!귀)|연(?:차)?|차례)")
NATIVE_NUMBERS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}
COUNT_NATIVE = re.compile(r"(?<![가-힣])(" + "|".join(sorted(NATIVE_NUMBERS, key=len, reverse=True))
                          + r")\s?(?:번(?!째|호|지)|차례)")
COUNT_ENTITY = re.compile(r"(\d+)\s?회")


def counts_in(text: str) -> set[int]:
    """입력에 나온 횟수(수량)의 집합 (설계 의도 3)."""
    lowered = (text or "").lower()
    found = {int(n) for n in COUNT_DIGIT.findall(lowered)}
    found |= {NATIVE_NUMBERS[w] for w in COUNT_NATIVE.findall(lowered)}
    return found


def is_grounded(entity: str, category: str, report_text: str) -> bool:
    """후보가 입력 리포트에 근거가 있는지 (설계 의도 3)."""
    source = _normalize(report_text)
    if category != "quantity" and _normalize(entity) in source:
        return True
    if category == "quantity":
        # "5초"가 "15초" 안에 들어 있다고 근거로 치지 않는다 (앞에 숫자가 붙으면 다른 수치)
        if re.search(r"(?<![\d.])" + re.escape(_normalize(entity)), source):
            return True
        count = COUNT_ENTITY.fullmatch(_normalize(entity))
        return bool(count) and int(count.group(1)) in counts_in(report_text)  # 설계 의도 3
    return all(tok in source for tok in _tokens(entity))


def is_hedged(text: str, start: int, end: int) -> bool:
    """후보가 슬래시 나열·예시·선택지 같은 요청 문맥 안에 있는지 (설계 의도 4)."""
    before, after = text[:start], text[end:]
    if before.rstrip().endswith("/") or after.lstrip().startswith("/"):
        return True
    return bool(HEDGE_BEFORE.search(before) or HEDGE_AFTER.search(after))


def detect(response_text: str, report_text: str) -> dict:
    """응답 1건을 검사한다. hallucinated 가 True 이면 근거 없는 구체 사실이 1개 이상 있다."""
    fields = {lbl: val for lbl, val, _ in parse_lines((response_text or "").strip()) if lbl in SCANNED_FIELDS}
    ungrounded, hedged = [], []
    for field, value in fields.items():
        lowered = value.lower()
        for category, pattern in COMPILED.items():
            for m in pattern.finditer(lowered):
                entity = m.group(0).strip()
                if is_grounded(entity, category, report_text):
                    continue
                item = {"category": category, "entity": value[m.start():m.end()].strip(), "field": field}
                (hedged if is_hedged(lowered, m.start(), m.end()) else ungrounded).append(item)
    return {"hallucinated": bool(ungrounded), "ungrounded": ungrounded, "hedged": hedged}
