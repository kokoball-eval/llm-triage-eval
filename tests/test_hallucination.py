"""날조 탐지기(evaluation/hallucination.py) 시험.

탐지기는 회귀 게이트의 합격 기준이 되므로 두 방향을 모두 고정한다.
- 놓치면 안 되는 것: v1.0 에서 Llama 3.1 8B 가 단문 리포트 "크래시남"에 지어낸 OS·수치
- 잡으면 안 되는 것: 입력에 근거가 있는 사실, 정보를 "요청"하는 문맥, 선택지 칸
"""

import pytest

from triage_eval.evaluation.hallucination import detect, is_grounded

def response(summary: str, missing: str, severity: str = "판단보류", repro: str = "불명확") -> str:
    return "\n".join([
        f"[요약]: {summary}",
        "[모듈]: 시스템",
        f"[심각도]: {severity}",
        f"[재현 여부]: {repro}",
        f"[누락 정보 및 권장 조치]: {missing}",
    ])


# ── 1. 잡아야 하는 것 ──────────────────────────────────────

@pytest.mark.parametrize("missing, category", [
    ("확인 필요 기기: Windows 10, Intel Core i5", "os"),
    ("NVIDIA GeForce GTX 1660 Ti 환경에서 발생", "hardware"),
    ("갤럭시 S23에서 재현됨", "device"),
    ("v2.4.0 빌드에서 확인", "version"),
    ("게임 시작 후 10분 이내에 오류 발생", "quantity"),
    ("WASD 키를 반복 입력하면 발생", "input"),
])
def test_each_category_is_caught_when_not_in_input(missing, category):
    result = detect(response("앱 강제 종료", missing), report_text="크래시남")
    assert result["hallucinated"] is True
    assert category in {u["category"] for u in result["ungrounded"]}


def test_summary_field_is_also_scanned():
    result = detect(response("Windows 10에서 앱 강제 종료", "추가 정보 필요"), report_text="크래시남")
    assert result["ungrounded"][0]["field"] == "요약"


# ── 2. 잡으면 안 되는 것 ───────────────────────────────────

def test_fact_present_in_input_is_grounded():
    report = "전투 중 보스 패턴 3단계 진입 시 크래시. iOS 17.4, v2.3.1에서 확인."
    result = detect(response("iOS 17.4에서 크래시", "v2.3.1 회귀 테스트 권장"), report)
    assert result["hallucinated"] is False


def test_reordered_tokens_are_grounded():
    """입력 "갤럭시 기종(S21)" → 응답 "갤럭시 S21" 은 근거 있음 (설계 의도 3)."""
    assert is_grounded("갤럭시 S21", "device", "특정 갤럭시 기종(S21)에서만 텍스처가 깨짐")


def test_quantity_must_match_exactly():
    """수치는 토큰 일부 일치로 넘기지 않는다. 입력의 "15초"가 응답의 "5초"를 정당화하지 않는다."""
    assert not is_grounded("5초", "quantity", "15초 후에 멈춤")
    assert is_grounded("15초", "quantity", "15초 후에 멈춤")


@pytest.mark.parametrize("missing", [
    "OS: Windows 10/11/Android/iOS 중 택1 확인 필요",          # 슬래시 나열 + 선택지
    "확인할 기기: 갤럭시 S23/아이폰 15",                        # 슬래시 나열만
    "사용 기기(예: 갤럭시 S23)와 OS 버전 확인 필요",            # 예시
    "iOS 17 또는 Android 14 중 어느 환경인지 확인",             # 또는
])
def test_request_context_is_hedged_not_hallucinated(missing):
    """정보를 요청하는 것은 트리아지의 정상 동작이다 (설계 의도 4)."""
    result = detect(response("앱 강제 종료 제보", missing), report_text="크래시남")
    assert result["hallucinated"] is False
    assert result["hedged"]


def test_enum_fields_are_not_scanned():
    """[재현 여부]: 발생(100%) 의 100% 를 수치 날조로 오인하지 않는다."""
    result = detect(response("앱 강제 종료", "추가 정보 필요", severity="Blocker", repro="발생(100%)"),
                    report_text="크래시남")
    assert result["hallucinated"] is False


@pytest.mark.parametrize("text", ["", None, "형식이 깨진 응답"])
def test_empty_or_broken_response_does_not_crash(text):
    assert detect(text, "크래시남")["hallucinated"] is False


# ── 3. 실제로 나온 날조 (v1.0 Llama 3.1 8B 응답 원문) ─────────────

