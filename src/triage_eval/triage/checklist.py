"""Critical 체크리스트(방법 M2) — 첫 응답 뒤 예/아니오 질문 6개를 묻고, Critical 로 올릴지는 코드가 정한다.

방법 비교(v1.4)에서 판정 예시를 넣는 방법(M1)과 함께 시험해 채택된 방법이다. 7B 모델은 Critical 을 놓치는 일이 많은데,
"Critical 인가?"를 통째로 묻는 대신 기준서의 Critical 조건을 사실 질문으로 쪼개 물으면 놓침이 줄었다.

[설계 의도]
1. 질문 문구는 판정 기준서의 Critical 조건(§4.1, F-2)을 그대로 옮긴 6문항이다. 6번은 서버 장애와 민감한 텍스트를 함께 묻되
   답을 "서버 장애 / 민감한 텍스트 / 아니오" 중 하나로 받는다. 재현 정보가 부족할 때 서버 장애만 예외로 올리는 규칙
   (기준서 H-3a·H-1b)을 적용하려면 이 구분이 필요하다.
2. 올리기만 한다. 첫 응답이 이미 Critical 이면 답을 기록만 하고 바꾸지 않는다. [우선순위] 줄만 바꾸고,
   바뀐 우선순위는 후처리 안전장치 H-1 이 긴급 사인 요청으로 보낸다.
3. 판단 칸(분류·우선순위·재현 정보)이 허용 값 밖이면 바꾸지 않는다. 허용 값 밖인 값을 Critical 로 덮으면 형식 위반이 가려진다.
4. 질문·스키마의 지문(assets_sha256)을 실행 기록에 남긴다. 회귀 게이트는 기준선과 이 값이 다르면 "방법 구성 변경"으로 알린다.
"""

import hashlib

from triage_eval.contracts import ENUMS, replace_field

YES, NO = "예", "아니오"
Q6_CHOICES = ["서버 장애", "민감한 텍스트", NO]
CHECKLIST = [
    "이 현상 때문에 게임 실행·로그인·메인 진행을 할 수 없고, 우회 방법이 없는가?",
    "결제 후 미지급·이중 결제처럼 유료 결제에 피해가 있는가?",
    "캐릭터·아이템·재화 데이터가 사라지거나 이전 상태로 되돌아갔는가?",
    "재화·보상·입장 기회를 반복해서 얻는 악용이 가능하거나, 승패·랭킹 기록이 남지 않는가?",
    "공지된 조정 내용과 실제 수치가 크게 다른가?",
    "여러 유저가 동시에 겪는 서버 장애로 보이는가, 또는 분쟁 지역 명칭·필터를 우회한 욕설처럼 민감한 텍스트 오류인가?",
]
KEYS = [f"q{i}" for i in range(1, 7)]
SCHEMA = {
    "type": "object",
    "properties": {**{k: {"type": "string", "enum": [YES, NO]} for k in KEYS[:5]},
                   "q6": {"type": "string", "enum": Q6_CHOICES}},
    "required": KEYS,
}
POLICY = {"asked_when": "분류=결함", "rule": "upgrade_only", "num_predict": 96}


def checklist_text() -> str:
    lines = ["", "[Critical 확인 질문]",
             "위 리포트와 1차 판정을 보고, 리포트에 적힌 내용만으로 답하세요. 확실하지 않으면 아니오입니다."]
    lines += [f"{n}. {q}" for n, q in enumerate(CHECKLIST, start=1)]
    lines.append("JSON으로 답하세요. 키는 q1~q6입니다. q1~q5의 값은 예 또는 아니오, "
                 "q6의 값은 서버 장애, 민감한 텍스트, 아니오 중 하나입니다.")
    return "\n".join(lines)


def build_prompt(base_prompt: str, first_response: str) -> str:
    return f"{base_prompt}\n\n[1차 판정]\n{first_response.strip()}\n{checklist_text()}"


def should_ask(pred: dict) -> bool:
    return pred.get("분류") == "결함"


def decide_upgrade(pred: dict, answers: dict) -> tuple[bool, str]:
    """설계 의도 2·3 — (올릴지, 사유). answers 는 q1~q6 값."""
    if any(pred.get(f) not in ENUMS[f] for f in ("분류", "우선순위", "재현 정보")):
        return False, "판단 필드 허용 값 밖 — 변경 안 함"
    if pred["우선순위"] == "Critical":
        return False, "이미 Critical — 기록만"
    if not answers or any(k not in answers for k in KEYS):
        return False, "체크리스트 답 없음"
    server = answers["q6"] == "서버 장애"
    any_yes = any(answers[k] == YES for k in KEYS[:5]) or answers["q6"] != NO
    if pred["재현 정보"] == "부족":
        return (True, "재현 정보 부족이지만 서버 장애 의심 (H-1b)") if server else (False, "재현 정보 부족 (H-3a)")
    return (True, "체크리스트 '예' — Critical") if any_yes else (False, "체크리스트 모두 아니오")


def apply_upgrade(text: str) -> str:
    return replace_field(text, "우선순위", "Critical")


def assets_sha256() -> str:
    """설계 의도 4. 계산식은 방법 비교 때와 같다(예시 블록 자리는 빈 문자열) — 기준선 실행 기록의 값과 비교되기 때문이다."""
    parts = ["", checklist_text(), repr(SCHEMA)]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
