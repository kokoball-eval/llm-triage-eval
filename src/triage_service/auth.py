"""망 접속 모드의 로그인 — 같은 와이파이의 휴대폰이 접속할 때 공용 시연 비밀번호 하나로 들어온다(`triage-serve --lan`).

[설계 의도]
1. 기본은 지금처럼 이 PC(127.0.0.1)만 받고 로그인이 없다. 망 접속은 --lan 으로 켰을 때만 열리고, 그때는 로그인하지 않은
   요청을 받지 않는다(2026-11-26 경민 결정: 시연용, 실제 BTS 도 허용). 리포트에 유저 정보가 들어 있고 BTS 에 쓰는 버튼이
   있어서다. 이 PC 자신(127.0.0.1)에서 온 요청은 지금처럼 로그인 없이 받는다.
2. 비밀번호는 .env 의 TRIAGE_DEMO_PASSWORD 하나다(사람마다 다른 계정은 다음 단계 B). 비교는 시간 차로 내용이 새지 않게
   hmac.compare_digest 로 한다. 같은 기기에서 1분에 config.LOGIN_MAX_FAILS 번 넘게 틀리면 잠시 막는다 — 카페처럼 열린 망에서
   비밀번호를 하나씩 맞혀 보는 것을 늦춘다.
3. 로그인 쿠키는 "발급 시각.난수.서명"이다. 서명 키는 서버를 켤 때마다 새로 만들어(표준 라이브러리, 새 의존성 없음) 서버를
   끄면 모두 로그아웃된다. config.LOGIN_HOURS 가 지난 쿠키는 받지 않는다. shortcut: 로그아웃은 그 브라우저의 쿠키를 지울 뿐
   이미 복사된 쿠키를 서버에서 무효로 만들지 않는다(시연 범위). 사람별 계정(B)에서 서버 쪽 세션으로 올린다.
4. 통신은 암호화하지 않는다(HTTP). 시연용 비밀번호와 시연 데이터가 오가는 전제다. 실제 업무 데이터로 쓰려면 HTTPS 가 먼저다.
"""

import hashlib
import hmac
import secrets
import time

from triage_service import config

COOKIE = "triage_login"


class Login:
    def __init__(self, password: str):
        self.password, self.key, self.fails = password.encode(), secrets.token_bytes(32), {}

    def check(self, password: str, who: str, now: float | None = None) -> bool | None:
        """설계 의도 2 — 맞으면 True, 틀리면 False, 너무 많이 틀려 잠시 막혔으면 None. who 는 접속한 기기 주소."""
        now = time.monotonic() if now is None else now
        recent = [t for t in self.fails.get(who, []) if now - t < 60]
        if len(recent) >= config.LOGIN_MAX_FAILS:
            self.fails[who] = recent
            return None
        if hmac.compare_digest(password.encode(), self.password):
            self.fails.pop(who, None)
            return True
        self.fails[who] = [*recent, now]
        return False

    def _sign(self, body: str) -> str:
        return hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()

    def token(self, now: float | None = None) -> str:
        """설계 의도 3 — 로그인 쿠키 값."""
        body = f"{int(time.time() if now is None else now)}.{secrets.token_hex(16)}"
        return f"{body}.{self._sign(body)}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        body, _, sig = (token or "").rpartition(".")
        issued = body.split(".", 1)[0]
        if not (body and issued.isdigit() and hmac.compare_digest(sig.encode(), self._sign(body).encode())):   # 바이트로 비교 — 이상한 문자가 든 쿠키도 오류 없이 거절
            return False
        return (time.time() if now is None else now) - int(issued) < config.LOGIN_HOURS * 3600
