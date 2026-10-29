"""v1.5 요약 품질 LLM-as-judge — 요약 품질 판정의 채점관(gemma4:12b)과 검토자 채점의 일치를 잰다.

계획(사전 등록): report/quality_v15_plan.md · 기준: quality_v15.toml · 세트: tools/quality_v15/build_set.py
채점 화면: tools/quality_v15/rater_server.py

이 단계(검토자 채점 전)에는 기준 파일을 읽는 함수만 둔다. 채점관 실행·지표·판정은 검토자 채점을 시작한 뒤, 측정 전에
같은 파일에 더해 커밋한다(계획 §9 진행 순서).

[설계 의도]
1. 기준 파일은 모르는 섹션·키나 빠진 키가 있으면 읽지 않는다(오타가 기준을 조용히 바꾸지 못하게). judge.py 와 같은 규칙.
"""

import tomllib
from pathlib import Path

from triage_eval.common.paths import ROOT
from triage_eval.pipeline.gate import GateError

CRITERIA = ROOT / "quality_v15.toml"
KNOWN = {
    "meta": {"version", "judge_model", "rater", "measure_set", "answer_key", "dev_set", "practice"},
    "natural": {"agreement_min", "kappa_min", "min_positive_for_kappa", "false_alarm_max"},
    "injected": {"recall_min"},
}


def load_criteria(path: Path = CRITERIA) -> dict:
    """설계 의도 1."""
    if not path.exists():
        raise GateError(f"기준 파일이 없습니다: {path}")
    c = tomllib.loads(path.read_text(encoding="utf-8"))
    problems = [f"모르는 섹션 [{s}]" for s in c if s not in KNOWN]
    problems += [f"[{s}] 모르는 키 {k!r}" for s, body in c.items() if s in KNOWN for k in body if k not in KNOWN[s]]
    problems += [f"[{s}] {k} 가 없습니다" for s, keys in KNOWN.items() for k in keys if k not in c.get(s, {})]
    if problems:
        raise GateError("기준 파일 오류 → " + "; ".join(problems))
    return c
