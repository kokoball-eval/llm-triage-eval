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
10. 검토 화면은 원문(왼쪽)과 8칸 폼(오른쪽)을 두 단으로 놓아 원문을 보며 바로 고친다. 좁은 화면에서는 한 단이 된다. 모델이
   낸 값과 달라진 칸은 테두리로 표시하고, 모델 원본 응답이 든 처리 기록은 검토에 필요한 정보가 아니라 접는다. 대시보드의
   카드·막대 스타일은 base.html 로 옮겨 첫 화면과 같은 틀을 쓰고, 맨 위 핵심 숫자는 metrics.compute() 결과를 그대로 보인다.
11. 화면은 BTS 에서 읽어 둔 상태(sync.py)를 보여 주기만 한다(설계 의도 20 의 홈·내 업무). BTS 를 부르는 것은 [지금 동기화]
   (POST /qa/sync)뿐이고, 네트워크를 기다리는 동안 다른 요청을 막지 않도록 스레드에서 실행한다(설계 의도 6).
12. QA 동작은 상세 화면에서만 누른다(POST /reports/{id}/qa). 원문과 확정한 8칸을 보며 판단하게 하려는 것이다. 버튼은 마지막으로
   읽은 BTS 상태에서 할 수 있는 동작만 보이고, 실제 허용 여부는 qa.act 가 BTS 를 다시 읽어 정한다. 결과는 QA 기록 표로 보인다.
13. 첫 화면 맨 위에 마일스톤 성과 카드를 둔다(QA 성과는 마일스톤 단위로 몇 건을 Closed 했는가다). 마일스톤 목록과 티켓은
   동기화로 읽어 둔 값을 보인다(S5-4d 부터는 열 때 오래됐으면 뒤에서 동기화한다, 설계 의도 15). 검토 화면의 마일스톤 칸도 읽어 둔 출시 전 버전만
   보이고 기본값은 현재 마일스톤이다(sync 설계 의도 9).
14. 대시보드의 QA 지표도 마일스톤 단위다(metrics 설계 의도 8). 고른 마일스톤이 없거나 모르는 이름이면 현재 마일스톤이고,
   (S5-5c 에서 기간 필터는 트리아지 품질 화면으로 옮겨 대시보드에는 마일스톤만 남았다.) 모든 마일스톤을 출시했으면 첫 화면과
   대시보드는 마지막 마일스톤을 보인다(읽어 둔 마일스톤이 있는데 "없다"고 안내하지 않게, 독립 검토에서 발견).
15. 홈·내 업무·대시보드는 열 때 마지막 동기화가 오래됐으면 뒤에서 동기화를 시작하고(sync 설계 의도 10), 읽어 둔 값으로
   바로 연다. 도는 동안은 상세 화면처럼 자동으로 다시 읽어 끝난 값을 보인다. 모든 화면 머리에 BTS 연결 표시(sync 설계 의도 12)를
   두고, 누르면 마지막 동기화 결과가 있는 홈으로 간다. 설계 의도 11 의 "BTS 를 부르는 것은 버튼뿐"은 이것으로 바뀌었다.
   등록과 QA 동작은 동기화와 같은 잠금 안에서 한다(sync 설계 의도 11).
16. 검토 화면에서 규칙의 팀 그룹 구성원과 각자의 미해결 수를 보이고 담당자를 고르게 한다(assign 설계 의도 8·9). BTS 를
   부르므로 검토 대기 건을 열 때만 읽고, 읽지 못해도 화면은 열며 등록할 때 다시 읽는다. 고른 구성원은 팀을 "규칙대로"로 둘
   때만 쓴다 — 팀을 바꾸면 그 팀에서 규칙대로 정한다. 긴급 사인 요청은 QA팀장으로 고정이라 고르지 않는다.
17. 대시보드(/dashboard)는 QA팀 대응 현황(마일스톤 진행·스프린트 현황·대응 흐름·팀별·구성원별 부하·회귀 테스트), 트리아지
   품질(/triage)은 모델과 검토 지표(기간별)로 나눈다(S5-5c, metrics 설계 의도 9). 보는 사람과 질문이 다르기 때문이다 — QA 리드는
   "지금 어디서 막혔나", 모델 운영자는 "모델이 얼마나 맞히나"를 본다.
