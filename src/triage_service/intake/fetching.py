"""링크 가져오기 공통 — 허용한 곳만, 정해 둔 크기·시간 안에서만 받는다.

[설계 의도]
1. 임의 주소 요청(SSRF)을 막는다. 출처 어댑터는 설정 파일에 적힌 호스트의 링크만 맡고(matches), 받는 쪽에서도
   리다이렉트를 따라가지 않는다. 허용한 게시판이 사내 다른 주소나 외부로 넘기는 응답을 돌려줘도 그 주소를 부르지 않는다.
2. 응답 크기에 상한을 둔다. 끝없이 큰 응답(실수든 공격이든)을 메모리로 다 받지 않고, 상한을 넘으면 그 자리에서 멈춘다.
   시간 제한은 읽기 한 번마다 걸린다(전체 시간 상한은 없다). 바이트를 아주 느리게 흘리는 서버는 요청 하나를 오래 잡을 수
   있다. 허용한 곳만 부르는 로컬 도구라 이 한계는 기록만 한다.
3. 실패는 IntakeError 하나로 알린다. 화면은 이유를 보여 주고 붙여넣기로 넘어가게 하며, 대신 채운 내용을 만들지 않는다.
   토큰은 머리글에만 있고 오류 메시지에는 넣지 않는다. 잘못된 링크(탭·줄바꿈 등)나 JSON 이 아닌 응답도 같은 오류로 바꾼다.
4. 링크 모양을 하나로 맞춘다(canonical). 같은 이슈를 가리키는 링크가 끝의 /, 쿼리, 대문자 스킴, 디스코드 하위 도메인만
   달라도 같은 원문으로 본다. 원문 링크로 중복 접수를 막기 때문이다(store 설계 의도 7).
5. 시각은 이 PC 의 현지 시각으로 맞춘다. 디스코드는 UTC, 게시판은 +09:00 처럼 시간대가 섞여 오는데, 측정한 입력의
   [게시 일시]는 현지 시각이었다. 시간대를 읽을 수 없는 값("3시간 전" 등)은 버린다.
"""

import json
import re
from datetime import datetime
from urllib.parse import urlsplit

import httpx2

CONTROL = re.compile(r"[\x00-\x20\x7f]")


class IntakeError(Exception):
    """링크에서 리포트를 가져오지 못했다. 메시지를 화면에 그대로 보여 준다."""


def clean_link(url: str) -> str:
    """앞뒤 공백을 걷고, 안에 공백·제어 문자가 있으면 거절한다 (설계 의도 3)."""
    url = (url or "").strip()
    if CONTROL.search(url):
        raise IntakeError("링크에 공백이나 줄바꿈이 들어 있습니다")
    if urlsplit(url).scheme.lower() not in ("http", "https"):
        raise IntakeError("http:// 또는 https:// 로 시작하는 링크를 넣어 주세요")
    return url


def get(http: httpx2.Client, url: str, max_bytes: int) -> bytes:
    """GET 으로 본문을 받는다. 리다이렉트·오류 상태·크기 초과는 IntakeError (설계 의도 1·2·3)."""
    try:
        with http.stream("GET", url, follow_redirects=False) as resp:
            if 300 <= resp.status_code < 400:
                raise IntakeError(f"다른 주소로 넘기는 응답({resp.status_code})은 따라가지 않습니다")
            if resp.status_code >= 400:
                raise IntakeError(f"가져오기 실패: 상태 {resp.status_code}")
            body = bytearray()
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    raise IntakeError(f"응답이 너무 큽니다({max_bytes:,}바이트 초과)")
            return bytes(body)
    except httpx2.TimeoutException:
        raise IntakeError("응답이 없습니다(시간 초과)")
    except httpx2.InvalidURL:
        raise IntakeError("링크 형식이 올바르지 않습니다")
    except httpx2.HTTPError as e:
        raise IntakeError(f"연결할 수 없습니다: {type(e).__name__}")


def get_json(http: httpx2.Client, url: str, max_bytes: int) -> dict:
    try:
        data = json.loads(get(http, url, max_bytes))
    except ValueError:
        raise IntakeError("응답 형식이 JSON 이 아닙니다")
    if not isinstance(data, dict):
        raise IntakeError("응답 형식이 예상과 다릅니다")
    return data


def local_time(value: str) -> str:
    """ISO 8601 시각 → 현지 'YYYY-MM-DD HH:MM'. 시간대가 없거나 읽을 수 없으면 빈 문자열 (설계 의도 5)."""
    try:
        t = datetime.fromisoformat((value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        return ""
    return t.astimezone().strftime("%Y-%m-%d %H:%M")
