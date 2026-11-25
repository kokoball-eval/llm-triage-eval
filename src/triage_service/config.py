"""서비스 설정 — 서비스가 쓰는 값은 이 파일 한 곳에서만 정한다. 모델·생성 옵션은 평가 패키지 설정(triage_eval.config)을 따른다.

[설계 의도]
1. 모델과 생성 옵션을 서비스에서 따로 정하지 않는다. 측정한 구성과 다른 값으로 돌면 측정한 성능이 서비스에 들어맞지 않는다.
2. 접속 주소는 127.0.0.1 이다. 이 PC 밖에서 접속하게 하려면 인증·토큰 관리가 먼저 있어야 한다(S2 이후 검토).
3. DB 파일은 저장소 데이터 폴더 아래에 두되 git 에는 올리지 않는다(.gitignore). 리포트 원문에 유저 정보가 들어갈 수 있다.
4. 입력 길이 상한은 측정한 범위에 가깝게 둔다. 측정한 입력은 제목 57자·본문 438자 이하였다. 훨씬 긴 입력은 성능을 잰 적이
   없고, 체크리스트·재요청 프롬프트까지 붙으면 num_ctx 를 넘어 앞부분이 조용히 잘릴 수 있다. 본문 1,000자는 측정 최대의
   약 2배로, 이보다 긴 리포트는 사람이 요점을 줄여 넣는다.
5. 회사마다 다른 BTS 값(주소·프로젝트·우선순위 대응)은 코드가 아니라 설정 파일에 둔다. 저장소에는 예시 값이 든
   설정/bts_매핑.toml 만 올리고, 실제 사이트 주소 등은 설정/bts_매핑.local.toml(git 제외)에 둔다. 읽는 순서는
   환경 변수 TRIAGE_BTS_MAPPING 의 경로 → .local 파일(있으면) → 저장소 파일이다.
6. BTS 토큰은 환경 변수로만 받는다. 저장소 루트의 .env 파일(git 제외)에 적어 두면 시작할 때 읽되, 이미 설정된 환경
   변수를 덮어쓰지 않는다. 토큰을 설정 파일이나 코드에 적지 않기 위해서다.
7. 받는 Host 는 127.0.0.1·localhost 뿐이다. 외부 도메인이 이 PC 를 가리키게 만들어(DNS 리바인딩) 브라우저로 로컬
   서버를 부르는 공격을 막는다.
8. BTS 동기화(sync.py)는 등록한 리포트 중 최근 SYNC_LIMIT 건만 읽고, 한 번에 SYNC_CHUNK 건씩 묶어 요청한다. 한 건씩 부르면
   요청이 건수만큼 늘고, 오래된 건까지 읽으면 BTS 호출 한도와 대기 시간이 커진다(shortcut: 200건이 모자라면 올린다).
"""

import os
from pathlib import Path

from triage_eval.config import DATA, ROOT

DB_PATH = DATA / "서비스" / "service.db"
HOST = "127.0.0.1"
PORT = 8000
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
MAPPING_PATH = ROOT / "설정" / "bts_매핑.toml"              # 저장소에 올리는 예시 설정
LOCAL_MAPPING_PATH = ROOT / "설정" / "bts_매핑.local.toml"  # 실제 사이트 설정 (git 제외)
SOURCES_PATH = ROOT / "설정" / "출처.toml"                  # 링크 출처 예시 설정
LOCAL_SOURCES_PATH = ROOT / "설정" / "출처.local.toml"      # 실제 출처 설정 (git 제외)
ENV_PATH = ROOT / ".env"
# 붙여넣기 입력 길이 상한 (설계 의도 4). 측정한 평가셋에서 가장 긴 제목은 57자, 본문은 438자다.
TITLE_MAX = 100
BODY_MAX = 1000
STATUS_REFRESH_SEC = 1    # 트리아지가 끝날 때까지 상세 화면을 다시 읽는 간격
AUTO_SYNC_MINUTES = 10    # 화면을 열 때 마지막 동기화가 이보다 오래됐으면 뒤에서 다시 읽는다(분, None·0 이하면 끈다. sync 설계 의도 10)
BOTTLENECK_DAYS = {"QA Review": 2, "Resolve": 2, "In Dev": 7, "Check": 14}   # 상태별로 이만큼(일) 넘게 머물면 병목으로 본다 (sync 설계 의도 13)
SPRINT_WEEKDAY = 3        # 스프린트 = 매주 이 요일의 정기 업데이트 (월=0 … 목=3, sprint 설계 의도 1)
SPRINT_WEEKS = {"Critical": 0, "Major": 1, "Minor": 2, "Trivial": 3}   # 우선순위별 기본 목표: 기준 업데이트에서 몇 주 뒤 (sprint 설계 의도 3)
CARRY_RECHECK_MINUTES = 10   # 이월한 티켓은 이 시간 안에는 다시 옮기지 않는다 — Jira 검색 색인이 늦어 옛 스프린트가 읽혀도 두 번 옮기지 않게 (sync 설계 의도 15)
SYNC_LIMIT = 200          # BTS 에서 상태를 읽는 등록 리포트 수의 상한 (설계 의도 8)
MILESTONE_LIMIT = 500     # 동기화에서 마일스톤 하나당 읽는 BTS 티켓 수의 상한 (sync 설계 의도 8)
SYNC_CHUNK = 50           # 한 번의 BTS 요청에 담는 티켓 수 (50 이하: Jira 검색의 reconcileIssues 가 ID 50개까지만 받는다. Redmine limit 100)


def load_env(path: Path = ENV_PATH) -> None:
    """.env 의 KEY=VALUE 줄을 환경 변수로 읽는다. 이미 있는 값은 그대로 둔다 (설계 의도 6)."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def mapping_path() -> Path:
    """설계 의도 5 — .env 를 읽은 뒤에 부른다(.env 에 TRIAGE_BTS_MAPPING 을 둘 수 있게)."""
    if os.environ.get("TRIAGE_BTS_MAPPING"):
        return Path(os.environ["TRIAGE_BTS_MAPPING"])
    return LOCAL_MAPPING_PATH if LOCAL_MAPPING_PATH.is_file() else MAPPING_PATH


def sources_path() -> Path:
    """링크 출처 설정 — .local 파일이 있으면 그것을, 없으면 저장소 파일을 읽는다 (설계 의도 5와 같은 방식)."""
    return LOCAL_SOURCES_PATH if LOCAL_SOURCES_PATH.is_file() else SOURCES_PATH
