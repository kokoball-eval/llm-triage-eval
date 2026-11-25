"""스프린트 — 매주 목요일 정기 업데이트 하나가 스프린트이고, 이슈마다 "어느 업데이트에서 고칠지"(목표 수정 버전)를 단다.

[설계 의도]
1. 마일스톤(월간, BTS 버전)과 따로 주간 단위를 둔다(2026-11-25 경민 결정). 스프린트는 기간이 아니라 이슈에 붙는 목표다 —
   11/25(수)에 확인한 이슈는 11/26(목) 업데이트에서 고치거나 그 뒤 업데이트로 넘긴다. 이름은 업데이트 날짜(YYYY-MM-DD)다.
   요일은 config.SPRINT_WEEKDAY 한 곳에서 정한다.
2. 기준 업데이트는 발견한 날 다음에 오는 첫 업데이트다. 업데이트 날 발견한 이슈는 그날 배포에 넣을 수 없으니 다음 주다.
3. 등록할 때의 기본 목표는 우선순위로 정한다(경민 결정): 기준 업데이트에서 config.SPRINT_WEEKS[우선순위] 주 뒤. 긴급 사인
   요청은 우선순위와 상관없이 기준 업데이트다. 사람은 검토 화면에서 바꿀 수 있다 — 사람이 병목이라 기본값으로 돌게 한다.
   지난 업데이트는 목표가 될 수 없다: 기본값은 진행 중인 업데이트보다 이르지 않고, 등록할 때도 거절한다(review.py, 독립 검토 반영).
   shortcut: 기본값은 검토 화면을 그릴 때의 우선순위로 정한다. 화면에서 우선순위를 바꾸면 스프린트도 사람이 맞춘다.
4. BTS 에는 표식으로 남긴다(Jira "스프린트_<날짜>" 라벨, Redmine 사용자 정의 필드, 경민 결정). 개발자도 BTS 에서 보고 바꾼다.
"""

from datetime import date, timedelta

from triage_service import config


def base(found: date) -> date:
    """설계 의도 2 — found 다음에 오는 첫 업데이트 날."""
    return found + timedelta(days=(config.SPRINT_WEEKDAY - found.weekday() - 1) % 7 + 1)


def default(found: date, priority: str, urgent: bool = False, today: date | None = None) -> str:
    """설계 의도 3 — 등록할 때의 목표 스프린트 이름. 오래전에 받은 리포트를 늦게 검토해도 이미 지난 업데이트는 고르지 않는다."""
    weeks = 0 if urgent else config.SPRINT_WEEKS.get(priority, max(config.SPRINT_WEEKS.values()))
    return max(base(found) + timedelta(weeks=weeks), current(today or date.today())).isoformat()


def current(today: date) -> date:
    """지금 진행 중인 스프린트 — 오늘이 업데이트 날이면 오늘, 아니면 다음 업데이트."""
    return base(today - timedelta(days=1))


def window(today: date, before: int = 0, after: int = 5) -> list[str]:
    """current 를 가운데 두고 앞뒤 스프린트 이름 (선택지)."""
    now = current(today)
    return [(now + timedelta(weeks=k)).isoformat() for k in range(-before, after + 1)]


def valid(name: str) -> bool:
    """업데이트 요일의 날짜인지 — 화면 입력(신뢰 경계)과 BTS 표식을 읽을 때 쓴다."""
    try:
        return date.fromisoformat(name).weekday() == config.SPRINT_WEEKDAY
    except ValueError:
        return False


def label(name: str) -> str:
    """화면 표시 — "11/26(목) 업데이트"."""
    d = date.fromisoformat(name)
    return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]}) 업데이트"
