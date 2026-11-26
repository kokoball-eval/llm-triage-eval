"""BTS 상태 동기화 — 등록한 티켓과 마일스톤 티켓의 BTS 상태를 읽어 저장하고, 내 업무 목록(mine)을 만든다.

[설계 의도]
1. BTS 가 원본이다. 서비스는 읽어 온 값을 복사해 두고 보여 줄 뿐이다. BTS 에 쓰는 것은 QA 가 누른 동작뿐이다(qa.py).
   개발자는 계속 BTS 에서 일하므로 서비스가 자기 상태를 따로 가지면 같은 버그의 상태가 두 곳에서 달라진다.
2. 읽기는 끌어오기다. 서비스는 이 PC(127.0.0.1)에서만 열려 있어 BTS 가 변경을 알려 주는 웹훅을 받을 수 없다. [지금 동기화]
   버튼과 화면을 열 때의 자동 동기화(설계 의도 10) 두 길로만 읽는다.
3. 상태는 다섯 이름(contracts.STATUSES)으로 나눈다: QA Review → 재현 대기, Resolve → 회귀 테스트 대기, Check → 보류.
   팀별 미해결은 대시보드가 BTS 팀 표식 기준으로 보인다. (옛 QA 보드는 2026-11-26 화면 개편 때 홈·내 업무로 나눠 옮겼다.) 다섯 이름 밖의 상태를 읽으면 그 이름을 화면에 그대로 보여 BTS 워크플로가 바뀐 것을 알아챈다.
   (S5-2 의 상태 대응 설정 [<bts>.status] 는 상태 이름을 통일하면서 없앴다.)
4. 읽기에 실패한 묶음은 이전에 읽은 값을 그대로 두고 이유를 보여 준다. 일부만 실패해도 성공한 묶음은 갱신한다. BTS 에서
   찾지 못한 티켓(지워졌거나 접근 권한 밖)은 갱신하지 않고 목록으로 알린다.
5. 대기 시간은 "BTS 가 그 티켓을 마지막으로 바꾼 뒤 경과 시간"이다. 상태가 바뀐 시각이 아니라, 회귀 테스트 대기 중 코멘트가
   달려도 줄어든다. 정확한 상태 변경 시각은 마일스톤 티켓의 상태 변화 기록(store.bts_ticket_changes, 발견한 시각)으로 센다.
6. 대상은 등록한 리포트 중 최근 config.SYNC_LIMIT 건이다. 보드도 같은 대상만 센다(동기화하지 않는 오래된 건이 "동기화 전"으로
   섞이지 않게).
7. 팀은 재현 확인으로 바꾼 팀(QA 기록의 마지막 팀)이 있으면 그것, 없으면 등록 때 배정한 팀이다(store 설계 의도 10). 기록의
   팀은 BTS 가 쓰기 요청을 받은 경우에만 채워진다(qa 설계 의도 3). 상태 확인에서만 실패한 기록도 팀은 바뀐 것이라 센다.
8. 같은 버튼으로 마일스톤도 읽는다: BTS 버전 목록을 저장하고, 출시 전 버전마다 그 버전의 티켓 전체를 읽는다(마일스톤당
   config.MILESTONE_LIMIT 건까지). 출시한 버전은 마지막에 읽은 값을 그대로 둔다. 버전 목록·한 버전 읽기가 실패해도 나머지는 계속한다.
9. 현재 마일스톤은 출시 전 버전 중 출시일이 가장 이른 것이다(출시일이 지났어도 출시 처리 전이면 현재). 마일스톤 성과는 그
   버전의 BTS 티켓 전체로 센다: Closed 수·완료율, 남은 상태별 건수, 재오픈(Resolve → In Dev 를 본 티켓) 수, QA 가 서비스에서
   회귀 테스트 통과로 닫은 수(QA 기록). 상태 변화는 발견한 시각 기준이라 동기화 사이에 오간 것은 놓칠 수 있다.
10. 홈·내 업무·대시보드를 열 때 마지막 동기화(실패 포함)가 config.AUTO_SYNC_MINUTES 분보다 오래됐으면 뒤에서 동기화한다
   (2026-11-25 경민 결정: 변화 시각이 동기화 간격만큼 늦게 잡히는 것을 줄인다). 화면은 기다리지 않고 읽어 둔 값으로 열린다.
   BTS 가 느리거나 멈춰도 화면이 묶이지 않게 하려는 것이다. 실패해도 다음 시도는 다시 그 간격 뒤라 BTS 가 멈춘 동안 화면을 열
   때마다 부르지 않는다. 마지막 시각은 서버 메모리에만 둔다(다시 켜면 처음 여는 화면에서 읽는다).
11. 동기화는 잠금 하나로 한 번에 하나만 돈다. 자동 동기화가 도는 중이면 새로 시작하지 않고, [지금 동기화]는 그것이 끝나길
   기다렸다가 다시 읽는다. 연달아 누른 두 동기화가 겹쳐 늦게 끝난 쪽이 오래된 값을 덮어쓰던 S5-2 의 한계도 이것으로 막힌다.
   BTS 에 쓰는 일(등록, QA 동작)도 같은 잠금 안에서 한다(Syncer.hold). 동기화가 읽어 둔 옛 상태를 QA 동작이 쓴 새 상태 뒤에
   저장해 되돌리는 일을 막는다(독립 검토에서 발견). 동기화가 도는 중이면 QA 동작은 그것이 끝날 때까지 기다린다.
12. 화면 구석의 BTS 연결 표시는 마지막 동기화 결과로 정한다(표시하려고 BTS 를 따로 부르지 않는다): 다 읽었으면 연결됨, 일부만
   실패했으면 일부 실패, 아무것도 읽지 못하고 실패했으면 연결 안 됨, 도는 중이면 읽는 중. 언제 읽은 값인지도 함께 보인다.
13. 내 업무(mine)의 나는 BTS 토큰 주인이다. 저장된 담당자는 표시 이름이라, 동기화할 때 QA팀 그룹 구성원 중 me() ID 와 같은
   사람을 찾아 이름까지 함께 기억한다(QA팀 구성원이 아니면 None — 화면이 안내한다). 내 차례는 QA Review(BTS 담당자가 나)와
   Resolve(QA 담당 qa_owner 가 나, 서비스를 거치지 않은 티켓은 BTS 담당자가 나)이고, 넘긴 이슈는 서비스가 QA 담당으로 기억한
   In Dev·Check 다. 머문 시간은 상태 변화 기록에서 그 상태로 들어온 것을 본 시각(없으면 BTS 가 마지막으로 바꾼 시각)부터 센다.
   기준(config.BOTTLENECK_DAYS)을 넘기면 병목이다. shortcut: 이름이 같은 사람이 둘이면 구분하지 못한다. 그런 계정이 생기면 담당자
   ID 도 저장한다.
14. 동기화가 BTS 에 쓰는 것은 이것 하나다(그 밖의 쓰기는 QA 가 누른 동작뿐, 설계 의도 1): 서비스가 등록했지만 QA 담당이 없는
   건(바로 개발 배정 — qa_owner 없음, 기존 이슈에 연결한 건 제외)이 Resolve 가 된 것을 발견하면 담당자를 QA팀 구성원 중
   미해결이 가장 적은 사람으로 바꾼다(2026-11-25 경민 결정: 회귀 테스트가 아무에게도 오지 않는 구멍을 막고, 사람이 병목이라 팀장이
   다시 나누지 않고 바로 자동 배정한다. 팀장도 구성원으로서 후보다). 접수 기록은 QA 동작 기록(qa.INTAKE)에 남기고,
   그 기록이 성공했으면 다시 하지 않는다 — 사람이 BTS 에서 다른 QA 구성원으로 바꿔도 되돌리지 않는다. 쓰기에 실패하면 기록에
   오류가 남아 다음 동기화에서 다시 시도하고, 실패 이유는 동기화 결과에 보인다. QA팀 그룹에 구성원이 없으면 오류로 남는다. BTS 에서 직접 만든
   이슈는 서비스 기록이 없어 대상이 아니다. 처음 배정 팀이 QA팀인 건(S5-5a 이전 기록 포함)과 쓰기 직전에 BTS 상태가 Resolve 가 아닌 건도 건너뛴다.
   shortcut: 쓰기 중 코멘트만 실패하면 재시도가 그 사이 사람이 바꾼 담당자를 되돌릴 수 있고, 계속 실패하면 동기화마다 오류
   기록이 쌓인다.
15. 스프린트 자동 이월(carry, S5-6b): 목표 업데이트가 지났는데 Closed 가 아닌 티켓의 스프린트를 지금 진행 중인 업데이트로 옮긴다
   (2026-11-25 경민 결정: 사람이 병목이라 사람 손 없이 돈다). 업데이트 날에는 아직 진행 중이라 옮기지 않고 다음 날부터 대상이다.
   Check(보류)도 미해결이라 옮긴다. 대상은 출시 전 마일스톤의 티켓 전부다(서비스가 등록하지 않은 것 포함, 출시한 마일스톤은 마지막
   값을 그대로 둔다). 옮기는 곳은 몇 주 밀렸어도 지금 진행 중인 업데이트다. 쓰기 직전에 BTS 상태를 다시 읽어 그사이 Closed 가
   됐으면 옮기지 않는다. 동기화가 BTS 에 쓰는 두 번째 경우다(첫째는 설계 의도 14). 스프린트 표식을 바꾼 뒤 이월 기록
   (store.save_carry)을 남기고 그 다음에 BTS 코멘트("이월 N회")를 쓴다 — 코멘트만 실패해도 표식은 이미 옮겨졌으니 횟수가 새지 않게
   기록을 먼저 남기고 실패는 동기화 결과에 보인다. 표식 쓰기가 실패하면 조건이 그대로라 다음 동기화에서 다시 시도한다. 이월 횟수는
   기록의 줄 수다. 방금(config.CARRY_RECHECK_MINUTES) 옮긴 티켓은 다시 옮기지 않는다 — Jira 검색은 색인이라 바꾼 라벨이 몇 초 늦게
   읽혀 두 번 이월로 세는 것을 막는다(독립 검토 지적). 사람이 표식을 지난 날짜로 되돌리면 다음 동기화에서 다시 옮기고 횟수가 는다. shortcut: 이월 시각은 동기화가 발견한 때라 목요일이 지나고 처음 동기화할 때 옮겨진다.
16. 사람별 로그인(auth 설계 의도 5)이면 내 업무의 "나"는 로그인한 사람의 BTS 계정이다. 저장된 담당자는 표시 이름이라 이름이
   필요하므로, 동기화할 때 자동 배정 팀 그룹 구성원 전체를 읽어 계정 ID → 구성원 표(people)를 둔다(팀 수만큼 BTS 요청이 늘어,
   사람별 로그인일 때만 읽는다). 표에 없으면 사용자 파일의 이름으로 대신한다.
"""

