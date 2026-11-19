"""BTS 어댑터가 함께 쓰는 것 — 티켓 본문 만들기와 HTTP 호출·오류 정리.

[설계 의도]
1. 티켓 본문은 어느 BTS 든 같은 내용이다: 확정한 8칸 → 원문 → 서비스 표식. 표식(ref)은 다시 등록할 때 같은 리포트의
   티켓이 이미 있는지 찾는 데 쓴다(review.py 설계 의도 5).
2. HTTP 오류는 TrackerError 하나로 바꾼다. 메시지에는 요청 방법·경로·상태 코드와 BTS 가 돌려준 오류 문구만 담고,
   요청 머리글(토큰)은 담지 않는다.
3. 등록 요청이 응답 없이 끊기면 실제로 등록됐는지 알 수 없다. 이때는 "등록 여부 불확실"로 알리고 자동으로 다시
   보내지 않는다. 다시 보내면 티켓이 두 개 생길 수 있다.
"""

import httpx2

from triage_eval.contracts import FIELDS
from triage_service.contracts import TRACKS, Issue, TrackerError

UNCERTAIN = "BTS 응답 없음 — 등록됐는지 알 수 없습니다. BTS 에서 확인한 뒤 다시 등록하세요(같은 리포트의 티켓이 있으면 새로 만들지 않고 연결합니다)"


def description_lines(issue: Issue) -> list[str]:
    r = issue.report
    where = f"{r.board} · {r.posted_at}" if r.track == "A" else r.reporter
    lines = [f"[{f}]: {issue.fields.get(f, '')}" for f in FIELDS]
    lines += ["", f"── 원문 ({TRACKS.get(r.track, r.track)} · {where} · 출처 {r.source}) ──", f"제목: {r.title}",
              *r.body.split("\n")]
    if r.url:
        lines.append(f"원문 링크: {r.url}")
    lines += ["", marker_line(issue.ref)]
    return lines


def marker_line(ref: str) -> str:
    """티켓 본문의 표식 줄. 찾을 때도 이 줄 전체가 같은지 확인한다(redmine.py 설계 의도 3)."""
    return f"트리아지 서비스 표식: {ref}"


def call(http: httpx2.Client, method: str, path: str, *, creating: bool = False, **kw) -> httpx2.Response:
    """설계 의도 2·3."""
    try:
        resp = http.request(method, path, **kw)
    except httpx2.TimeoutException:
        raise TrackerError(UNCERTAIN if creating else f"BTS 응답 없음 ({method} {path})")
    except httpx2.HTTPError as e:
        raise TrackerError(f"BTS 에 연결할 수 없습니다 ({method} {path}): {type(e).__name__}")
    if resp.status_code >= 400:
        raise TrackerError(f"BTS 가 거절했습니다 ({method} {path} → {resp.status_code}): {brief(resp)}")
    return resp


def brief(resp: httpx2.Response) -> str:
    """BTS 가 돌려준 오류 문구만 짧게 (Jira: errorMessages·errors, Redmine: errors)."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if not isinstance(body, dict):
        return str(body)[:200]
    parts = list(body.get("errorMessages") or [])
    errors = body.get("errors") or []
    parts += [f"{k}: {v}" for k, v in errors.items()] if isinstance(errors, dict) else list(errors)
    return "; ".join(str(p) for p in parts)[:300] if parts else str(body)[:200]
