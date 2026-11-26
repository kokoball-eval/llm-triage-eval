"""작업 페이지 실행 명령 — uv run triage-serve [--check].

[설계 의도]
1. 설정(BTS·출처)이 잘못되면 서버를 시작하지 않고 문제를 출력한다(bts/settings.py, intake/settings.py). 토큰은 .env 를
   먼저 읽은 뒤 검사한다.
2. --check 는 서버를 띄우지 않고 BTS 연결(설정·토큰·검색 권한)과 켜진·꺼진 출처만 확인한다. 실제 BTS 에 연결하기 전에
   설정이 맞는지 먼저 보기 위해서다.
3. 자동 배정을 쓰면 --check 가 팀마다 설정한 계정이 BTS 에서 담당자가 될 수 있는지도 확인한다. 계정이 틀리면 등록할
   때마다 BTS 가 거절하기 때문이다.
4. --check 는 BTS 워크플로에 다섯 상태(contracts.STATUSES)가 있는지도 본다. 없으면 첫 상태·QA 동작이 BTS 에서 거절된다.
5. --lan 은 같은 와이파이의 휴대폰이 접속하는 망 접속 모드다(auth.py). 시연 비밀번호(.env 의 TRIAGE_DEMO_PASSWORD, 8자 이상)가
   없으면 시작하지 않는다. 와이파이가 바뀌면 이 PC 의 주소도 바뀌므로 켤 때마다 찾아 받는 주소에 더하고, 휴대폰으로 열 주소와
   QR 코드를 터미널에 보인다. 주소는 psutil 로 네트워크 카드마다 읽고, 카드 이름으로 휴대폰용(Wi-Fi)·PC용(유선)·가상 망을
   짐작해 주소 옆에 적는다(2026-11-26 경민 요청). 노트북이 유선과 와이파이에 함께 붙어 있으면 주소가 여럿이고 서버는 휴대폰이 어느
   망에 있는지 모른다(실제 시연 준비에서 QR 이 유선 주소를 가리켜 휴대폰이 접속하지 못했다). 그래서 하나만 고르지 않고 모두 보여
   주며, 주소가 둘 이상이면 QR 은 /connect 화면에서 주소별로 본다. shortcut: 카드 이름 단어로 짐작하는 것이라 낯선 이름은
   "알 수 없음"으로 나온다 — 그런 이름이 나오면 kind_of 의 단어를 더한다.
"""

import os
import socket
import sys
from collections import Counter

import psutil
import segno
import uvicorn

from triage_service import assign, auth, config, intake
from triage_service.contracts import STATUSES
from triage_service.bts import make_tracker, settings
from triage_service.intake import settings as intake_settings
from triage_service.store import Store
from triage_service.web.app import create_app