import threading
from datetime import date, datetime, timedelta

from triage_service import assign, config, flow, metrics, qa, sprint
from triage_service.contracts import CHECK, CLOSED, IN_DEV, QA_REVIEW, RESOLVE, STATUSES, Member
from triage_service.store import Store, now


def targets(store: Store) -> list[dict]:
    """설계 의도 6 — 동기화·보드 대상(등록한 리포트 중 최근 SYNC_LIMIT 건, 번호 큰 순)."""
    return [r for r in store.list(flow.REGISTERED) if r["ticket_key"]][:config.SYNC_LIMIT]


def run(store: Store, tracker) -> dict:
    """BTS 에서 상태를 읽어 저장한다. {"synced": 건수, "missing": [BTS 에서 못 찾은 키], "errors": [묶음 실패 이유],
    "milestones": [(마일스톤, 읽은 티켓 수)]} (설계 의도 8)."""
    out = {"synced": 0, "missing": [], "errors": [], "milestones": []}
    records, at = targets(store), now()
    for i in range(0, len(records), config.SYNC_CHUNK):
        chunk = records[i:i + config.SYNC_CHUNK]
        try:
            found = tracker.snapshot([r["ticket_key"] for r in chunk])
        except Exception as e:   # 설계 의도 4 — 한 묶음의 실패가 나머지를 막지 않는다
            out["errors"].append(f"{chunk[0]['ticket_key']} 등 {len(chunk)}건: {e}")
            continue
        for r in chunk:
            state = found.get(r["ticket_key"])
            if state is None:
                out["missing"].append(r["ticket_key"])
                continue
            store.save_bts(r["id"], {"status": state.status, "assignee": state.assignee, "updated": state.updated, "synced_at": at})
            out["synced"] += 1
    try:   # 설계 의도 8
        milestones = tracker.versions()
    except Exception as e:
        out["errors"].append(f"마일스톤 목록: {e}")
        return out
    store.save_milestones(milestones, at)
    for m in milestones:
        if m.released:
            continue
        try:
            tickets = tracker.milestone_tickets(m.name)
        except Exception as e:
            out["errors"].append(f"마일스톤 {m.name}: {e}")
            continue
        store.save_tickets(m.name, tickets, at)
        out["milestones"].append((m.name, len(tickets)))
        if len(tickets) >= config.MILESTONE_LIMIT:
            out["errors"].append(f"마일스톤 {m.name}: {config.MILESTONE_LIMIT}건까지만 읽었습니다 — 성과가 실제보다 적게 보일 수 있습니다(config.MILESTONE_LIMIT)")
    return out



