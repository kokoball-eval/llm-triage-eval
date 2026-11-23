"""작업 페이지 화면 — 리포트 넣기 → 트리아지 진행 상태 → 8칸 확인·수정 → BTS 등록, 대시보드.

실행: uv run triage-serve (cli.py)

[설계 의도]
1. FastAPI 가 HTML 을 만들어 보내는 서버 렌더링이다. 별도 프런트 빌드가 없어 Python 하나로 유지보수한다(문서/서비스_설계.md §3).
2. 화면은 얇게 둔다. 입력 검사는 intake/paste.py, 링크 미리보기·접수 확인은 intake/reception.py, 등록·폐기 규칙은
   review.py, 상태 규칙은 flow.py, 지표는 metrics.py 에 있고 이 파일은 그 결과를 보여 주기만 한다.
3. 템플릿은 자동 이스케이프한다(Jinja2 기본). 리포트 원문은 외부 유저가 쓴 글이라 HTML·스크립트가 들어 있을 수 있다.
4. 트리아지가 끝날 때까지 상세 화면은 브라우저 기본 기능(meta refresh)으로 다시 읽고, 대시보드는 기간을 바꾸면 페이지를
   다시 읽는다. 부분 갱신(HTMX)이 필요한 곳이 없어 넣지 않았다. 막대 그래프는 HTML·CSS 라 CDN 없이 사내망에서 동작한다.
5. create_app() 이 저장소·작업자·BTS·출처를 인자로 받아, 시험에서 임시 DB·가짜 모델로 같은 화면을 돌린다.
6. 링크 가져오기·등록은 네트워크(BTS·게시판)를 기다리는 동안 다른 화면 요청을 막지 않도록 스레드에서 실행한다.
   허용되지 않은 동작(이미 등록된 건의 폐기 등)은 오류 JSON 이 아니라 상세 화면에 이유를 보여 준다.
7. 다른 사이트에서 보낸 요청을 막는다. 이 PC 에서 열어 둔 다른 사이트가 브라우저를 통해 몰래 [등록]·[폐기]를 보내면
   실제 BTS 토큰으로 티켓이 생긴다. 상태를 바꾸는 요청(POST)은 Origin(없으면 Referer)이 이 서버와 같을 때만 받는다.
   로그인 쿠키를 쓰지 않으므로 토큰 방식보다 이 확인이 단순하고 충분하다. Host 머리글도 127.0.0.1·localhost 만 받는다
   (외부 도메인이 이 PC 를 가리키게 만드는 DNS 리바인딩 차단, config 설계 의도 7).
8. 첫 화면(/)은 하고 싶은 일을 고르는 입구 카드이고, 리포트 표는 /reports 다. 입구마다 단계 표시를 붙여 다음에 무엇이
   일어나는지 미리 보인다. 색·모서리 같은 디자인 값은 base.html 의 :root 한 곳에서만 정한다. 외부 글꼴·이미지를 쓰지
   않아 사내망에서도 같게 보이고, 최근 5건은 store.list() 결과를 자를 뿐이라 새 규칙이 없다.
9. 새 리포트 화면은 "직접 붙여넣기"와 "링크로 가져오기" 두 탭이고 탭은 주소(/new?mode=)로 고른다(자바스크립트 없음).
   모르는 mode 값은 붙여넣기로 간다. 가져오기가 실패하거나 중복이면 붙여넣기 탭에 이유를 보여 줘 바로 직접 붙여
   넣을 수 있다(기존 동작과 시험 그대로). 게시판·작성자·참고 칸은 입력 종류에 따라 쓰는 칸이라 <details> 로 접고,
   가져오기로 채워졌으면 펼친다. 폼의 칸 이름과 전송 주소는 바꾸지 않아 입력 검사·접수 규칙은 그대로다.
"""

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from triage_eval.contracts import ENUMS, FIELDS, MODULES, NO_MODULE, split_modules
from triage_service import assign, config, flow, metrics, review
from triage_service.bts import make_tracker, settings
from triage_service.contracts import REPORTERS, TRACKS
from triage_service import intake
from triage_service.intake import reception
from triage_service.intake import settings as intake_settings
from triage_service.intake.paste import InputError
from triage_service.store import Store
from triage_service.worker import Worker

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(TITLE_MAX=config.TITLE_MAX, BODY_MAX=config.BODY_MAX, split_modules=split_modules, FIELDS=FIELDS, ENUMS=ENUMS, MODULES=MODULES, NO_MODULE=NO_MODULE, TRACKS=TRACKS, REPORTERS=REPORTERS,
                             STATES=flow.STATES, PENDING=flow.PENDING, REVIEW=flow.REVIEW, FAILED=flow.FAILED,
                             REFRESH=config.STATUS_REFRESH_SEC)


