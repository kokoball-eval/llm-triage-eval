"""서비스 영역 사이에 주고받는 타입과 출처·BTS 계약.

[설계 의도]
1. 트리아지·흐름·화면은 이 파일의 타입만 다루고 출처(게시판·디스코드·Jira 등)나 BTS(Jira·Redmine)의 종류를 모른다.
   출처·BTS의 차이는 어댑터(intake/·bts/) 안에서 끝난다. 새 출처·BTS는 Source·Tracker 를 구현한 파일 하나로 붙는다.
2. Report.to_item() 은 평가셋 문항과 같은 모양({"track", "input"})을 만든다. 측정한 프롬프트는 이 모양으로 입력을
   읽으므로, 모양이 달라지면 측정한 성능이 서비스에는 들어맞지 않는다. 시험(tests/test_service.py 의 test_service_input_matches_measured_prompt_input)이 평가셋 문항 전부로
   "문항 → Report → 문항"을 돌려 모델 입력 문자열이 같은지 고정한다.
3. 입력 종류(track)는 측정 때와 같은 두 가지다. A는 커뮤니티 원문(게시판·게시 일시), B는 QA가 쓴 BTS 이슈(작성자).
   B 의 작성자는 REPORTERS 중 하나여야 한다(안전장치 규칙이 작성자 값을 본다).
"""

from dataclasses import dataclass
from typing import Protocol

TRACKS = {"A": "커뮤니티 제보", "B": "QA 이슈"}
# B 의 작성자는 측정한 평가셋에 쓰인 값만 받는다. 안전장치 H-2b 가 작성자(내부·퍼블리셔 QA)를 보고 처리를 고치므로,
# 다른 표기("QA팀" 등)가 들어오면 측정 때와 다른 규칙이 적용된다.
REPORTERS = ("내부 QA", "퍼블리셔 QA", "운영/CS")


@dataclass(frozen=True)
class Report:
    """출처에서 가져온 리포트 한 건."""
    track: str
    title: str
    body: str
    board: str = ""            # track A — 게시판 이름
    posted_at: str = ""        # track A — 게시 일시
    reporter: str = ""         # track B — 작성자(소속·역할)
    update_note: str = ""      # 참고: 최근 업데이트 공지
    known_issues: tuple[str, ...] = ()   # 참고: 기존 등록 이슈 제목
    source: str = "paste"      # 출처 어댑터 이름
    url: str = ""              # 원문 링크 (붙여넣기는 없음)

    def to_item(self) -> dict:
        """평가셋 문항과 같은 모양 (설계 의도 2)."""
        head = {"board": self.board, "posted_at": self.posted_at} if self.track == "A" else {"reporter": self.reporter}
        context = {"update_note": self.update_note, "known_issues": list(self.known_issues)}
        return {"track": self.track, "input": {**head, "title": self.title, "body": self.body, "context": context}}


@dataclass(frozen=True)
class Issue:
    """사람이 확인한 8칸 — BTS 에 등록할 내용. ref 는 서비스 표식(같은 리포트의 티켓을 다시 찾는 데 쓴다)."""
    report: Report
    fields: dict[str, str]
    ref: str = ""


@dataclass(frozen=True)
class Ticket:
    key: str
    url: str


class Source(Protocol):
    """출처 어댑터 계약 — 링크를 받아 Report 를 만든다 (S3 에서 게시판·디스코드 등을 붙인다)."""
    name: str

    def matches(self, url: str) -> bool: ...

    def fetch(self, url: str) -> Report: ...


class Tracker(Protocol):
    """BTS 어댑터 계약 — 등록하고 티켓을 돌려준다. 실패는 TrackerError 로 알린다(대체 티켓을 만들지 않는다)."""
    name: str

    def find(self, ref: str) -> Ticket | None: ...

    def create(self, issue: Issue) -> Ticket: ...

    def status(self, key: str) -> str: ...


class TrackerError(Exception):
    """BTS 가 등록·조회를 거절했을 때 어댑터가 올리는 예외."""