def intake(store: Store, tracker, qa_team: str, result: dict) -> dict:
    """설계 의도 14 — 방금 읽은 상태에서 Resolve 가 된 QA 담당 없는 서비스 등록 건을 QA팀 구성원 중 미해결이 가장 적은 사람에게
    넘긴다(assign.least_busy). result 에 "intake": [넘긴 티켓 키] 를 더하고, 실패 이유는 result["errors"] 에 보탠다.
    쓰기는 한 건이 실패해도 나머지를 계속한다."""
    result = {**result, "errors": list(result["errors"]), "intake": []}
    done = {rid for rid, log in store.qa_log().items() if any(a["action"] == qa.INTAKE and not a["error"] for a in log)}
    todo = [r for r in targets(store) if r["bts"] and r["bts"]["status"] == RESOLVE and r["id"] not in done and r["assignment"]
            and r["assignment"].get("team") != qa_team and not r["assignment"].get("qa_owner") and not r["assignment"].get("linked_existing")]
    recent = assign.recent(store.list(flow.REGISTERED))
    for r in todo:
        try:
            if tracker.status(r["ticket_key"]) != RESOLVE:   # 읽어 둔 상태가 묶음 실패로 오래됐을 수 있다 — 지금 상태를 다시 본다
                continue
            who = assign.least_busy(assign.candidates(tracker, qa_team), recent)   # 건마다 다시 읽어 방금 넘긴 건도 센다
            note = f"[트리아지 서비스 · {qa.INTAKE}] 개발 배정 건이 Resolve 가 되어 회귀 테스트를 QA팀에 배정했습니다 (담당: {who.name})"
            done_ = qa.write(store, tracker, r["id"], qa.INTAKE, RESOLVE, "", "", note, who.id)
        except Exception as e:
            result["errors"].append(f"{r['ticket_key']} 회귀 접수: {e}")
            continue
        if done_["ok"]:
            result["intake"].append(r["ticket_key"])
            recent = [who.id, *recent]
        else:
            result["errors"].append(f"{r['ticket_key']} 회귀 접수: {done_['error']}")
    return result


