"""트리아지 작업자 — 접수된 리포트를 한 번에 한 건씩 측정한 파이프라인(triage)에 넣는다.

[설계 의도]
1. 작업자는 하나이고 차례대로 처리한다. 8GB GPU 에 모델 하나를 올려 쓰므로 동시에 여러 건을 부르면 느려지고,
   실행 조건(동시 요청)도 측정 때와 달라진다. 화면은 접수만 하고 바로 돌아오며, 상세 화면이 상태를 다시 읽는다.
2. 트리아지는 triage_eval.triage.pipeline.triage() 를 그대로 호출한다. 프롬프트·체크리스트·안전장치를 서비스에서
   고치지 않는다. 처리 구성 지문(run_config)을 결과와 함께 저장해, 어떤 구성이 낸 판정인지 남긴다.
3. 실패는 숨기지 않는다. 모델 호출 실패(success=False)나 예외는 "실패" 상태와 오류 내용으로 남기고 다음 건으로 넘어간다.
4. 서버가 처리 도중 꺼지면 "접수"·"트리아지 중"에 멈춘 건이 남는다. 다시 켤 때 이런 건을 "실패"(중단됨)로 바꿔,
   사람이 [다시 시도]로 처리하게 한다. 조용히 다시 돌리지 않는 것은 중단 원인을 사람이 먼저 보게 하기 위해서다.
5. 작업자 스레드는 죽으면 안 된다. 상태 기록(DB 잠김 등)까지 실패해도 그 건만 실패로 남기거나 기록을 남기고 다음 건으로
   넘어간다. 스레드가 멈추면 뒤에 들어온 리포트가 모두 "접수"에 쌓인 채 아무 표시 없이 멈추기 때문이다.
"""

import queue
import threading

from triage_eval import llm
from triage_eval.contracts import check_format, parse_response
from triage_eval.triage.pipeline import run_config, triage
from triage_service import flow, routing
from triage_service.store import Store

INTERRUPTED = "서버가 처리 도중 다시 시작되어 중단됨 — [다시 시도]로 다시 처리하세요"


class Worker:
    def __init__(self, store: Store, client_factory=llm.client):
        self.store = store
        self.client_factory = client_factory
        self.client = None
        self.jobs: queue.Queue[int] = queue.Queue()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.recover()
        self.thread = threading.Thread(target=self._loop, name="triage-worker", daemon=True)
        self.thread.start()

    def submit(self, rid: int) -> None:
        self.jobs.put(rid)

    def _loop(self) -> None:
        """설계 의도 5 — 한 건에서 어떤 오류가 나도 작업자는 다음 건으로 넘어간다."""
        while True:
            rid = self.jobs.get()
            try:
                self.process(rid)
            except Exception as e:
                print(f"[작업자] 리포트 {rid} 처리 중 기록 실패: {e!r}")

    def recover(self) -> None:
        """설계 의도 4 — 처리 중에 멈춘 건을 실패로 바꾼다."""
        for state in flow.PENDING:
            for rec in self.store.list(state):
                self.store.move(rec["id"], flow.FAILED, failed_step="트리아지", error=INTERRUPTED)

    def process(self, rid: int) -> None:
        """한 건을 트리아지하고 검토 대기로 보낸다. 실패하면 실패 상태로 남긴다 (설계 의도 2·3)."""
        try:
            rec = self.store.move(rid, flow.TRIAGING)
            if self.client is None:
                self.client = self.client_factory()
            result = triage(self.client, rec["report"].to_item())
            if not result.get("success"):
                self.store.move(rid, flow.FAILED, result=result, failed_step="트리아지",
                                error=f"모델 호출 실패: {result.get('error_message')}")
                return
            text = result["response_text"]
            self.store.move(rid, flow.REVIEW, result={**result, "format": check_format(text)}, pipeline=run_config(),
                            routing=routing.decide(result), model_fields=parse_response(text))
        except Exception as e:   # 설계 의도 3
            current = self.store.get(rid)
            if current and current["state"] in flow.PENDING:
                self.store.move(rid, flow.FAILED, failed_step="트리아지", error=f"트리아지 중 오류: {e}")
