"""LLM 1차 트리아지 평가 도구 (검증 사례: 게임 라이브 서비스 버그 리포트).

v1.0 벤치마크(bench_v1) 등 측정이 끝난 단계의 코드는 지웠다. 그 코드는 태그 v1.5-archive 에 있고, 결과 요약은 보고서/v1.5까지_요약.md 에 있다.

하위 패키지
-----------
    config.py     모델·생성 옵션·파일 위치 (경로는 이 파일에서만 정한다)
    contracts.py  8칸 출력 형식·허용 값·형식 규칙
    llm.py        Ollama 호출 입구
    triage/       리포트 1건 처리 — triage() 하나로 프롬프트부터 안전장치까지
    evaluation/   평가 — 실행(triage-run), 성능표(triage-eval), 회귀 게이트(triage-gate)
"""