def create_app(store: Store | None = None, worker: Worker | None = None, tracker=None,
               start_worker: bool = True, allowed_hosts=config.ALLOWED_HOSTS,
               sources: list | None = None, sources_off: dict | None = None, assign_rules: dict | None = None) -> FastAPI:
    """assign_rules: 자동 배정 규칙(설정 [assign], 없으면 배정하지 않음). tracker 를 넘기지 않으면 설정 파일에서 둘 다 읽는다."""
    store = store or Store(config.DB_PATH)
    worker = worker or Worker(store)
    if tracker is None:
        loaded = settings.load(config.mapping_path())
        tracker, assign_rules = make_tracker(loaded, store), loaded["assign"]
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

    def detail_page(request: Request, rec: dict, values: dict, problems: list, status_code: int = 200,
                    team: str = assign.AUTO) -> HTMLResponse:
        return page(request, "detail.html", status_code=status_code, rec=rec, values=values, problems=problems,
                    teams=assign.teams(assign_rules), team=team, AUTO=assign.AUTO,
                    suggested=assign.suggest(assign_rules, values))

    def get_or_404(rid: int) -> dict:
        rec = store.get(rid)
        if rec is None:
            raise HTTPException(404, "리포트가 없습니다")
        return rec

    def go(rid: int) -> RedirectResponse:
        return RedirectResponse(f"/reports/{rid}", status_code=303)

    def counts() -> dict:
        return {s: len(store.list(s)) for s in flow.STATES}

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):   # 설계 의도 8
        return page(request, "home.html", counts=counts(), recent=store.list()[:5])

    @app.get("/reports", response_class=HTMLResponse)
    def index(request: Request, state: str | None = None):
        if state is not None and state not in flow.STATES:
            state = None
        return page(request, "list.html", reports=store.list(state), state=state, counts=counts())

    def new_page(request: Request, form: dict, status_code: int = 200, error=None, notice=None, existing=None,
                 mode: str = "paste"):
        on = sorted({s.name for s in sources})
        return page(request, "new.html", status_code=status_code, form=form, error=error, notice=notice,
                    existing=existing, sources_on=on, sources_off=sources_off, mode=mode)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, period: str = "30"):   # 설계 의도 4
        return page(request, "dashboard.html", m=metrics.compute(store.list(), store.history(), period))

    @app.get("/new", response_class=HTMLResponse)
    def new(request: Request, mode: str = "paste"):   # 설계 의도 9
        return new_page(request, {"track": "A"}, mode="link" if mode == "link" else "paste")

    @app.post("/fetch")
    async def fetch(request: Request):
        form = await request.form()
        link = (form.get("link") if isinstance(form.get("link"), str) else "").strip()
        try:
            prefill, notice = await run_in_threadpool(reception.preview, sources, store, link)
        except reception.Duplicate as e:
            return new_page(request, {"track": "A", "link": link}, 409, error=str(e), existing=e.existing)
        except intake.IntakeError as e:
            return new_page(request, {"track": "A", "link": link}, 422, error=f"가져오지 못했습니다: {e}")
        return new_page(request, prefill, notice=notice)

    @app.post("/reports")
    async def create(request: Request):
        form = dict(await request.form())
        try:
            report = reception.accept(sources, store, form)
        except reception.Duplicate as e:
            return new_page(request, form, 409, error=str(e), existing=e.existing)
        except (InputError, intake.IntakeError) as e:
            return new_page(request, form, 422, error=str(e))
        rid = store.add(report)
        worker.submit(rid)
        return go(rid)

    @app.get("/reports/{rid}", response_class=HTMLResponse)
    def detail(request: Request, rid: int):
        rec = get_or_404(rid)
        a = rec["assignment"]   # 등록 실패 후 검토로 되돌린 건은 사람이 고른 팀을 그대로 보인다 (review 설계 의도 7)
        team = a["team"] if a and a["team"] != a["suggested"] else assign.AUTO
        return detail_page(request, rec, rec["final_fields"] or rec["model_fields"] or {}, [], team=team)

    @app.post("/reports/{rid}/register")
    async def register(request: Request, rid: int):
        get_or_404(rid)
        form = await request.form()
        values = {f: v if isinstance(v := form.get(f, ""), str) else "" for f in FIELDS if f != "모듈"}
        values["모듈"] = " / ".join(m for m in form.getlist("모듈") if isinstance(m, str))
        team = t if isinstance(t := form.get("team", assign.AUTO), str) else assign.AUTO
        try:   # DB·BTS 호출은 기다리는 동안 다른 요청을 막지 않게 스레드에서 (설계 의도 6)
            await run_in_threadpool(review.register, store, tracker, rid, values, assign_rules, team)
        except ValueError as e:
            return detail_page(request, store.get(rid), values, e.args[0], 422, team)
        except flow.FlowError as e:
            return refused(request, rid, e)
        return go(rid)

    def refused(request: Request, rid: int, e: Exception) -> HTMLResponse:
        rec = store.get(rid)
        return detail_page(request, rec, rec["final_fields"] or rec["model_fields"] or {}, [str(e)], 409)

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
