"""본문 붙여넣기 출처 — 화면 입력 칸에서 Report 를 만든다.

[설계 의도]
1. 링크로 가져올 수 없는 리포트(로그인이 필요한 곳, 가져오기 실패)도 처리할 수 있게 하는 기본 출처다.
2. 입력은 여기서 한 번 검사한다(입력 종류, 빈 제목·본문, 길이 상한, QA 이슈의 작성자). 길이 상한은 프롬프트가 num_ctx 를 넘어
   앞부분이 잘린 채 트리아지되는 일을 막는다. 잘린 입력은 오류 없이 엉뚱한 판정을 낸다.
3. 본문은 고치지 않는다. 지시문처럼 보이는 문장이 들어 있어도 그대로 입력 데이터로 넘기고, 결과는 형식 규칙과
   안전장치를 통과해야 등록된다(문서/서비스_설계.md §5 안전장치, 프롬프트 주입).
4. 줄바꿈은 LF 로 맞춘다. 브라우저가 보내는 CRLF 가 그대로 들어가면 측정 때 한 번도 없던 문자(CR)가 모델 입력에 섞인다.
5. 링크로 가져온 리포트도 미리보기 화면(같은 입력 칸)을 거쳐 이 함수로 들어온다. 출처 이름과 원문 링크는 숨은 칸으로
   함께 오고, 여기서 형식을 확인한다. 같은 검사(길이 상한 등)를 붙여넣기와 똑같이 거친다.
"""

from datetime import datetime

from triage_service.config import BODY_MAX, TITLE_MAX
from triage_service.contracts import REPORTERS, SOURCE_NAMES, TRACKS, Report


class InputError(ValueError):
    """화면에 그대로 보여 줄 입력 오류."""


def from_form(form: dict) -> Report:
    def text(name: str) -> str:
        value = form.get(name)
        if not isinstance(value, str):   # 파일 등 글자가 아닌 값은 받지 않는다
            return ""
        # 브라우저는 여러 줄 입력을 CRLF 로 보낸다. 측정한 평가셋 입력은 LF 뿐이라 같게 맞춘다 (설계 의도 4)
        return value.replace("\r\n", "\n").replace("\r", "\n").strip()

    track = text("track")
    if track not in TRACKS:
        raise InputError("입력 종류를 고르세요")
    title, body = text("title"), text("body")
    if not title or not body:
        raise InputError("제목과 본문을 모두 넣으세요")
    if len(title) > TITLE_MAX or len(body) > BODY_MAX:
        raise InputError(f"제목은 {TITLE_MAX}자, 본문은 {BODY_MAX}자까지 넣을 수 있습니다")
    known = tuple(k.strip() for k in text("known_issues").splitlines() if k.strip())
    source, url = text("source") or "paste", text("url")
    if source not in SOURCE_NAMES or (source == "paste") != (not url) or \
            (url and not url.lower().startswith(("http://", "https://"))):
        raise InputError("출처 정보가 맞지 않습니다. 링크로 다시 가져오거나 본문을 붙여 넣어 주세요")
    common = {"track": track, "title": title, "body": body, "update_note": text("update_note"), "known_issues": known,
              "source": source, "url": url}
    if track == "A":
        return Report(**common, board=text("board") or "미기재",
                      posted_at=text("posted_at") or datetime.now().strftime("%Y-%m-%d %H:%M"))
    reporter = text("reporter")
    if reporter not in REPORTERS:
        raise InputError(f"QA 이슈는 작성자를 고르세요 ({', '.join(REPORTERS)})")
    return Report(**common, reporter=reporter)
