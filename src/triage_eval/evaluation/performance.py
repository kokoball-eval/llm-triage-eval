"""성능표 — 실행 기록을 정답 라벨로 채점해 "얼마나 잘 하는가"를 한 표로 보여 준다.

사용법:
    uv run triage-eval --log <실행 기록.json> [--log ...]                    # 개발용 정답표(기본)로 채점
    uv run triage-eval --log <실행 기록.json> --labels <평가셋.json>          # 정답이 든 다른 평가셋으로 채점
    uv run triage-eval --log ... --out <결과.json>                           # 응답별 채점과 요약을 파일로 저장

[설계 의도]
1. 지표 정의는 score.py 한 곳에 있다. 이 파일은 묶어서 보여 주기만 한다. 회귀 게이트와 성능표가 같은 채점을 쓴다.
2. 위험 건(X-1·X-2·X-3)은 비율이 아니라 "놓친 건수"로 보여 준다. 출시 전 검수에서 결함 누락(escape)을 세는 방식과 같다.
3. 정답표는 실행 기록의 문항 ID 로 찾는다. 정답표에 없는 문항이 있으면 채점하지 않는다(다른 평가셋 라벨로 채점하는 실수 방지).
"""

import argparse
import json
import sys
from pathlib import Path

from triage_eval.config import DEV_LABELS, rel
from triage_eval.evaluation.score import JUDGED, pct, score_one, summarize


def score_logs(log_paths: list[Path], labels_path: Path) -> tuple[list[dict], dict]:
    items = {i["id"]: i for i in json.loads(Path(labels_path).read_text(encoding="utf-8"))["items"]}
    rows = []
    for path in log_paths:
        for rec in json.loads(Path(path).read_text(encoding="utf-8"))["results"]:
            if rec["item_id"] not in items:
                raise SystemExit(f"{rel(path)} 의 문항 {rec['item_id']} 가 정답표 {rel(labels_path)} 에 없습니다 (설계 의도 3)")
            rows.append({"log": rel(path), **score_one(rec, items[rec["item_id"]])})
    return rows, summarize(rows, items)


def render(s: dict) -> str:
    n = s["n"]
    if not n:
        return "채점할 응답이 없습니다."
    L = [f"응답 {n}건 (호출 성공 {s['success']}, 형식 완전 준수 {s['strict']} = {pct(s['strict'], n)}%)", "",
         f"{'칸':<10}{'정답(허용 답)':>14}{'최선 답':>10}"]
    for f in JUDGED:
        L.append(f"{f:<10}{pct(s['field_ok'][f], n):>13}%{pct(s['field_best'][f], n):>9}%")
    L.append(f"{'6칸 모두':<10}{pct(s['all_fields_ok'], n):>13}%")
    L += ["", "위험 건 — 놓친 건수 / 해당 응답 수"]
    names = {"X-1": "Critical 미상신", "X-2": "결함을 폐기", "X-3": "사람 확인 없이 종결"}
    L += [f"  {k} {names[k]:<14} {s['risk_miss'][k]} / {s['risk_total'][k]}" for k in ("X-1", "X-2", "X-3")]
    L += ["", f"과잉 상신 {s['over_escalation']}건 · 날조(규칙 탐지) {s['hallucinated']}건 · "
              f"요청·권장 반영 {s['required_coverage_pct']}% · 평균 지연 {s['latency_sec_mean']}초"]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="실행 기록 채점 — 성능표")
    p.add_argument("--log", type=Path, action="append", required=True)
    p.add_argument("--labels", type=Path, default=DEV_LABELS, help="정답이 든 평가셋 (기본: 개발용 정답표)")
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args(argv)
    rows, summary = score_logs(args.log, args.labels)
    print(render(summary))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"labels": rel(args.labels), "logs": [rel(p) for p in args.log],
                                        "summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {rel(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
