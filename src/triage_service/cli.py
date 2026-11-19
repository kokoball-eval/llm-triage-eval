"""작업 페이지 실행 명령 — uv run triage-serve [--check].

[설계 의도]
1. 설정(BTS·출처)이 잘못되면 서버를 시작하지 않고 문제를 출력한다(bts/settings.py, intake/settings.py). 토큰은 .env 를
   먼저 읽은 뒤 검사한다.
2. --check 는 서버를 띄우지 않고 BTS 연결(설정·토큰·검색 권한)과 켜진·꺼진 출처만 확인한다. 실제 BTS 에 연결하기 전에
   설정이 맞는지 먼저 보기 위해서다.
"""

import sys

import uvicorn

from triage_service import config, intake
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
            make_tracker(loaded, store).find("triage-connection-check")
        except Exception as e:
            print(f"BTS 연결 실패 ({loaded['use']}): {e}")
            return 1
        print(f"BTS 연결 확인 ({loaded['use']}): 설정·토큰·검색 권한 정상")
        on = [s.name for s in intake.make_sources(loaded_sources)]
        print(f"링크 출처: 켜짐 {on or '없음'}" + "".join(f" · {k} 꺼짐({v})" for k, v in loaded_sources["off"].items()))
        return 0
    print(f"작업 페이지: http://{config.HOST}:{config.PORT}  · BTS: {loaded['use']} ({config.mapping_path().name})  (종료: Ctrl+C)")
    app = create_app(store, tracker=make_tracker(loaded, store), sources=intake.make_sources(loaded_sources),
                     sources_off=loaded_sources["off"])
    uvicorn.run(app, host=config.HOST, port=config.PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
