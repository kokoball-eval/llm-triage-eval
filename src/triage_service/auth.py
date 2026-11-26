"""로그인 — 사람별 계정(설정/사용자.local.toml) 또는 망 접속 시연용 공용 비밀번호.

[설계 의도]
1. 기본은 이 PC(127.0.0.1)만 받고 로그인이 없다. 망 접속은 --lan 으로 켰을 때만 열리고, 그때는 로그인하지 않은 요청을
   받지 않는다(2026-11-26 경민 결정: 시연용, 실제 BTS 도 허용). 리포트에 유저 정보가 들어 있고 BTS 에 쓰는 버튼이 있어서다.
2. 비밀번호 비교는 시간 차로 내용이 새지 않게 hmac.compare_digest 로 한다. 같은 기기에서 1분에 config.LOGIN_MAX_FAILS 번 넘게
   틀리면 잠시 막는다 — 열린 망에서 비밀번호를 하나씩 맞혀 보는 것을 늦춘다.
3. 로그인 쿠키는 "사용자.발급 시각.난수.서명"이다. 서명 키는 서버를 켤 때마다 새로 만들어(표준 라이브러리) 서버를 끄면 모두
   로그아웃된다. config.LOGIN_HOURS 가 지난 쿠키, 사용자 파일에서 지워진 사람의 쿠키는 받지 않는다. shortcut: 로그아웃은 그
   브라우저의 쿠키를 지울 뿐 이미 복사된 쿠키를 서버에서 무효로 만들지 않는다.
4. 통신은 암호화하지 않는다(HTTP). 실제 업무 데이터로 쓰려면 HTTPS 가 먼저다.
5. 사람별 계정(2026-11-26 경민 결정, 역할 2단계의 1단계): 설정/사용자.local.toml(git 제외)에 사람마다 로그인 이름·표시 이름·
   역할(leader 팀장·파트장 / member 실무자)·BTS 계정 ID·비밀번호 해시를 적는다. 사람 수가 적어(5~10명) 관리 화면 대신 파일이다.
   이 파일이 있으면 이 PC 에서도 로그인해야 한다 — 내 업무의 "나"가 로그인한 사람이기 때문이다. 없으면 예전처럼 로그인이 없거나
   (--lan 이면) 공용 시연 비밀번호 하나다. 비밀번호는 평문으로 두지 않고 hashlib.scrypt 해시(새 의존성 없음)로 적는다
   (uv run triage-user hash). 없는 이름으로 로그인해도 같은 시간이 걸리게 가짜 해시와 비교한다.
6. 역할(2026-11-26 경민 결정, 2단계): 리더(팀장·파트장)와 실무자. 리더만 대시보드·트리아지 품질을 보고, 팀원의 내 업무를 고르고,
   긴급 사인(팀장 부재 시 파트장 대행)과 담당 바꾸기를 한다. 실무자는 서로 돕고 대신 맡을 수 있어야 해서 QA 동작은 모든 이슈에
   할 수 있다. 권한 표(LEADER_ONLY)는 여기 한 곳이고 메뉴와 서버가 같은 표를 쓴다 — 메뉴만 숨기면 주소를 직접 쳐서 열 수 있기
   때문이다. 역할은 사람별 로그인일 때만 있다.
"""

import getpass
import hashlib
import hmac
import re
import secrets
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from triage_service import config

COOKIE = "triage_login"
ROLES = {"leader": "리더", "member": "실무자"}
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
# 리더(팀장·파트장)만 할 수 있는 것 (설계 의도 6) — 화면 주소와 동작 이름
LEADER_ONLY = {"/dashboard": "대시보드", "/triage": "트리아지 품질", "team": "팀원 업무 보기", "sign": "긴급 사인", "reassign": "담당 바꾸기"}


@dataclass(frozen=True)
class User:
    id: str
    name: str
    role: str = "member"
    bts: str = ""   # BTS 구성원 ID (Jira accountId, Redmine 사용자 번호) — 내 업무의 "나"


SHARED = User("", "시연")   # 공용 시연 비밀번호로 들어온 사람(누구인지 모른다)


def can(user: "User | None", what: str) -> bool:
    """설계 의도 6 — 이 사람이 what(LEADER_ONLY 의 주소·동작)을 할 수 있는가. 사람별 로그인이 아니면(로그인 없음, 공용 시연
    비밀번호) 역할이 없으므로 예전처럼 모두 할 수 있다. 긴급 사인은 역할이 없을 때 qa.py 가 QA팀장인지 따로 본다."""
    return user is None or not user.id or user.role == "leader" or what not in LEADER_ONLY


