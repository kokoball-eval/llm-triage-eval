"""v1.5 새 최종 평가용 세트 초안 — 문항 작성용 공통 틀 (계획: report/final_v15_plan.md).

[설계 의도]
1. 문항 작성 코드를 기존 최종 세트(tools/dataset_v14/final_v14_items.py)와 같은 모양(L·A·B 함수)으로 맞춰,
   검토자가 이전 세트와 같은 방식으로 읽을 수 있게 했다.
2. 업데이트 공지·알려진 이슈는 새 세트 전체가 같은 값을 쓴다. 중복 문항은 `known` 으로 해당 이슈만 덧붙인다.
3. 모든 문항은 `review="pending"` 초안으로 만든다. 검토자 승인 전에는 어떤 모델에도 넣지 않는다.
"""

UPDATE_NOTE = "v2.6.0 정기 업데이트: 2026-11-19(목) 오전 점검 후 적용"

# 리포트가 올라온 시점에 이미 BTS에 등록돼 있던 이슈 (중복 판정용)
KNOWN_BASE = [
    "[UI·텍스트] 상점 '교환' 탭 설명 문구 줄바꿈 오류 (v2.6.0)",
    "[네트워크] 길드 레이드 입장 대기열 표시가 간헐적으로 늦음 (v2.6.0)",
    "[그래픽·사운드] 신규 의상 '달빛 사냥꾼' 모자 반사광 과다 (v2.6.0)",
]
REVIEW = "pending"


def L(분류, 우선순위, 모듈, 재현정보, 처리, 빈도, human_review=False, risk=None,
      must_request=None, must_recommend=None):
    """정답 라벨. 각 칸은 허용 답 목록이고 첫 값이 가장 바람직한 답이다."""
    return {
        "분류": 분류, "우선순위": 우선순위, "모듈": 모듈, "재현 정보": 재현정보, "처리": 처리,
        "human_review": human_review, "risk": risk or [],
        "must_request": must_request or [], "must_recommend": must_recommend or [],
        "발생 빈도": 빈도,
    }


def base(id_, track, quality, inp, labels, rationale, known=None):
    inp = dict(inp)
    inp["context"] = {"update_note": UPDATE_NOTE, "known_issues": list(KNOWN_BASE) + list(known or [])}
    return {
        "id": id_, "set": "representative", "track": track, "split": "final",
        "quality": quality, "defect_types": [None],
        "input": inp, "labels": labels, "rationale": rationale,
        "provenance": {"scenario": "Claude 작성 (final_v15_plan.md 할당표)", "text": "Claude 작성",
                       "labels": "Claude 제안 → 검토 대기"},
        "review": REVIEW,
    }


def A(id_, quality, posted_at, board, title, body, labels, rationale, known=None):
    """트랙 A — 커뮤니티 제보."""
    return base(id_, "A", quality, {"board": board, "posted_at": posted_at, "title": title, "body": body},
                labels, rationale, known)


def B(id_, quality, reporter, title, env, rate, desc, expected, actual, ref, labels, rationale, known=None):
    """트랙 B — QA 작성 이슈(2차 B1 양식)."""
    body = (f"[테스트 환경]\n{env}\n\n[재현율]\n{rate}\n\n[이슈 설명]\n{desc}\n\n"
            f"[기대 결과]\n{expected}\n\n[실제 결과]\n{actual}\n\n[참고 문서]\n{ref}")
    return base(id_, "B", quality, {"reporter": reporter, "title": title, "body": body}, labels, rationale, known)


BOARD = "공식 홈페이지 > 버그 제보"
FREE = "자유게시판"
BUILD = "빌드 2.6.0.2231 (QA 빌드)"