def carry(store: Store, tracker, result: dict, today: date | None = None) -> dict:
    """설계 의도 15 — 목표 업데이트가 지난 미완료 티켓을 지금 진행 중인 업데이트로 옮긴다. result 에 "carried": [옮긴 티켓 키] 를 더하고
    실패 이유는 result["errors"] 에 보탠다. 한 건이 실패해도 나머지를 계속한다."""
    result = {**result, "errors": list(result["errors"]), "carried": []}
    now_ = sprint.current(today or date.today()).isoformat()
    done = store.carries()
    for m in store.milestones():
        if m["released"]:
            continue
        for t in store.tickets(m["name"]):
            if not t["sprint"] or t["sprint"] >= now_ or t["status"] == CLOSED:
                continue
            last = (done.get(t["key"]) or [(None, None, None)])[-1]
            if last[2] == now_ and datetime.now() - datetime.fromisoformat(last[0]) < timedelta(minutes=config.CARRY_RECHECK_MINUTES):
                continue   # 방금 옮겼다 — 검색 색인이 늦어 옛 값이 읽힌 것이다
            try:
                if tracker.status(t["key"]) == CLOSED:   # 읽어 둔 상태가 오래됐을 수 있다 — 지금 상태를 다시 본다
                    continue
                tracker.update(t["key"], sprint=now_)
                store.save_carry(t["key"], t["sprint"], now_, now())
            except Exception as e:
                result["errors"].append(f"{t['key']} 스프린트 이월: {e}")
                continue
            result["carried"].append(t["key"])   # 표식은 옮겨졌고 기록도 남았다 — 코멘트가 실패해도 센다
            try:
                tracker.update(t["key"], comment=f"[트리아지 서비스 · 스프린트 이월] {sprint.label(t['sprint'])} → {sprint.label(now_)} (이월 {len(done.get(t['key'], [])) + 1}회)")
            except Exception as e:
                result["errors"].append(f"{t['key']} 스프린트 이월 코멘트: {e}")
    return result