18. 검토 화면에서 목표 스프린트(sprint.py)를 고른다. 기본값은 리포트를 받은 날과 우선순위로 정한 규칙의 업데이트이고,
   선택지는 지금 진행 중인 업데이트부터 6주다. 대시보드는 고른 스프린트(기본: 지금 진행 중)의 목표 이슈 완료 현황을 보인다.
   스프린트 표식은 마일스톤 티켓에서 읽으므로 마일스톤이 없는 티켓은 세지 않는다.
19. 망 접속 모드(cli --lan, auth.py)에서는 로그인하지 않은 요청을 로그인 화면으로 보낸다. 이 PC 자신(127.0.0.1)에서 온 요청은
   지금처럼 받는다. 로그인 화면과 휴대폰 접속 화면(주소·QR)은 망 접속 모드에서만 있다. 받는 주소 목록과 다른 사이트 요청 차단은
   그대로라, 휴대폰은 이 PC 의 와이파이 주소로만 들어온다.
20. 메뉴는 홈·내 업무·리포트·대시보드·트리아지 품질 다섯 개다(2026-11-26 경민 요청, 문서/화면_설계.md). 홈은 QA 업무 현황만
   (현재 마일스톤·이번 스프린트·QA Review 이슈와 담당자, [지금 동기화]와 마지막 결과), 내 업무는 지금 처리할 일(내 QA Review·
   회귀 테스트, 검토 대기·실패 리포트 — 버튼이 그 처리 칸으로 데려간다)과 새 리포트 넣기, 내가 넘긴 이슈다. 예전 QA 보드(/qa)는
   홈으로 넘기고, [지금 동기화]는 끝나면 홈으로 돌아간다. 새 리포트 화면(/new)은 링크 미리보기·입력 오류를 다시 그릴 때 쓰고
   메뉴에서는 내 업무에 속한다.
21. 사람별 로그인(auth 설계 의도 5)이면 요청마다 로그인한 사람을 확인해 이 PC 에서도 로그인을 받는다. 내 업무의 "나"와 긴급 사인
   확인은 로그인한 사람의 BTS 계정이고, 등록·QA 동작에는 누른 사람의 이름을 남긴다(배정 기록 by, QA 동작 기록 actor, BTS 코멘트
   "(실행: 이름)"). BTS 에 쓰는 토큰은 서비스 공용이라 BTS 화면에는 공용 계정으로 보인다 — 누가 했는지는 이 기록으로 안다.
   메뉴·동작을 역할(리더·실무자)로 나누는 것은 다음 단계다.
22. 역할(auth 설계 의도 6): 리더만 대시보드·트리아지 품질을 열고(실무자에게는 메뉴에서 숨기고, 주소로 와도 "권한이 없습니다"),
   내 업무에서 QA팀 구성원을 골라 그 사람의 일을 보고(?who=), 긴급 사인·담당 바꾸기를 한다. 담당 바꾸기의 사람은 동기화로 읽은
   BTS 구성원 표에서만 받는다(화면 입력은 신뢰 경계). 사람별 로그인이 아니면 역할이 없어 예전처럼 모두 열린다.