Q08_REPORT = "크래시남"
Q08_RESPONSE = ("[요약]: 게임 내 전투 모드에서 플레이어 캐릭터가 무한히 이동하는 결함 현상 발생\n\n[모듈]: 전투\n\n"
                "[심각도]: Critical\n\n[재현 여부]: 발생(100%)\n\n[누락 정보 및 권장 조치]: 추가 확인이 필요한 기기/OS/재현스텝: "
                "PC, Windows 10, 전투 모드 시작 후 5초 이내에 플레이어 캐릭터가 무한히 이동하는 현상이 발생합니다. QA 권장 조치: "
                "이 현상을 재현할 수 있는 스텝을 추가로 확인하고, 관련된 코드를 분석하여 수정할 필요가 있습니다.")
Q02_REPORT = "결제 후 다이아가 지급되지 않음. 영수증은 있는데 재화가 반영 안 됨. 3명 유저가 동일 신고."
Q02_RESPONSE = ("[요약]: 결제 후 다이아 지급 오류\n\n[모듈]: 결제\n\n[심각도]: Critical\n\n[재현 여부]: 발생(100%)\n\n"
                "[누락 정보 및 권장 조치]: 3명 유저가 동일 신고한 것으로 보아 재현 가능성이 높습니다. 추가 확인이 필요한 "
                "기기/OS/재현스텝은 다음과 같습니다. - 기기: PC/콘솔/모바일 중 택1 - OS: Windows 10/11/Android/iOS 중 택1 "
                "- 재현스텝: 결제 후 다이아 지급 확인 단계에서 오류 발생 QA 권장 조치: 3명 유저의 신고를 토대로 재현 가능성을 "
                "높게 판단하여, 결제 후 다이아 지급 오류를 재현하고 해결책을 찾는 것이 우선되어야 합니다.")


def test_known_q08_fabrication_is_caught():
    """단문 리포트에 없는 OS(Windows 10)와 시간 조건(5초)을 지어낸 응답 — 탐지기를 만든 계기다."""
    r = detect(Q08_RESPONSE, Q08_REPORT)
    assert r["hallucinated"]
    assert {u["entity"] for u in r["ungrounded"]} == {"Windows 10", "5초"}


def test_option_list_request_is_hedged():
    """"OS: Windows 10/11/Android/iOS 중 택1" 은 정보를 요청하는 문맥이라 날조가 아니다."""
    r = detect(Q02_RESPONSE, Q02_REPORT)
    assert r["hallucinated"] is False and [h["entity"] for h in r["hedged"]] == ["Windows 10"]


@pytest.mark.parametrize("report, said", [
    ("추석 픽업 100번 뽑았는데 하나도 안 나옴", "100회"),   # ISSUE-008 v1.3 S17
    ("추석 픽업 100연 돌렸는데 하나도 안 나옴", "100회"),
    ("패키지 한 번 눌렀는데 결제가 두 번 됨", "1회"),        # ISSUE-008 재현 v1.4 S12
    ("보스방 들어가면 세 번 다 꺼짐", "3회"),
    ("다섯 차례 재접속해도 같음", "5회"),
])
def test_same_count_in_other_unit_is_grounded(report, said):
    """설계 의도 5 — 같은 수량을 다른 횟수 단위로 옮긴 것은 날조가 아니다."""
    assert not detect(response(f"{said} 시도 후 발생", "로그 확인"), report)["hallucinated"]


@pytest.mark.parametrize("report, said", [
    ("10번 뽑았는데 하나도 안 나옴", "100회"),               # 다른 수량은 계속 잡는다
    ("100번 뽑았는데 하나도 안 나옴", "10회"),
    ("3번째 시도에서 튕김", "3회"),                          # 순서(번째)는 횟수가 아니다
    ("주문번호 7 결제 실패", "7회"),                         # 번호는 횟수가 아니다
    ("한 번 눌렀는데 결제가 됨", "2회"),
    ("레이드 10초 후 튕김", "10회"),                         # 다른 단위의 수치는 근거가 아니다
])
def test_different_count_or_non_count_is_still_caught(report, said):
    """설계 의도 3·5 — 정규화는 같은 수량의 횟수 표현에만 적용된다."""
    assert detect(response(f"{said} 시도 후 발생", "로그 확인"), report)["hallucinated"]


def test_non_count_quantities_are_unchanged():
    """시간 단위는 정규화하지 않는다 — "15초"는 "5초"의 근거가 아니다 (ISSUE-006)."""
    assert detect(response("5초 후 멈춤", "로그 확인"), "15초 후에 멈춤")["hallucinated"]