class Syncer:
    """설계 의도 10~12 — 동기화를 한 번에 하나만 돌리고 마지막 결과를 기억한다."""

    def __init__(self, store: Store, tracker, qa_team: str = "", teams: tuple = ()):
        self.store, self.tracker, self.qa_team, self.teams, self.lock = store, tracker, qa_team, teams, threading.Lock()
        self.me: Member | None = None   # 설계 의도 13
        self.people: dict[str, Member] = {}   # 설계 의도 16 — BTS 계정 ID → 구성원(이름), 사람별 로그인일 때만 읽는다
        self.me_error = ""   # 내 계정을 못 읽은 이유 — 티켓 동기화는 멀쩡하니 연결 표시(status)에는 섞지 않고 /mine 에서만 보인다
        self.last: dict | None = None   # {"at": 시각, "result": run() 의 결과}
        self.thread: threading.Thread | None = None

    def due(self, at: datetime | None = None) -> bool:
        minutes = config.AUTO_SYNC_MINUTES
        if minutes is None or minutes <= 0:   # 0 이면 화면을 다시 읽을 때마다 동기화가 돌아 끝나지 않는다
            return False
        return self.last is None or ((at or datetime.now()) - self.last["at"]).total_seconds() >= minutes * 60

    def start(self) -> None:
        """설계 의도 10 — 때가 됐으면 뒤에서 동기화를 시작한다. 이미 도는 중이면 아무것도 하지 않는다."""
        if not (self.due() and self.lock.acquire(blocking=False)):
            return
        if not self.due():   # 잠금을 얻기 직전에 다른 동기화가 끝났다
            self.lock.release()
            return
        try:
            self.thread = threading.Thread(target=self._locked, daemon=True)
            self.thread.start()
        except BaseException:   # 스레드를 못 띄우면 잠금이 영영 잡혀 있지 않게
            self.lock.release()
            raise

    def now(self) -> dict:
        """[지금 동기화] — 도는 동기화가 있으면 끝나길 기다렸다가 읽는다(설계 의도 11)."""
        with self.lock:
            return self._run()

    def hold(self, fn, *args):
        """설계 의도 11 — BTS 에 쓰는 일을 동기화와 겹치지 않게 같은 잠금 안에서 한다."""
        with self.lock:
            return fn(*args)

    def _locked(self) -> None:
        try:
            self._run()
        finally:
            self.lock.release()

    def _run(self) -> dict:
        try:
            result = run(self.store, self.tracker)
            if self.qa_team:
                result = intake(self.store, self.tracker, self.qa_team, result)
            result = carry(self.store, self.tracker, result)
        except Exception as e:   # run 은 묶음별 실패를 모으지만, 저장 실패 같은 것도 표시에 남긴다
            result = {"synced": 0, "missing": [], "errors": [f"동기화 실패: {e}"], "milestones": []}
        if self.qa_team:   # 설계 의도 13 — 실패하면 이전 값을 그대로 둔다
            try:
                me = self.tracker.me()
                self.me, self.me_error = next((m for m in self.tracker.members(self.qa_team) if m.id == me), None), ""
            except Exception as e:
                self.me_error = str(e)
            if self.teams:   # 설계 의도 16 — 토큰 주인을 못 읽어도 구성원 표는 따로 읽는다(실패하면 이전 표를 둔다)
                try:
                    self.people = {m.id: m for team in self.teams for m in self.tracker.members(team)}
                except Exception as e:
                    self.me_error = str(e)
        self.last = {"at": datetime.now(), "result": result}
        return result

    def status(self) -> dict:
        """설계 의도 12 — {"bts": 이름, "state": never·running·ok·partial·down, "ago": "방금"·"3분", "errors": [...]}."""
        out = {"bts": getattr(self.tracker, "name", ""), "state": "never", "ago": "", "errors": []}
        if self.last:
            r = self.last["result"]
            read = r["synced"] or r["milestones"]
            minutes = (datetime.now() - self.last["at"]).total_seconds() / 60
            out.update(ago=ago(minutes) if minutes >= 1 else "방금", errors=r["errors"],
                       state="ok" if not r["errors"] else "partial" if read else "down")
        if self.lock.locked():
            out["state"] = "running"
        return out


