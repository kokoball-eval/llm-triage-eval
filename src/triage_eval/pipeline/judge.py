"""v1.5 LLM-as-judge — 요약 날조 판정의 채점관(gemma4:12b)과 검토자 채점의 일치를 잰다.

계획(사전 등록): report/judge_v15_plan.md · 기준: judge_v15.toml

이 단계에서는 기준 파일 읽기만 둔다. 채점관 실행과 일치 판정은 계획 §5 절차(개발용 자료로 프롬프트 확정)를 거쳐
이 파일에 더한다. 측정 세트·대응표는 tools/judge_v15/build_set.py 가 만든다.

[설계 의도]
1. 기준 파일에 모르는 섹션·키가 있거나 필요한 키가 없으면 읽지 않는다(v1.1.1 ISSUE-003). 오타로 기준이 조용히 꺼진 채
   채택 판정이 나는 것을 막는다.
"""

import tomllib
from pathlib import Path

from triage_eval.common.paths import ROOT
from triage_eval.pipeline.gate import GateError

CRITERIA = ROOT / "judge_v15.toml"
KNOWN = {
    "meta": {"version", "judge_model", "rater", "measure_set", "answer_key", "dev_set"},
    "natural": {"agreement_min", "kappa_min", "min_positive_for_kappa", "false_alarm_max"},
    "injected": {"recall_min", "credit_requires_key"},
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
