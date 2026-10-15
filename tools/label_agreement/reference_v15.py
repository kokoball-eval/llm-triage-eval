"""v1.5 라벨 일치율 — 채점자끼리 갈린 문항과 v1.5 라벨 기준 참고 일치율.

계획: report/label_agreement_v15_plan.md §5 · 결과 보고서: report/label_agreement_v15_results.md

사용법
------
    uv run python tools/label_agreement/reference_v15.py              # data/label_agreement/reference_v15.json
    uv run python tools/label_agreement/reference_v15.py --no-write

종료 코드
---------
    0  계산 완료   2  검증 실패 (저장된 판정 결과를 재현하지 못함, 대응표·평가셋 불일치) — 파일을 쓰지 않는다

[설계 의도]
1. 채점자 답은 판정 시트가 아니라 저장된 판정 결과(data/label_agreement/agreement_v15.json)의 answers 에서 읽는다.
   시트를 읽고 검사한 것은 triage-agreement 가 이미 했고, 같은 답을 다시 읽어 결과가 달라질 여지를 없앤다.
2. 계산 전에 v1.4 라벨로 다시 계산한 채점자 통계·채점자끼리 κ가 저장된 판정 결과와 같은지 확인한다. 같은 자로 다시 쟀을 때
   같은 값이 나와야 v1.5 라벨 값과의 차이를 라벨 탓으로 볼 수 있다(재채점 도구 rescore.py 와 같은 원칙).
3. 채점자끼리 갈린 문항(계획 §5 "새 경계 사례 후보 → 라우팅의 사람 검토 근거")은 정답 라벨과 무관하게 두 채점자의 답만 비교한다.
   모듈은 고른 모듈의 집합으로 비교한다(모듈1·모듈2 순서가 달라도 같은 판단). 비용이 큰 칸(분류·우선순위·처리)이 갈린 문항을
   따로 모은다. 게이트가 하락을 0~2응답으로 엄하게 보는 칸이고, 라우팅에서 사람이 봐야 할 판단이다.
   갈린 비용 큰 칸은 v1.5 허용 답으로 다시 나눈다. 두 답이 모두 허용 답 안이면 "원래 갈리는 판단"(검토에서 둘 다 인정),
   적어도 한쪽이 밖이면 "기준 적용 차이"(기준서대로면 적어도 한쪽이 틀림)다. v1.5 허용 답은 이 답들을 보고 넓혔으므로
   이 나눔은 독립된 근거가 아니라 검토자 판단을 다시 보여 주는 것이다(설계 의도 4와 같은 이유).
4. v1.5 라벨 기준 일치율은 참고 값이다. 불일치 검토에서 채점자 답을 보고 허용 답을 넓혔으므로 독립된 측정이 아니다
   (docs/dataset/review_log.md "판정 수집 과정의 기록"). 사전 등록 판정을 대신하지 않으며, 판정 함수(agreement.judge)를 부르지 않는다.
5. 모델 응답은 읽지 않는다. 라벨 검토 자료와 같은 원칙이다(계획 §5).
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from triage_eval.common.paths import ROOT
from triage_eval.pipeline import agreement as ag
from triage_eval.pipeline.contract import split_modules
from triage_eval.pipeline.gate import GateError
from triage_eval.pipeline.score import JUDGED, field_correct, rel

AGREEMENT = ROOT / "data" / "label_agreement" / "agreement_v15.json"
DATASET_V15 = ROOT / "data" / "eval_v15" / "aether_raid_v15.json"
OUT = ROOT / "data" / "label_agreement" / "reference_v15.json"
COSTLY = ("분류", "우선순위", "처리")   # 설계 의도 3


def same_answer(field: str, a: str, b: str) -> bool:
    if field == "모듈":
        return set(split_modules(a)) == set(split_modules(b))
    return a == b


def between_raters(answers: dict[str, dict], items14: dict, items15: dict, qmap: dict[str, str]) -> dict:
    """설계 의도 3 — 두 채점자의 답이 다른 (문항, 칸)과 문항별 묶음."""
    r1, r2 = list(answers)
    per_item = []
    for q in sorted(items14):
        diff = [f for f in JUDGED if not same_answer(f, answers[r1][q][f], answers[r2][q][f])]
        if not diff:
            continue
        it14, it15 = items14[q], items15[q]
        per_item.append({
            "번호": q, "문항": qmap[q], "quality": it14.get("quality"), "set": it14["set"],
            "risk_v15": it15["labels"]["risk"], "갈린 칸": diff, "비용 큰 칸": [f for f in diff if f in COSTLY],
            "답": {f: {r1: answers[r1][q][f], r2: answers[r2][q][f]} for f in diff},
            "확신도": {r1: answers[r1][q]["확신도"], r2: answers[r2][q]["확신도"]},
            "v15 허용 답": {f: it15["labels"][f] for f in diff},
            "원래 갈리는 비용 큰 칸": [f for f in diff if f in COSTLY
                                   and all(field_correct(f, answers[r][q][f], it15["labels"][f])[0] for r in (r1, r2))],
        })
    field_counts = {f: sum(f in x["갈린 칸"] for x in per_item) for f in JUDGED}
    costly = [x for x in per_item if x["비용 큰 칸"]]
    return {
        "n_items": len(items14), "items_with_any_split": len(per_item), "field_counts": field_counts,
        "costly_items": [x["번호"] for x in costly],
        "costly_by_set": dict(Counter(x["set"] for x in costly)),
        "costly_with_x1": [x["번호"] for x in costly if "X-1" in x["risk_v15"]],
        "costly_both_allowed_items": [x["번호"] for x in per_item if x["원래 갈리는 비용 큰 칸"]],
        "costly_both_allowed_v15": {f: [x["번호"] for x in per_item if f in x["원래 갈리는 비용 큰 칸"]] for f in COSTLY},
        "costly_rule_gap_v15": {f: [x["번호"] for x in per_item
                                    if f in x["비용 큰 칸"] and f not in x["원래 갈리는 비용 큰 칸"]] for f in COSTLY},
        "split_count_histogram": dict(sorted(Counter(len(x["갈린 칸"]) for x in per_item).items())),
        "items": per_item,
    }


def run(write: bool = True) -> int:
    crit = ag.load_criteria()
    stored = json.loads(AGREEMENT.read_text(encoding="utf-8"))
    items14, qmap = ag.load_items(crit)            # 대응표 지문 = v1.4 평가셋 지문 확인 (agreement 설계 의도 6)
    by_id15 = {i["id"]: i for i in json.loads(DATASET_V15.read_text(encoding="utf-8"))["items"]}
    missing = sorted(i for i in qmap.values() if i not in by_id15)
    if missing:
        raise GateError(f"v1.5 평가셋에 대응표의 문항이 없습니다: {missing}")
    items15 = {q: by_id15[i] for q, i in qmap.items()}
    answers = stored["answers"]
    raters = crit["meta"]["raters"]
    problems = []
    if list(answers) != raters:
        problems.append(f"판정 결과의 채점자 순서 {list(answers)} 가 기준 파일 {raters} 과 다릅니다")
    for r in answers:
        if sorted(answers[r]) != sorted(items14):
            problems.append(f"{r} 답의 문항 번호가 대응표와 다릅니다")
    if any(items14[q]["input"] != items15[q]["input"] for q in items14):
        problems.append("v1.5 평가셋의 입력이 v1.4와 다릅니다")
    if problems:
        raise GateError("; ".join(problems))
    qs = sorted(items14)
    stats14 = {r: ag.rater_stats(a, items14) for r, a in answers.items()}
    kappa14 = {f: ag.cohen_kappa(*[[ag.kappa_value(answers[r][q], f) for q in qs] for r in raters]) for f in JUDGED}
    if stats14 != stored["stats"] or kappa14 != stored["between_raters_kappa"]:   # 설계 의도 2
        raise GateError("v1.4 라벨로 다시 계산한 값이 저장된 판정 결과(agreement_v15.json)와 다릅니다")

    stats15 = {r: ag.rater_stats(a, items15) for r, a in answers.items()}   # 설계 의도 4 — 참고 값
    result = {
        "source": rel(AGREEMENT), "labels_v15": rel(DATASET_V15),
        "verification": "v1.4 라벨 재계산 = 저장된 판정 결과",
        "between_raters": between_raters(answers, items14, items15, qmap),
        "reference_v15": {"note": "불일치 검토에서 채점자 답을 보고 허용 답을 넓혔으므로 독립된 측정이 아닌 참고 값",
                          "stats_v14": stats14, "stats_v15": stats15},
    }
    b = result["between_raters"]
    print(f"채점자끼리 갈린 문항 {b['items_with_any_split']}/{b['n_items']} · 비용 큰 칸(분류·우선순위·처리)이 갈린 문항 "
          f"{len(b['costly_items'])} (그중 두 답이 모두 v1.5 허용 답 안 {len(b['costly_both_allowed_items'])}) · 칸별 {b['field_counts']}")
    for r in raters:
        print(f"[참고] {r} 허용 답 일치율 v1.4 → v1.5: " + ", ".join(
            f"{f} {stats14[r][f]['allowed_rate']}→{stats15[r][f]['allowed_rate']}" for f in JUDGED)
              + f", Critical {stats14[r]['critical']['hit']}/{stats14[r]['critical']['n']}→"
                f"{stats15[r]['critical']['hit']}/{stats15[r]['critical']['n']}")
    if write:
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"저장: {rel(OUT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="채점자끼리 갈린 문항과 v1.5 라벨 기준 참고 일치율")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    try:
        return run(write=not args.no_write)
    except GateError as e:
        print(f"검증 실패 — 파일을 쓰지 않습니다: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