def current(milestones: list[dict]) -> str:
    """설계 의도 9 — 현재 마일스톤 이름(없으면 빈 문자열). milestones 는 store.milestones()(출시일 순)."""
    return next((m["name"] for m in milestones if not m["released"]), "")


def ago(minutes: float) -> str:
    return f"{minutes:.0f}분" if minutes < 60 else f"{minutes / 60:.0f}시간" if minutes < 1440 else f"{minutes / 1440:.0f}일"


def team(record: dict, log: list[dict]) -> str:
    """설계 의도 7 — 지금 담당 팀."""
    return qa.team_now(record, log) or "배정 없음"


def mine(records: list[dict], tickets: list[dict], changes: dict, log: dict, me: Member, at: datetime | None = None) -> dict:
    """설계 의도 13 — {"me": 이름, "mine": 내 차례, "handed": 넘긴 이슈}, 각 줄은 {key, title, status, team,
    assignee, days, limit, late, rid, action}(rid 는 서비스가 등록한 건만, action 은 내 차례의 할 일: repro 재현 확인·sign 팀장 사인·
    retest 회귀 테스트 — 화면은 이 값으로 버튼 이름만 고른다). 오래 머문 순. tickets 는 마일스톤 티켓 전체,
    records 는 등록한 리포트(같은 키면 리포트 쪽이 이긴다), changes 는 store.ticket_changes(), log 는 store.qa_log()."""
    at = at or datetime.now().astimezone()
    rows = {t["key"]: {"key": t["key"], "title": t["title"], "status": t["status"], "assignee": t["assignee"],
                       "team": t["team"] or "팀 없음", "updated": t["updated"], "rid": None, "owner": None, "urgent": False} for t in tickets}
    for r in records:
        b = r["bts"]
        if b:
            rows[r["ticket_key"]] = {"key": r["ticket_key"], "title": r["report"].title, "status": b["status"], "assignee": b["assignee"],
                                     "team": team(r, log.get(r["id"], [])), "updated": b["updated"], "rid": r["id"],
                                     "owner": (r["assignment"] or {}).get("qa_owner"), "urgent": qa.urgent(r)}
    out = {"me": me.name, "mine": [], "handed": []}
    for key, row in rows.items():
        status, owner = row["status"], row["owner"]
        mine_ = ((status == QA_REVIEW and row["assignee"] == me.name)
                 or (status == RESOLVE and (owner["id"] == me.id if owner else row["assignee"] == me.name)))
        handed = status in (IN_DEV, CHECK) and bool(owner) and owner["id"] == me.id
        if not (mine_ or handed):
            continue
        days = metrics.stay_days(changes.get(key, []), status, row["updated"], at)
        limit = config.BOTTLENECK_DAYS[status]
        line = {"key": key, "title": row["title"], "status": status, "team": row["team"], "assignee": row["assignee"],
                "days": round(days, 1), "limit": limit, "late": days > limit, "rid": row["rid"],
                "action": "retest" if status == RESOLVE else "sign" if row["urgent"] else "repro"}
        out["mine" if mine_ else "handed"].append(line)
    for k in out:
        if k != "me":
            out[k].sort(key=lambda x: -x["days"])
    return out
