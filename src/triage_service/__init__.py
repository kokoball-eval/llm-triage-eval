"""트리아지 서비스 — 리포트를 받아 트리아지하고, 사람이 확인·수정한 뒤 BTS에 등록한다.

트리아지는 평가 패키지(triage_eval)가 측정한 구성(triage_eval.triage.pipeline.triage)을 그대로 호출한다.
서비스는 그 앞뒤(접수·상태 흐름·검토·등록·화면)만 맡는다. 설계: 문서/서비스_설계.md

하위 모듈
---------
    contracts.py  영역 사이에 주고받는 타입(Report·Issue·Ticket)과 출처·BTS 계약(Source·Tracker)
    config.py     서비스 설정 (DB 위치, 사용할 BTS, 접속 주소)
    intake/       출처 — 지금은 본문 붙여넣기만
    routing.py    자동 등록 / 검토 결정 (지금은 항상 검토)
    flow.py       상태와 허용 전이
    store.py      SQLite 저장소
    worker.py     트리아지 작업자 (한 번에 한 건)
    bts/          BTS 어댑터 — 지금은 가짜 BTS만
    web/          FastAPI 화면 (uv run triage-serve)
"""
