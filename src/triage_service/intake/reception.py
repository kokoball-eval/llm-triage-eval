"""접수 흐름 — 링크 미리보기와 접수 확인. 화면(web/app.py)은 이 결과를 보여 주기만 한다.

[설계 의도]
1. 링크로 가져온 리포트는 바로 접수하지 않는다. preview() 는 가져온 제목·본문을 입력 칸에 채울 값으로 돌려주고, 사람이
   확인·수정한 뒤 [트리아지]를 눌러야 accept() 가 접수할 Report 를 만든다. 게시판 구조가 바뀌어 엉뚱한 글을 가져오는
   경우를 사람이 걸러 내기 위해서다. 길이 상한은 미리보기에서는 안내만 하고, 접수할 때 붙여넣기와 같은 검사로 막는다.
2. 같은 원문 링크는 한 번만 접수한다(store 설계 의도 7). 가져오기 전과 접수할 때 두 번, 하나로 맞춘 링크로 확인한다.
   폐기·실패한 리포트의 링크도 다시 접수하지 않는다. 같은 원문을 두 번 판단하지 않고 기존 리포트에서 다시 시도한다.
3. 접수할 때 숨은 칸의 출처·링크가 그 링크를 맡는 실제 출처·맞춘 링크와 같은지 확인한다. 손으로 만든 요청으로 다른 링크를
   "이미 접수"로 만들거나 중복 확인을 피하는 일을 막는다.
"""

from triage_service.config import BODY_MAX, TITLE_MAX
from triage_service.contracts import Report
from triage_service.intake import identify
from triage_service.intake.paste import InputError, from_form
from triage_service.store import Store


class Duplicate(Exception):
    """이미 접수한 원문. existing 은 그 리포트 기록이다."""

    def __init__(self, existing: dict):
        super().__init__("이미 접수한 링크입니다")
        self.existing = existing


def preview(sources: list, store: Store, link: str) -> tuple[dict, str]:
    """(입력 칸에 채울 값, 안내 문구). 실패는 IntakeError, 중복은 Duplicate (설계 의도 1·2). 네트워크를 쓴다."""
    source, canonical = identify(sources, link)
    existing = store.find_by_url(canonical)
    if existing:
        raise Duplicate(existing)
    report = source.fetch(canonical)
    prefill = {"track": report.track, "title": report.title, "body": report.body, "board": report.board,
               "posted_at": report.posted_at, "reporter": report.reporter, "source": report.source,
               "url": report.url, "link": link}
    notice = f"{source.name} 에서 가져왔습니다. 내용을 확인하고 고친 뒤 [트리아지]를 누르세요."
    if len(report.title) > TITLE_MAX or len(report.body) > BODY_MAX:
        notice += f" 제목은 {TITLE_MAX}자, 본문은 {BODY_MAX}자까지 받습니다(측정한 리포트 길이 범위). 요점만 남겨 줄여 주세요."
    return prefill, notice


def accept(sources: list, store: Store, form: dict) -> Report:
    """입력 칸 → 접수할 Report. 입력 문제는 InputError, 출처 문제는 IntakeError, 중복은 Duplicate (설계 의도 2·3)."""
    report = from_form(form)
    if report.url:
        source, canonical = identify(sources, report.url)
        if (source.name, canonical) != (report.source, report.url):
            raise InputError("출처 정보가 맞지 않습니다. 링크로 다시 가져와 주세요")
    existing = store.find_by_url(report.url)
    if existing:
        raise Duplicate(existing)
    return report