def main(argv: list[str] | None = None) -> int:
    """uv run triage-serve [--check | --lan]. --check 는 서버를 띄우지 않고 BTS 연결(설정·토큰·검색 권한)만 확인한다."""
    argv = sys.argv[1:] if argv is None else argv
    sys.stdout.reconfigure(encoding="utf-8")
    config.load_env()
    try:   # 설계 의도 1
        loaded = settings.load(config.mapping_path())
        loaded_sources = intake_settings.load(config.sources_path(), config.mapping_path())
    except settings.SettingsError as e:
        print(e)
        return 2
    store = Store(config.DB_PATH)
    if "--check" in argv:
        try:
            tracker = make_tracker(loaded, store)
            tracker.find("triage-connection-check")
            missing = tracker.check_assignees() if loaded["assign"] else []   # 설계 의도 3
            no_status = tracker.check_statuses()   # 설계 의도 4
            versions = tracker.versions()
        except Exception as e:
            print(f"BTS 연결 실패 ({loaded['use']}): {e}")
            return 1
        print(f"BTS 연결 확인 ({loaded['use']}): 설정·토큰·검색 권한 정상")
        if loaded["assign"]:   # 내 업무 화면은 담당자를 표시 이름으로 비교한다(sync 설계 의도 13)
            names = Counter(m.name for m in tracker.members(loaded["assign"]["qa"]))
            if same := sorted(n for n, c in names.items() if c > 1):
                print(f"경고: QA팀 그룹에 표시 이름이 같은 구성원이 있습니다: {', '.join(same)} — 내 업무 화면이 서로 구분하지 못하니 이름을 다르게 하세요")
        if missing:
            print("자동 배정 계정 문제:\n- " + "\n- ".join(missing))
            return 1
        teams = assign.teams(loaded["assign"])
        print(f"자동 배정: 팀 {', '.join(teams)} — 그룹·구성원·팀장 확인" if teams else "자동 배정: 쓰지 않음([assign] 없음)")
        if no_status:
            print("BTS 상태 문제:\n- " + "\n- ".join(no_status))
            return 1
        print(f"BTS 상태: {' · '.join(STATUSES)} 확인")
        print("마일스톤(BTS 버전): " + (" · ".join(f"{v.name}({v.release or '출시일 없음'}{', 출시함' if v.released else ''})" for v in versions)
                                      or "없음 — BTS 에 버전을 만들면 첫 화면에 마일스톤 성과가 보입니다"))
        on = [s.name for s in intake.make_sources(loaded_sources)]
        print(f"링크 출처: 켜짐 {on or '없음'}" + "".join(f" · {k} 꺼짐({v})" for k, v in loaded_sources["off"].items()))
        return 0
    login, hosts, urls, host = None, config.ALLOWED_HOSTS, [], config.HOST
    if "--lan" in argv:   # 설계 의도 5
        password = os.environ.get("TRIAGE_DEMO_PASSWORD", "")
        if len(password) < config.PASSWORD_MIN:
            print("망 접속 모드에는 .env 의 TRIAGE_DEMO_PASSWORD(시연 비밀번호) 줄이 필요합니다" if not password else
                  f"TRIAGE_DEMO_PASSWORD 가 {len(password)}자입니다 — {config.PASSWORD_MIN}자 이상으로 바꾸세요")
            return 2
        found = lan_addresses()
        if not found:
            print("이 PC 의 와이파이 주소를 찾지 못했습니다 — 와이파이나 핫스팟에 연결했는지 확인하세요")
            return 2
        urls = [(f"http://{ip}:{config.PORT}", kind) for ip, kind in found]
        login, hosts, host = auth.Login(password), (*config.ALLOWED_HOSTS, *(ip for ip, _ in found)), config.LAN_HOST
        print("망 접속 모드 — 휴대폰이 붙은 망의 주소로 여세요 (공용 시연 비밀번호로 로그인, 암호화하지 않는 HTTP):")
        for u, kind in urls:
            print(f"  {u}   [{config.KINDS[kind]}]")
        if len(urls) == 1:
            segno.make(urls[0][0]).terminal(compact=True)
        else:   # 유선·와이파이·가상 망이 함께 있으면 어느 주소가 휴대폰의 망인지 서버는 모른다 — QR 은 화면에서 주소별로 본다
            print(f"망이 여러 개입니다. 노트북 브라우저에서 http://127.0.0.1:{config.PORT}/connect 를 열면 주소별 QR 코드가 있습니다.")
    print(f"작업 페이지: http://{config.HOST}:{config.PORT}  · BTS: {loaded['use']} ({config.mapping_path().name})  (종료: Ctrl+C)")
    app = create_app(store, tracker=make_tracker(loaded, store), sources=intake.make_sources(loaded_sources),
                     sources_off=loaded_sources["off"], assign_rules=loaded["assign"], allowed_hosts=hosts, login=login, lan_urls=urls)
    uvicorn.run(app, host=host, port=config.PORT, proxy_headers=login is None)   # 망 접속 모드에서는 X-Forwarded-For 로 이 PC 인 척하는 것을 막는다
    return 0


ORDER = {kind: i for i, kind in enumerate(config.KINDS)}   # 휴대폰용을 앞에 둔다


def kind_of(card: str) -> str:
    """설계 의도 5 — 네트워크 카드 이름으로 주소의 종류(KINDS 의 키)를 짐작한다. 이름은 윈도우 언어마다 다르다(이더넷·Ethernet)."""
    name = card.lower()
    if any(w in name for w in ("vethernet", "wsl", "docker", "vmware", "virtualbox", "vbox", "hyper-v", "tailscale", "bluetooth")):
        return "virtual"
    if any(w in name for w in ("wi-fi", "wifi", "wlan", "wireless", "무선")):
        return "phone"
    return "pc" if any(w in name for w in ("ethernet", "이더넷", "eth")) else "other"


def lan_addresses() -> list[tuple[str, str]]:
    """설계 의도 5 — 이 PC 의 망 주소(IPv4, 루프백·자동 할당 제외)와 종류. 휴대폰용이 앞이다."""
    found = {}
    for card, addresses in psutil.net_if_addrs().items():
        for a in addresses:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "0.", "169.254.")):
                found.setdefault(a.address, kind_of(card))
    return sorted(found.items(), key=lambda item: ORDER[item[1]])


if __name__ == "__main__":
    raise SystemExit(main())