class UsersError(Exception):
    """사용자 파일 문제. 메시지를 그대로 보여 주고 서버를 시작하지 않는다."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def verify(password: str, stored: str) -> bool:
    try:
        _, salt, want = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT).hex()
    except ValueError:
        return False
    return hmac.compare_digest(got, want)


_DUMMY = hash_password(secrets.token_hex(8))   # 설계 의도 5 — 없는 이름도 같은 시간이 걸리게


def load_users(path: Path) -> tuple[dict[str, User], dict[str, str]]:
    """설계 의도 5 — ({로그인 이름: User}, {로그인 이름: 비밀번호 해시}). 문제가 있으면 UsersError(문제 목록)."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise UsersError(f"사용자 파일 문법 오류 ({path}): {e}")
    problems, users, hashes = [], {}, {}
    if set(data) - {"user"} or not isinstance(data.get("user"), list):
        raise UsersError(f"사용자 파일 형식 오류 ({path}): [[user]] 표만 둡니다")
    for i, u in enumerate(data["user"], 1):
        if not isinstance(u, dict) or set(u) != {"id", "name", "role", "bts", "password"} or not all(isinstance(v, str) for v in u.values()):
            problems.append(f"{i}번째 사람: id·name·role·bts·password 다섯 칸을 글자로 적어야 합니다")
            continue
        if not re.fullmatch(r"[a-z0-9_-]{2,32}", u["id"]):
            problems.append(f"{u['id']!r}: 로그인 이름은 영문 소문자·숫자·_·- 2~32자")
        elif u["id"] in users:
            problems.append(f"{u['id']}: 로그인 이름이 겹칩니다")
        if u["role"] not in ROLES:
            problems.append(f"{u['id']}: role 은 {' · '.join(ROLES)} 중 하나")
        if not re.fullmatch(r"scrypt\$[0-9a-f]{32}\$[0-9a-f]{128}", u["password"]):
            problems.append(f"{u['id']}: password 는 uv run triage-user hash 로 만든 해시를 적습니다(평문 금지)")
        if not u["name"].strip() or not u["bts"].strip():
            problems.append(f"{u['id']}: name 과 bts(BTS 계정 ID)를 비울 수 없습니다")
        elif any(ch < " " for ch in u["name"] + u["bts"]):   # 이름은 BTS 코멘트에 그대로 들어간다 — 줄바꿈 등으로 코멘트를 꾸미지 못하게
            problems.append(f"{u['id']}: name·bts 에 줄바꿈 같은 제어 문자를 넣을 수 없습니다")
        users[u["id"]], hashes[u["id"]] = User(u["id"], u["name"].strip(), u["role"], u["bts"].strip()), u["password"]
    if users and not any(u.role == "leader" for u in users.values()):
        problems.append("리더(role = \"leader\")가 한 명 이상 있어야 합니다")
    if problems:
        raise UsersError(f"사용자 파일 문제 ({path}):\n- " + "\n- ".join(problems))
    return users, hashes


class Login:
    """users 가 있으면 사람별 로그인, 없으면 공용 비밀번호(password) 하나."""

    def __init__(self, password: str = "", users: dict[str, User] | None = None, hashes: dict[str, str] | None = None):
        self.password, self.users, self.hashes = password.encode(), users or {}, hashes or {}
        self.key, self.fails = secrets.token_bytes(32), {}

    def check(self, password: str, who: str, user_id: str = "", now: float | None = None) -> User | bool | None:
        """설계 의도 2·5 — 맞으면 User, 틀리면 False, 너무 많이 틀려 잠시 막혔으면 None. who 는 접속한 기기 주소."""
        now = time.monotonic() if now is None else now
        recent = [t for t in self.fails.get(who, []) if now - t < 60]
        if len(recent) >= config.LOGIN_MAX_FAILS:
            self.fails[who] = recent
            return None
        if self.users:
            ok = verify(password, self.hashes.get(user_id, _DUMMY)) and user_id in self.users
            user = self.users.get(user_id)
        else:
            ok, user = hmac.compare_digest(password.encode(), self.password), SHARED
        if ok:
            self.fails.pop(who, None)
            return user
        self.fails[who] = [*recent, now]
        return False

    def _sign(self, body: str) -> str:
        return hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()

    def token(self, user: User = SHARED, now: float | None = None) -> str:
        """설계 의도 3 — 로그인 쿠키 값."""
        body = f"{user.id}.{int(time.time() if now is None else now)}.{secrets.token_hex(16)}"
        return f"{body}.{self._sign(body)}"

    def valid(self, token: str | None, now: float | None = None) -> User | None:
        """설계 의도 3 — 쿠키가 맞으면 그 사람, 아니면 None."""
        body, _, sig = (token or "").rpartition(".")
        user_id, _, rest = body.partition(".")
        issued = rest.split(".", 1)[0]
        if not (body and issued.isdigit() and hmac.compare_digest(sig.encode(), self._sign(body).encode())):   # 바이트로 비교 — 이상한 문자가 든 쿠키도 오류 없이 거절
            return None
        if (time.time() if now is None else now) - int(issued) >= config.LOGIN_HOURS * 3600:
            return None
        return self.users.get(user_id) if self.users else SHARED


def main(argv: list[str] | None = None) -> int:
    """uv run triage-user hash — 비밀번호를 물어 해시를 출력한다(사용자 파일의 password 칸에 붙여 넣는다)."""
    argv = sys.argv[1:] if argv is None else argv
    sys.stdout.reconfigure(encoding="utf-8")
    if argv != ["hash"]:
        print("사용법: uv run triage-user hash")
        return 2
    first = getpass.getpass("비밀번호: ")
    if len(first) < config.PASSWORD_MIN:
        print(f"{config.PASSWORD_MIN}자 이상이어야 합니다")
        return 2
    if getpass.getpass("한 번 더: ") != first:
        print("두 번 넣은 비밀번호가 다릅니다")
        return 2
    print(hash_password(first))
    return 0
