"""작업 페이지 — 리포트 붙여넣기 → 트리아지 진행 상태 → 8칸 확인·수정 → BTS 등록.

사용법:
    uv run triage-serve          # http://127.0.0.1:8000

[설계 의도]
1. FastAPI 가 HTML 을 만들어 보내는 서버 렌더링이다. 별도 프런트 빌드가 없어 Python 하나로 유지보수한다(문서/서비스_설계.md §3).
2. 트리아지가 끝날 때까지 상세 화면은 브라우저 기본 기능(meta refresh)으로 다시 읽는다. 부분 갱신 라이브러리(HTMX)는
   여러 영역을 따로 갱신해야 하는 대시보드(S4)에서 붙인다. 지금은 화면 하나를 다시 읽는 것으로 충분하다.
3. 템플릿은 자동 이스케이프한다(Jinja2 기본). 리포트 원문은 외부 유저가 쓴 글이라 HTML·스크립트가 들어 있을 수 있다.
4. 화면은 얇게 둔다. 입력 검사는 intake/paste.py, 등록·폐기 규칙은 review.py, 상태 규칙은 flow.py 에 있고
   이 파일은 그 결과를 보여 주기만 한다.
5. create_app() 이 저장소·작업자·BTS 를 인자로 받아, 시험에서 임시 DB·가짜 모델로 같은 화면을 돌린다.
6. 등록은 DB·BTS 를 기다리는 동안 다른 화면 요청을 막지 않도록 스레드에서 실행한다. 허용되지 않은 동작(이미 등록된 건의
   폐기 등)은 오류 JSON 이 아니라 상세 화면에 이유를 보여 준다.
7. 다른 사이트에서 보낸 요청을 막는다. 이 PC 에서 열어 둔 다른 사이트가 브라우저를 통해 몰래 [등록]·[폐기]를 보내면
   실제 BTS 토큰으로 티켓이 생긴다. 상태를 바꾸는 요청(POST)은 Origin(없으면 Referer)이 이 서버와 같을 때만 받는다.
   로그인 쿠키를 쓰지 않으므로 토큰 방식보다 이 확인이 단순하고 충분하다. Host 머리글도 127.0.0.1·localhost 만 받는다
   (외부 도메인이 이 PC 를 가리키게 만드는 DNS 리바인딩 차단, config 설계 의도 7).
8. BTS 설정이 잘못되면 서버를 시작하지 않고 문제를 출력한다(bts/settings.py).
9. 링크로 가져온 리포트는 바로 트리아지하지 않고 미리보기(새 리포트와 같은 입력 칸)에 채워 보여 준다. 사람이 가져온
   제목·본문을 확인하고 고친 뒤 [트리아지]를 눌러야 접수된다. 가져오기에 실패하면 이유를 보여 주고 붙여넣기로 이어 간다.
10. 같은 원문 링크는 한 번만 접수한다(store 설계 의도 7). 가져오기 전과 접수할 때 두 번, 하나로 맞춘 링크로 확인한다.
    접수할 때 숨은 칸의 출처·링크가 그 링크를 맡는 실제 출처·맞춘 링크와 같은지도 확인한다(손으로 만든 요청 차단).
    폐기·실패한 리포트의 링크도 다시 접수하지 않는다. 같은 원문을 두 번 판단하지 않고 기존 리포트에서 다시 시도한다.
11. 대시보드는 숫자 계산을 metrics.py 에 맡기고 보여 주기만 한다. 그래프는 서버가 HTML·CSS 막대로 그려 차트 라이브러리나
    외부 CDN 없이 사내망에서 그대로 동작한다. 기간을 바꾸면 페이지를 다시 읽는다 — 부분 갱신(HTMX)이 필요한 곳이 없어
    넣지 않았다(서비스_설계 §3).
"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from triage_eval.contracts import ENUMS, FIELDS, MODULES, NO_MODULE, split_modules
from triage_service import config, flow, metrics, review
from triage_service.bts import make_tracker, settings
from triage_service.contracts import REPORTERS, TRACKS
from triage_service import intake
from triage_service.intake import settings as intake_settings
from triage_service.intake.paste import InputError, from_form
from triage_service.store import Store
from triage_service.worker import Worker

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(TITLE_MAX=config.TITLE_MAX, BODY_MAX=config.BODY_MAX, split_modules=split_modules, FIELDS=FIELDS, ENUMS=ENUMS, MODULES=MODULES, NO_MODULE=NO_MODULE, TRACKS=TRACKS, REPORTERS=REPORTERS,
                             STATES=flow.STATES, PENDING=flow.PENDING, REVIEW=flow.REVIEW, FAILED=flow.FAILED,
                             REFRESH=config.STATUS_REFRESH_SEC)


def create_app(store: Store | None = None, worker: Worker | None = None, tracker=None,
               start_worker: bool = True, allowed_hosts=config.ALLOWED_HOSTS,
               sources: list | None = None, sources_off: dict | None = None) -> FastAPI:
    store = store or Store(config.DB_PATH)
    worker = worker or Worker(store)
    tracker = tracker or make_tracker(settings.load(config.mapping_path()), store)
    if sources is None:
        loaded = intake_settings.load(config.sources_path(), config.mapping_path())
        sources, sources_off = intake.make_sources(loaded), loaded["off"]
    sources_off = sources_off or {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()
        yield

    app = FastAPI(title="트리아지 작업 페이지", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))

    @app.middleware("http")
    async def same_origin_only(request: Request, call_next):   # 설계 의도 7
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            source = request.headers.get("origin") or request.headers.get("referer")
            if source and urlsplit(source).netloc != request.headers.get("host"):
                return PlainTextResponse("다른 사이트에서 보낸 요청은 받지 않습니다", status_code=403)
        return await call_next(request)

    def page(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def get_or_404(rid: int) -> dict:
        rec = store.get(rid)
        if rec is None:
            raise HTTPException(404, "리포트가 없습니다")
        return rec

    def go(rid: int) -> RedirectResponse:
        return RedirectResponse(f"/reports/{rid}", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, state: str | None = None):
        if state is not None and state not in flow.STATES:
            state = None
        counts = {s: len(store.list(s)) for s in flow.STATES}
        return page(request, "list.html", reports=store.list(state), state=state, counts=counts)

    def new_page(request: Request, form: dict, status_code: int = 200, error=None, notice=None, existing=None):
        on = sorted({s.name for s in sources})
        return page(request, "new.html", status_code=status_code, form=form, error=error, notice=notice,
                    existing=existing, sources_on=on, sources_off=sources_off)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, period: str = "30"):   # 설계 의도 11
        return page(request, "dashboard.html", m=metrics.compute(store.list(), store.history(), period))

    @app.get("/new", response_class=HTMLResponse)
    def new(request: Request):
        return new_page(request, {"track": "A"})

    @app.post("/fetch")
    async def fetch(request: Request):   # 설계 의도 9
        form = await request.form()
        url = (form.get("link") if isinstance(form.get("link"), str) else "").strip()
        try:
            source, canonical = intake.identify(sources, url)
            existing = store.find_by_url(canonical)
            if existing:   # 설계 의도 10
                return new_page(request, {"track": "A", "link": url}, 409, error="이미 접수한 링크입니다",
                                existing=existing)
            report = await run_in_threadpool(source.fetch, canonical)
        except intake.IntakeError as e:
            return new_page(request, {"track": "A", "link": url}, 422, error=f"가져오지 못했습니다: {e}")
        prefill = {"track": report.track, "title": report.title, "body": report.body, "board": report.board,
                   "posted_at": report.posted_at, "reporter": report.reporter, "source": report.source,
                   "url": report.url, "link": url}
        notice = f"{source.name} 에서 가져왔습니다. 내용을 확인하고 고친 뒤 [트리아지]를 누르세요."
        if len(report.title) > config.TITLE_MAX or len(report.body) > config.BODY_MAX:
            notice += f" 제목은 {config.TITLE_MAX}자, 본문은 {config.BODY_MAX}자까지 받습니다(측정한 리포트 길이 범위). 요점만 남겨 줄여 주세요."
        return new_page(request, prefill, notice=notice)

    @app.post("/reports")
    async def create(request: Request):
        form = dict(await request.form())
        try:
            report = from_form(form)
            if report.url:   # 숨은 칸의 출처·링크가 실제 출처와 맞는지 — 맞춘 링크가 아니면 받지 않는다
                source, canonical = intake.identify(sources, report.url)
                if (source.name, canonical) != (report.source, report.url):
                    raise InputError("출처 정보가 맞지 않습니다. 링크로 다시 가져와 주세요")
        except (InputError, intake.IntakeError) as e:
            return new_page(request, form, 422, error=str(e))
        existing = store.find_by_url(report.url)
        if existing:   # 설계 의도 10
            return new_page(request, form, 409, error="이미 접수한 링크입니다", existing=existing)
        rid = store.add(report)
        worker.submit(rid)
        return go(rid)

    @app.get("/reports/{rid}", response_class=HTMLResponse)
    def detail(request: Request, rid: int):
        rec = get_or_404(rid)
        return page(request, "detail.html", rec=rec, values=rec["final_fields"] or rec["model_fields"] or {},
                    problems=[])

    @app.post("/reports/{rid}/register")
    async def register(request: Request, rid: int):
        get_or_404(rid)
        form = await request.form()
        values = {f: v if isinstance(v := form.get(f, ""), str) else "" for f in FIELDS if f != "모듈"}
        values["모듈"] = " / ".join(m for m in form.getlist("모듈") if isinstance(m, str))
        try:   # DB·BTS 호출은 기다리는 동안 다른 요청을 막지 않게 스레드에서 (설계 의도 6)
            await run_in_threadpool(review.register, store, tracker, rid, values)
        except ValueError as e:
            return page(request, "detail.html", status_code=422, rec=store.get(rid), values=values, problems=e.args[0])
        except flow.FlowError as e:
            return refused(request, rid, e)
        return go(rid)

    def refused(request: Request, rid: int, e: Exception) -> HTMLResponse:
        rec = store.get(rid)
        return page(request, "detail.html", status_code=409, rec=rec,
                    values=rec["final_fields"] or rec["model_fields"] or {}, problems=[str(e)])

    @app.post("/reports/{rid}/discard")
    def discard(request: Request, rid: int):
        get_or_404(rid)
        try:
            review.discard(store, rid)
        except flow.FlowError as e:
            return refused(request, rid, e)
        return go(rid)

    @app.post("/reports/{rid}/back-to-review")
    def back_to_review(request: Request, rid: int):
        get_or_404(rid)
        try:
            review.back_to_review(store, rid)
        except flow.FlowError as e:
            return refused(request, rid, e)
        return go(rid)

    @app.get("/reports/{rid}/bts-status", response_class=HTMLResponse)
    def bts_status(request: Request, rid: int):
        rec = get_or_404(rid)
        if not rec["ticket_key"]:
            return go(rid)
        try:
            status, problems = tracker.status(rec["ticket_key"]), []
        except Exception as e:
            status, problems = None, [f"BTS 상태를 가져오지 못했습니다: {e}"]
        return page(request, "detail.html", rec=rec, values=rec["final_fields"] or {}, problems=problems,
                    bts_status=status)

    @app.post("/reports/{rid}/retry")
    def retry(request: Request, rid: int):
        get_or_404(rid)
        try:
            review.retry(store, worker, rid)
        except flow.FlowError as e:
            return refused(request, rid, e)
        return go(rid)

    @app.get("/fake-bts/{key}", response_class=HTMLResponse)
    def fake_ticket(request: Request, key: str):
        if getattr(tracker, "name", "") != "fake":
            raise HTTPException(404)
        try:
            ticket = tracker.get(key)
        except Exception:
            raise HTTPException(404, "티켓이 없습니다")
        return page(request, "fake_ticket.html", ticket=ticket)

    return app


def main(argv: list[str] | None = None) -> int:
    """uv run triage-serve [--check]. --check 는 서버를 띄우지 않고 BTS 연결(설정·토큰·검색 권한)만 확인한다."""
    argv = sys.argv[1:] if argv is None else argv
    sys.stdout.reconfigure(encoding="utf-8")
    config.load_env()
    try:   # 설계 의도 8
        loaded = settings.load(config.mapping_path())
    except settings.SettingsError as e:
        print(e)
        return 2
    try:
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