"""

from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
import segno
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from triage_eval.contracts import ENUMS, FIELDS, MODULES, NO_MODULE, split_modules
from triage_service import assign, auth, config, flow, metrics, qa, review, sync
from triage_service import sprint as sprint_mod   # 대시보드의 sprint 매개변수와 이름이 겹치지 않게
from triage_service.bts import make_tracker, settings
from triage_service.contracts import REPORTERS, TRACKS, Member
from triage_service import intake
from triage_service.intake import reception
from triage_service.intake import settings as intake_settings
from triage_service.intake.paste import InputError
from triage_service.store import Store
from triage_service.worker import Worker

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals.update(TITLE_MAX=config.TITLE_MAX, BODY_MAX=config.BODY_MAX, split_modules=split_modules, FIELDS=FIELDS, ENUMS=ENUMS, MODULES=MODULES, NO_MODULE=NO_MODULE, TRACKS=TRACKS, REPORTERS=REPORTERS,
                             STATES=flow.STATES, PENDING=flow.PENDING, REVIEW=flow.REVIEW, FAILED=flow.FAILED,
                             REFRESH=config.STATUS_REFRESH_SEC, BOTTLENECK_DAYS=config.BOTTLENECK_DAYS, ROLES=auth.ROLES, can=auth.can)


def create_app(store: Store | None = None, worker: Worker | None = None, tracker=None,
               start_worker: bool = True, allowed_hosts=config.ALLOWED_HOSTS,
               sources: list | None = None, sources_off: dict | None = None, assign_rules: dict | None = None,
               login: auth.Login | None = None, lan_urls: list[tuple[str, str]] | None = None) -> FastAPI:
    """assign_rules: 자동 배정 규칙(설정 [assign], 없으면 배정하지 않음). tracker 를 넘기지 않으면 설정 파일에서 둘 다 읽는다.
    login·lan_urls: 망 접속 모드(cli --lan)의 로그인과 휴대폰으로 열 수 있는 (주소, 종류) 목록 — 없으면 지금처럼 이 PC 만 받는다(설계 의도 19)."""
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

    @app.middleware("http")
    async def login_required(request: Request, call_next):   # 설계 의도 19·21
        user = login.valid(request.cookies.get(auth.COOKIE)) if login else None
        request.state.user = user
        here = request.client.host in ("127.0.0.1", "::1") and not login.users if login else True   # 사람별 로그인이면 이 PC 도 로그인
        if login and user is None and not here and request.url.path not in ("/login", "/logout"):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    people_teams = tuple(assign.teams(assign_rules)) if login and login.users and assign_rules else ()   # sync 설계 의도 16
    syncer = app.state.syncer = sync.Syncer(store, tracker, assign_rules["qa"] if assign_rules else "", people_teams)   # app.state: 시험이 뒤에서 도는 동기화를 기다릴 수 있게

    def page(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"bts": syncer.status(), "lan": bool(lan_urls), "user": getattr(request.state, "user", None),
                                                          **ctx}, status_code=status_code)

    def synced_page(request: Request, name: str, **ctx) -> HTMLResponse:
        """설계 의도 15 — 홈·내 업무·대시보드: 때가 됐으면 뒤에서 동기화하고 읽어 둔 값으로 바로 연다."""
        syncer.start()
        return page(request, name, auto_refresh=True, **ctx)

    def candidates(rec: dict, values: dict, suggested: str) -> dict:
        """설계 의도 16 — 검토 화면의 담당자 미리 보기. BTS 를 부르므로 검토 대기 건에서만, 실패해도 화면은 연다."""
        if rec["state"] != flow.REVIEW or not assign_rules or not suggested or values.get("처리") == assign.URGENT:
            return {}
        try:
            cands = assign.candidates(tracker, suggested)
        except Exception as e:
            return {"cands_error": str(e)}
        return {"cands": cands, "rule_member": assign.least_busy(cands, assign.recent(store.list(flow.REGISTERED)))}

    def detail_page(request: Request, rec: dict, values: dict, problems: list, status_code: int = 200,
                    team: str = assign.AUTO, repro: str = assign.AUTO, milestone: str | None = None,
                    member: str = assign.AUTO, sprint_: str | None = None, **ctx) -> HTMLResponse:
        repro_now = assign.cs_repro(values, repro)
        suggested = assign.suggest(assign_rules, values, repro_now)
        milestones = store.milestones()   # 설계 의도 13
        return page(request, "detail.html", status_code=status_code, rec=rec, values=values, problems=problems,
                    teams=assign.teams(assign_rules), team=team, AUTO=assign.AUTO, repro=repro, REPRO_CHOICES=assign.REPRO_CHOICES,
                    repro_default=assign.cs_repro(values), suggested=suggested, member=member, URGENT=assign.URGENT,
                    urgent=qa.urgent(rec) and bool(assign_rules), **candidates(rec, values, suggested),
                    allowed=qa.allowed, ACTIONS=qa.ACTIONS, members=reassignable(request, rec),
                    dev_team=assign.dev_team(assign_rules, rec["final_fields"] or {}),
                    qa_log=store.qa_log().get(rec["id"], []),
                    open_milestones=[m for m in milestones if not m["released"]],
                    milestone=sync.current(milestones) if milestone is None else milestone,
                    sprints=sprint_mod.window(date.today()), sprint_label=sprint_mod.label,   # 설계 의도 18
                    sprint_=sprint_mod.default(date.fromisoformat(rec["created_at"][:10]), values.get("우선순위", ""),
                                           values.get("처리") == assign.URGENT) if sprint_ is None else sprint_, **ctx)

    def get_or_404(rid: int) -> dict:
        rec = store.get(rid)
        if rec is None:
            raise HTTPException(404, "리포트가 없습니다")
        return rec

    def go(rid: int) -> RedirectResponse:
        return RedirectResponse(f"/reports/{rid}", status_code=303)

    def counts() -> dict:
        return {s: len(store.list(s)) for s in flow.STATES}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if not login:
            raise HTTPException(404)
        return page(request, "login.html", lan=False, bts=None, guest=True, named=bool(login.users))   # 로그인 전에는 메뉴·BTS 연결 상태(주소·오류 문구)를 보이지 않는다

    @app.post("/login")
    async def login_post(request: Request):   # 설계 의도 19 — 비밀번호 비교·실패 제한은 auth.Login
        if not login:
            raise HTTPException(404)
        form = await request.form()
        password, user_id = (t if isinstance(t := form.get(k, ""), str) else "" for k in ("password", "id"))
        ok = await run_in_threadpool(login.check, password, request.client.host, user_id.strip())   # scrypt(약 40ms)가 다른 요청을 막지 않게
        if not ok:
            error = "너무 많이 틀렸습니다. 1분 뒤에 다시 시도하세요" if ok is None else                 "이름 또는 비밀번호가 맞지 않습니다" if login.users else "비밀번호가 맞지 않습니다"
            return page(request, "login.html", status_code=429 if ok is None else 401, error=error, lan=False, bts=None, guest=True,
                        named=bool(login.users), user_id=user_id)
        done = RedirectResponse("/", status_code=303)
        done.set_cookie(auth.COOKIE, login.token(ok), max_age=config.LOGIN_HOURS * 3600, httponly=True, samesite="lax")
        return done

    @app.post("/logout")
    def logout():
        done = RedirectResponse("/login", status_code=303)
        done.delete_cookie(auth.COOKIE)
        return done

    @app.get("/connect", response_class=HTMLResponse)
    def connect(request: Request):   # 설계 의도 19 — 휴대폰으로 열 주소와 QR 코드
        if not lan_urls:
            raise HTTPException(404)
        return page(request, "connect.html", urls=[(u, Markup(segno.make(u).svg_inline(scale=6)), kind) for u, kind in lan_urls], kinds=config.KINDS)

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, milestone: str = ""):   # 설계 의도 8·13
        milestones = store.milestones()
        name = shown(milestones, milestone)
        ctx = {}
        if name:   # 설계 의도 20 — 홈은 QA 업무 현황만: 마일스톤·이번 스프린트·QA Review 이슈
            everything = [t for m in milestones for t in store.tickets(m["name"])]
            ctx = {"summary": metrics.milestone_summary(store, name),
                   "sp": metrics.sprint(everything, sprint_mod.current(date.today()).isoformat(), store.carries()),
                   "review": metrics.qa_review(store.tickets(name), sync.targets(store), store.ticket_changes())}
        return synced_page(request, "home.html", milestones=milestones, sprint_label=sprint_mod.label,
                           result=syncer.last and syncer.last["result"], **ctx)

    @app.get("/reports", response_class=HTMLResponse)
    def index(request: Request, state: str | None = None):
        if state is not None and state not in flow.STATES:
            state = None
        return page(request, "list.html", reports=store.list(state), state=state, counts=counts())

    def new_page(request: Request, form: dict, status_code: int = 200, error=None, notice=None, existing=None,
                 mode: str = "paste"):
        on = sorted({s.name for s in sources})
        return page(request, "new.html", status_code=status_code, form=form, error=error, notice=notice,
                    existing=existing, sources_on=on, sources_off=sources_off, mode=mode, menu="/mine")   # 메뉴는 내 업무가 켜진다

    @app.get("/qa")
    def qa_board():   # 설계 의도 20 — QA 보드는 홈(QA Review)과 내 업무로 나눠 옮겼다. 예전 링크는 홈으로
        return RedirectResponse("/", status_code=303)

    @app.get("/mine", response_class=HTMLResponse)
    def mine(request: Request, mode: str = "", who: str = ""):   # 설계 의도 20·22, sync 설계 의도 13 — 화면은 읽어 둔 값만 보여 준다
        tickets = [t for m in store.milestones() for t in store.tickets(m["name"])]
        team = syncer.groups.get(assign_rules["qa"], []) if assign_rules and auth.can(user_of(request), "team") else []
        me = next((p for p in team if p.id == who), None) or person(request)   # 리더는 팀원을 골라 본다(설계 의도 22)
        m = me and sync.mine(sync.targets(store), tickets, store.ticket_changes(), store.qa_log(), me)
        return synced_page(request, "mine.html", m=m, rules=bool(assign_rules), synced=syncer.last is not None, me_error=syncer.me_error,
                           review=store.list(flow.REVIEW), failed=store.list(flow.FAILED), open_new=bool(mode),
                           form={"track": "A"}, mode="link" if mode == "link" else "paste",
                           sources_on=sorted({s.name for s in sources}), sources_off=sources_off, team=team, viewing=me.id if me else "")

    @app.post("/qa/sync")
    async def qa_sync():   # 결과는 홈이 보인다(syncer.last)
        await run_in_threadpool(syncer.now)
        return RedirectResponse("/", status_code=303)

    def reassignable(request: Request, rec: dict) -> list:
        """설계 의도 22 — 담당 바꾸기 후보: 리더에게만, 그 이슈의 지금 담당 팀 그룹 구성원(팀은 그대로 두므로 다른 팀 사람에게 주면
        BTS 팀 표식과 회귀 실패 때 돌려줄 팀이 어긋난다)."""
        if not auth.can(user_of(request), "reassign"):
            return []
        return sorted(syncer.groups.get(qa.team_now(rec, store.qa_log().get(rec["id"], [])), []), key=lambda p: p.name)

    def user_of(request: Request):
        return getattr(request.state, "user", None)

    def forbidden(request: Request, what: str) -> HTMLResponse:
        """설계 의도 22 — 역할로 막힌 화면·동작."""
        return page(request, "forbidden.html", status_code=403, what=auth.LEADER_ONLY[what])

    def person(request: Request):
        """설계 의도 21 — 내 업무의 "나". 사람별 로그인이면 로그인한 사람의 BTS 계정, 아니면 BTS 토큰 주인(sync 설계 의도 13·16)."""
        user = getattr(request.state, "user", None)
        if not (login and login.users and user):
            return syncer.me
        return syncer.people.get(user.bts) or Member(user.bts, user.name)

    def actor(request: Request) -> tuple[str, str]:
        """설계 의도 21 — (BTS 계정, 이름) 누른 사람. 사람별 로그인이 아니면 ("", "") — 예전처럼 토큰 주인으로 본다."""
        user = getattr(request.state, "user", None)
        return (user.bts, user.name) if login and login.users and user else ("", "")

    def shown(milestones: list[dict], wanted: str) -> str:
        """설계 의도 13·14 — 고른 마일스톤, 없으면 현재, 모두 출시했으면 마지막 마일스톤."""
        if wanted in {m["name"] for m in milestones}:
            return wanted
        return sync.current(milestones) or (milestones[-1]["name"] if milestones else "")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, milestone: str = "", sprint: str = ""):   # 설계 의도 14·17·18 — QA팀 대응 현황
        if not auth.can(user_of(request), "/dashboard"):   # 설계 의도 22
            return forbidden(request, "/dashboard")
        milestones = store.milestones()
        name = shown(milestones, milestone)
        sprints, now_ = sprint_mod.window(date.today(), before=2, after=3), sprint_mod.current(date.today()).isoformat()
        chosen = sprint if sprint in sprints else now_
        ctx = {}
        if name:
            tickets, changes = store.tickets(name), store.ticket_changes()
            everything = [t for m in milestones for t in store.tickets(m["name"])]
            ctx = {"ms": metrics.milestone_summary(store, name), "q": metrics.qa(tickets, changes), "r": metrics.response(tickets, changes),
                   "sp": metrics.sprint(everything, chosen, store.carries())}
        return synced_page(request, "dashboard.html", milestones=milestones, milestone=name, sprints=sprints, sprint=chosen, sprint_now=now_,
                           sprint_label=sprint_mod.label, **ctx)

    @app.get("/triage", response_class=HTMLResponse)
    def triage_quality(request: Request, period: str = "30"):   # 설계 의도 17 — BTS 를 읽지 않는 화면이라 자동 동기화하지 않는다
        if not auth.can(user_of(request), "/triage"):   # 설계 의도 22
            return forbidden(request, "/triage")
        return page(request, "triage.html", m=metrics.compute(store.list(), store.history(), period))

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
        repro = t if isinstance(t := form.get("repro", assign.AUTO), str) else assign.AUTO
        milestone = t if isinstance(t := form.get("milestone", ""), str) else ""
        member = t if isinstance(t := form.get("member", assign.AUTO), str) and team == assign.AUTO else assign.AUTO   # 설계 의도 16
        sprint_ = t if isinstance(t := form.get("sprint", ""), str) else ""
        try:   # DB·BTS 호출은 기다리는 동안 다른 요청을 막지 않게 스레드에서 (설계 의도 6)
            await run_in_threadpool(syncer.hold, review.register, store, tracker, rid, values, assign_rules, team, repro, milestone, member, sprint_,
                                    actor(request)[1])
        except ValueError as e:   # 다시 그릴 때도 구성원을 BTS 에서 읽으므로 스레드에서 (설계 의도 6)
            return await run_in_threadpool(detail_page, request, store.get(rid), values, e.args[0], 422, team, repro, milestone, member, sprint_)
        except flow.FlowError as e:
            return refused(request, rid, e)
        return go(rid)

    def refused(request: Request, rid: int, e: Exception) -> HTMLResponse:
        rec = store.get(rid)
        return detail_page(request, rec, rec["final_fields"] or rec["model_fields"] or {}, [str(e)], 409)

    @app.post("/reports/{rid}/qa")
    async def qa_action(request: Request, rid: int):   # 설계 의도 12
        get_or_404(rid)
        form = await request.form()
        action, comment, team, chosen = (v if isinstance(v := form.get(k, d), str) else d
                                         for k, d in (("action", ""), ("comment", ""), ("team", assign.AUTO), ("assignee", "")))
        user = user_of(request)
        if action in ("sign", "reassign") and not auth.can(user, action):   # 설계 의도 22 — 서버가 역할로 거절
            return forbidden(request, action)
        picked = next((p for p in reassignable(request, get_or_404(rid)) if p.id == chosen), None) if action == "reassign" else None
        if action == "reassign" and picked is None:   # 화면 입력은 신뢰 경계 — 동기화로 읽은 구성원만
            return qa_refused(request, rid, ["담당자는 이 이슈의 지금 담당 팀 그룹 구성원 중에서 고르세요(홈에서 동기화하면 목록이 새로 읽힙니다)"], 422, comment, team)
        try:
            result = await run_in_threadpool(syncer.hold, qa.act, store, tracker, rid, action, comment, team, assign_rules,
                                             *actor(request), bool(user and user.id and user.role == "leader"),
                                             picked.id if picked else "", picked.name if picked else "")
        except ValueError as e:
            return qa_refused(request, rid, e.args[0], 422, comment, team)
        except qa.Conflict as e:
            return qa_refused(request, rid, [str(e)], 409, comment, team)
        if not result["ok"]:
            return qa_refused(request, rid, [result["error"]], 502, comment, team)
        return go(rid)

    def qa_refused(request: Request, rid: int, problems: list, status_code: int, comment: str, team: str) -> HTMLResponse:
        rec = store.get(rid)   # 쓴 코멘트와 고른 팀을 그대로 돌려준다
        return detail_page(request, rec, rec["final_fields"] or {}, problems, status_code, comment=comment, qa_team=team)

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
        return detail_page(request, rec, rec["final_fields"] or {}, problems, bts_status=status)

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
