"""작업 페이지 실행 명령 — uv run triage-serve [--check].

[설계 의도]
1. 설정(BTS·출처)이 잘못되면 서버를 시작하지 않고 문제를 출력한다(bts/settings.py, intake/settings.py). 토큰은 .env 를
   먼저 읽은 뒤 검사한다.
2. --check 는 서버를 띄우지 않고 BTS 연결(설정·토큰·검색 권한)과 켜진·꺼진 출처만 확인한다. 실제 BTS 에 연결하기 전에
   설정이 맞는지 먼저 보기 위해서다.
3. 자동 배정을 쓰면 --check 가 팀마다 설정한 계정이 BTS 에서 담당자가 될 수 있는지도 확인한다. 계정이 틀리면 등록할
   때마다 BTS 가 거절하기 때문이다.
4. --check 는 BTS 워크플로에 다섯 상태(contracts.STATUSES)가 있는지도 본다. 없으면 첫 상태·QA 동작이 BTS 에서 거절된다.
"""

import sys
from collections import Counter

import uvicorn

from triage_service import assign, config, intake
from triage_service.contracts import STATUSES
from triage_service.bts import make_tracker, settings
from triage_service.intake import settings as intake_settings
from triage_service.store import Store
from triage_service.web.app import create_app


def main(argv: list[str] | None = None) -> int:
    """uv run triage-serve [--check]. --check 는 서버를 띄우지 않고 BTS 연결(설정·토큰·검색 권한)만 확인한다."""
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
        if loaded["assign"]:   # 내 담당 화면은 담당자를 표시 이름으로 비교한다(sync 설계 의도 13)
            names = Counter(m.name for m in tracker.members(loaded["assign"]["qa"]))
            if same := sorted(n for n, c in names.items() if c > 1):
                print(f"경고: QA팀 그룹에 표시 이름이 같은 구성원이 있습니다: {', '.join(same)} — 내 담당 화면이 서로 구분하지 못하니 이름을 다르게 하세요")
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
    print(f"작업 페이지: http://{config.HOST}:{config.PORT}  · BTS: {loaded['use']} ({config.mapping_path().name})  (종료: Ctrl+C)")
    app = create_app(store, tracker=make_tracker(loaded, store), sources=intake.make_sources(loaded_sources),
                     sources_off=loaded_sources["off"], assign_rules=loaded["assign"])
    uvicorn.run(app, host=config.HOST, port=config.PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
