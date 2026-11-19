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
"""

from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from fastapi.templating import Jinja2Templates

from triage_eval.contracts import ENUMS, FIELDS, MODULES, NO_MODULE, split_modules
from triage_service import config, flow, review
from triage_service.bts import make_tracker
from triage_service.contracts import REPORTERS, TRACKS
from triage_service.intake.paste import InputError, from_form
from triage_service.store import Store
from triage_service.worker import Worker

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(split_modules=split_modules, FIELDS=FIELDS, ENUMS=ENUMS, MODULES=MODULES, NO_MODULE=NO_MODULE, TRACKS=TRACKS, REPORTERS=REPORTERS,
                             STATES=flow.STATES, PENDING=flow.PENDING, REVIEW=flow.REVIEW, FAILED=flow.FAILED,
                             REFRESH=config.STATUS_REFRESH_SEC)


def create_app(store: Store | None = None, worker: Worker | None = None, tracker=None,
               start_worker: bool = True) -> FastAPI:
    store = store or Store(config.DB_PATH)
    worker = worker or Worker(store)
    tracker = tracker or make_tracker(config.TRACKER, store)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()
        yield

    app = FastAPI(title="트리아지 작업 페이지", lifespan=lifespan)

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

    @app.get("/new", response_class=HTMLResponse)
    def new(request: Request):
        return page(request, "new.html", form={"track": "A"}, error=None)

    @app.post("/reports")
    async def create(request: Request):
        form = dict(await request.form())
        try:
            report = from_form(form)
        except InputError as e:
            return page(request, "new.html", status_code=422, form=form, error=str(e))
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


def main() -> None:
    print(f"작업 페이지: http://{config.HOST}:{config.PORT}  (종료: Ctrl+C)")
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT)


if __name__ == "__main__":
    main()
