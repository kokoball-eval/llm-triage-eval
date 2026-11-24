"""서비스 영역 사이에 주고받는 타입과 출처·BTS 계약.

[설계 의도]
1. 트리아지·흐름·화면은 이 파일의 타입만 다루고 출처(게시판·디스코드·Jira 등)나 BTS(Jira·Redmine)의 종류를 모른다.
   출처·BTS의 차이는 어댑터(intake/·bts/) 안에서 끝난다. 새 출처·BTS는 Source·Tracker 를 구현한 파일 하나로 붙는다.
2. Report.to_item() 은 평가셋 문항과 같은 모양({"track", "input"})을 만든다. 측정한 프롬프트는 이 모양으로 입력을
   읽으므로, 모양이 달라지면 측정한 성능이 서비스에는 들어맞지 않는다. 시험(tests/test_service.py 의 test_service_input_matches_measured_prompt_input)이 평가셋 문항 전부로
   "문항 → Report → 문항"을 돌려 모델 입력 문자열이 같은지 고정한다.
3. 입력 종류(track)는 측정 때와 같은 두 가지다. A는 커뮤니티 원문(게시판·게시 일시), B는 QA가 쓴 BTS 이슈(작성자).
   B 의 작성자는 REPORTERS 중 하나여야 한다(안전장치 규칙이 작성자 값을 본다).
4. BTS 티켓 상태는 서비스와 Jira·Redmine 이 같은 다섯 이름(STATUSES)을 쓴다(2026-11-24 경민 결정). 회사마다 이름을
   다르게 두는 대응표는 두지 않고, BTS 에 이 다섯 상태가 있는지만 연결 확인(--check)에서 본다. 어댑터는 읽은 이름을 그대로
   돌려주되, BTS 가 사용자 언어로 번역해 보여 주는 이름은 어댑터가 이 이름으로 되돌린다(jira.py 설계 의도 8).
5. BTS 에 쓰는 것(update)은 상태·담당 팀·코멘트 세 가지다. 무엇을 언제 쓸지(허용 조건, 쓰기 전후 확인)는 qa.py 가 정하고
   어댑터는 받은 대로 보낸다.
6. 마일스톤은 BTS 의 버전이다(Jira 릴리스 fixVersions, Redmine 목표 버전). 두 BTS 에 공통으로 있는 단위가 버전뿐이라서다.
   어댑터는 버전 목록(versions)과 한 버전에 속한 티켓 전체(milestone_tickets)를 읽고, 등록할 때 Issue.milestone 을 넣는다.
   마일스톤 성과는 서비스가 등록한 티켓만이 아니라 그 버전의 BTS 티켓 전체로 센다(BTS 에 직접 등록한 이슈도 QA 업무다).
"""

from dataclasses import dataclass
from typing import Protocol

TRACKS = {"A": "커뮤니티 제보", "B": "QA 이슈"}
# B 의 작성자는 측정한 평가셋에 쓰인 값만 받는다. 안전장치 H-2b 가 작성자(내부·퍼블리셔 QA)를 보고 처리를 고치므로,
# 다른 표기("QA팀" 등)가 들어오면 측정 때와 다른 규칙이 적용된다.
REPORTERS = ("내부 QA", "퍼블리셔 QA", "운영/CS")
# BTS 티켓 상태 (설계 의도 4). QA Review: QA 재현 확인 중 / In Dev: 개발 대응 중 / Resolve: 수정 완료, QA 회귀 테스트 필요 /
# Check: 일정 지연 등으로 대응 보류(사유 코멘트 필수) / Closed: 대응 종료
STATUSES = QA_REVIEW, IN_DEV, RESOLVE, CHECK, CLOSED = ("QA Review", "In Dev", "Resolve", "Check", "Closed")
SOURCE_NAMES = ("paste", "jira", "redmine", "board", "discord")   # Report.source 로 올 수 있는 출처 이름


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
    """사람이 확인한 8칸 — BTS 에 등록할 내용. ref 는 서비스 표식(같은 리포트의 티켓을 다시 찾는 데 쓴다).
    team 은 자동 배정한 팀 이름(assign.py, 빈 문자열이면 배정하지 않음). 팀 → BTS 계정은 어댑터가 설정에서 찾는다."""
    report: Report
    fields: dict[str, str]
    ref: str = ""
    team: str = ""
    milestone: str = ""   # BTS 버전 이름 (설계 의도 6, 빈 문자열이면 넣지 않음)


@dataclass(frozen=True)
class Ticket:
    key: str
    url: str


@dataclass(frozen=True)
class BtsState:
    """BTS 에서 읽은 티켓 상태. updated 는 BTS 가 기록한 마지막 변경 시각(ISO 8601)."""
    status: str
    assignee: str = ""
    updated: str = ""


@dataclass(frozen=True)
class Milestone:
    """BTS 버전 하나 (설계 의도 6). release 는 출시일(YYYY-MM-DD, 없으면 빈 문자열)."""
    name: str
    release: str = ""
    released: bool = False


@dataclass(frozen=True)
class BtsTicket:
    """마일스톤에 속한 BTS 티켓 하나 — 서비스가 등록하지 않은 티켓도 있다."""
    key: str
    title: str
    state: BtsState


class Source(Protocol):
    """출처 어댑터 계약 — 링크를 받아 Report 를 만든다 (S3 에서 게시판·디스코드 등을 붙인다)."""
    name: str

    def matches(self, url: str) -> bool: ...

    def canonical(self, url: str) -> str: ...   # 같은 원문을 가리키는 링크를 하나로 맞춘 모양

    def fetch(self, url: str) -> Report: ...


class Tracker(Protocol):
    """BTS 어댑터 계약 — 등록하고 티켓을 돌려준다. 실패는 TrackerError 로 알린다(대체 티켓을 만들지 않는다)."""
    name: str

    def find(self, ref: str) -> Ticket | None: ...

    def create(self, issue: Issue) -> Ticket: ...

    def status(self, key: str) -> str: ...

    def check_assignees(self) -> list[str]: ...   # 자동 배정 계정이 BTS 에서 담당자가 될 수 없으면 문제 목록

    def snapshot(self, keys: list[str]) -> dict[str, BtsState]: ...   # 티켓 여러 건의 상태(BTS 에 없는 키는 빠진다)

    def update(self, key: str, status: str = "", team: str = "", comment: str = "") -> None: ...   # 빈 값은 바꾸지 않는다 (설계 의도 5)

    def check_statuses(self) -> list[str]: ...   # BTS 에 없는 STATUSES 이름 (연결 확인 --check)

    def versions(self) -> list[Milestone]: ...   # 프로젝트의 버전(마일스톤) 목록 (설계 의도 6)

    def milestone_tickets(self, name: str) -> list[BtsTicket]: ...   # 그 버전의 티켓 전체(config.MILESTONE_LIMIT 까지)


class TrackerError(Exception):
    """BTS 가 등록·조회를 거절했을 때 어댑터가 올리는 예외."""
