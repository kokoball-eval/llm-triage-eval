"""상태 흐름 — 리포트 한 건이 거칠 수 있는 상태와 허용 전이.

    접수 → 트리아지 중 → 검토 대기 → 등록 완료
                                   ↘ 폐기
    (접수·트리아지 중·검토 대기) → 실패 → 접수 (다시 시도)
                               실패 → 검토 대기 (BTS 등록 단계에서 실패한 건만, review.back_to_review)

[설계 의도]
1. 전이를 표 하나로 정하고 저장소(store.move)가 상태를 바꿀 때마다 확인한다. 예를 들어 사람이 보지 않은 건이
   "검토 대기"를 건너뛰고 "등록 완료"가 되는 일을 코드가 막는다(9단계 판정 미채택 → 모든 건이 사람 확인을 거친다).
2. 실패는 실패로 남긴다. 가져오기·트리아지·등록이 실패하면 대체 데이터를 만들지 않고 "실패"와 오류 내용을 남긴다.
   사람이 [다시 시도]를 누르면 접수부터 다시 한다.
3. 실패 → 검토 대기는 BTS 등록에서 실패한 건만 허용한다(review.back_to_review 가 실패 단계를 확인한다). 사람이 이미
   확정한 8칸이 있으므로 모델을 다시 돌릴 필요가 없다. 트리아지에서 실패한 건은 검토할 8칸이 없어 접수부터 다시 한다.
4. 등록 완료·폐기는 끝 상태다. 되돌리려면 BTS 에서 처리한다(서비스와 BTS 의 상태가 갈라지지 않게).
"""

RECEIVED, TRIAGING, REVIEW, REGISTERED, DISCARDED, FAILED = "접수", "트리아지 중", "검토 대기", "등록 완료", "폐기", "실패"
STATES = (RECEIVED, TRIAGING, REVIEW, REGISTERED, DISCARDED, FAILED)
PENDING = (RECEIVED, TRIAGING)   # 작업자가 처리 중인 상태

TRANSITIONS = {
    RECEIVED: {TRIAGING, FAILED},
    TRIAGING: {REVIEW, FAILED},
    REVIEW: {REGISTERED, DISCARDED, FAILED},
    FAILED: {RECEIVED, REVIEW},
    REGISTERED: set(),
    DISCARDED: set(),
}


class FlowError(Exception):
    """허용되지 않은 상태 전이."""


def check(src: str, dst: str) -> None:
    if dst not in TRANSITIONS.get(src, set()):
        raise FlowError(f"'{src}' 에서 '{dst}' 로 바꿀 수 없습니다")
