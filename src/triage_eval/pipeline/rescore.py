"""v1.4 실행 기록 v1.5 라벨 재채점 — 모델을 다시 호출하지 않고 정답표만 바꿔 다시 센다.

계획: report/rescore_v15_plan.md (재채점 결과를 보기 전에 커밋)

사용법
------
    uv run triage-rescore              # 계획 §2의 대상 전부를 재채점
    uv run triage-rescore --no-write   # 검증·출력만 하고 파일을 쓰지 않음

종료 코드
---------
    0  재채점 완료   2  도구 검증 실패(계획 §5 V0~V3) — 결과 파일을 쓰지 않는다

출력
----
    data/results/v15/rescore/rescore_v15.json   기계 판독용 (v1.4·v1.5 라벨 값, 변화 상세, 검증 결과)
    report/rescore_v15.md                       사람이 읽는 비교 표

[설계 의도]
1. 채점·집계·판정은 기존 함수(score.score_one·aggregate, gate.counts, compare.compare_seed, selection.summarize·decide)를
   그대로 쓴다. 채점 규칙을 새로 만들면 v1.4 결과와 같은 자로 비교할 수 없다. 이 파일에는 대상 고르기, 검증, 출력만 둔다.
2. 실행 기록의 dataset_version(v1.4)과 다른 라벨(v1.5)로 채점하는 것은 이 도구에서만 의도적으로 허용한다.
   score.py 는 실행 기록이 라벨을 고르게 해 실수를 막지만(score.py 설계 의도 7), 이 도구의 목적이 바로 다른 라벨로 다시 세는 것이다.
   대신 두 평가셋의 문항 ID·입력이 같은지 먼저 확인한다(V0). 입력이 같아야 "모델이 본 리포트에 v1.5 정답을 대조했다"고 말할 수 있다.
3. v1.4 라벨로 다시 채점한 값이 저장된 결과 파일과 정확히 같은지 먼저 확인한다(V1). 같은 자로 다시 쟀을 때 같은 값이 나와야
   v1.5 값과의 차이를 라벨 탓으로 볼 수 있다. 하나라도 어긋나면 결과 파일을 쓰지 않는다(종료 코드 2).
4. 두 라벨의 채점 결과가 다른 응답은 라벨이 바뀐 문항(changed_ids_v15)에만 있어야 하고, 라벨과 무관한 값(형식·날조·지연)은
   모든 응답에서 같아야 한다(V3). 어긋나면 채점 경로가 라벨이 아닌 다른 것을 읽고 있다는 뜻이다.
5. 실험 A·B는 사전 등록 기준(method_selection_v14.toml)으로 v1.5 라벨 판정을 다시 계산하지만 "참고"로만 적는다.
   사전 등록 판정(M2 채택, qwen2.5:7b 유지, 게이트 PASS)은 v1.4 라벨로 내린 것이며 이 도구가 바꾸지 않는다(계획 §6).
6. 이전 버전 파일(실행 기록·score_*.json·compare_*.json·gate_result_v14.json·report/eval_v14*.md)은 읽기만 한다.
   score.main 은 report/eval_v14.md 를 덮어쓰므로 쓰지 않고, 출력은 v15 폴더의 새 파일로만 낸다.
"""

import argparse
import json
import sys
from pathlib import Path

from triage_eval.common.paths import ROOT
from triage_eval.pipeline import compare as cmp
from triage_eval.pipeline import gate as g
from triage_eval.pipeline import gate_v14 as gv
from triage_eval.pipeline import selection as sel
from triage_eval.pipeline.score import JUDGED, aggregate, score_one

DATASET_V14 = ROOT / "data" / "eval_v14" / "aether_raid_v14.json"
DATASET_V15 = ROOT / "data" / "eval_v15" / "aether_raid_v15.json"
RESULTS_V14 = ROOT / "data" / "results" / "v14"
FINAL_LOG = RESULTS_V14 / "history" / "v14_final_m2_20261004_224554.json"
FINAL_SCORE = RESULTS_V14 / "score_v14_final_m2_20261004_224554.json"
GATE_RESULT = RESULTS_V14 / "gate_result_v14.json"
COMPARES = {"a": RESULTS_V14 / "compare_a.json", "b": RESULTS_V14 / "compare_b.json"}
OUT_JSON = ROOT / "data" / "results" / "v15" / "rescore" / "rescore_v15.json"
REPORT = ROOT / "report" / "rescore_v15.md"
NOTE_ITEM = "F08"   # 계획 §6 — 최종 평가용 라벨이 v1.5 기준서와 어긋나는 문항
LABEL_FREE = ("format", "hallucinated", "ungrounded", "elapsed_sec", "prompt_eval_count")   # 라벨과 무관한 채점 값 (V3)
SAME_ITEM = ("input", "split", "set", "track", "quality", "defect_types")   # 두 평가셋에서 같아야 하는 문항 필드 (V0)


class RescoreError(Exception):
    """도구 검증 실패 — 결과를 쓰지 않는다."""


def load_dataset(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def items_of(dataset: dict) -> dict:
    return {i["id"]: i for i in dataset["items"]}


def norm(x):
    """JSON으로 저장했다 읽은 모양으로 맞춘다 (튜플→리스트 등). 저장된 결과와 비교할 때 쓴다."""
    return json.loads(json.dumps(x, ensure_ascii=False))


# ── 검증 (계획 §5) ───────────────────────────────────────────

def check_datasets(d14: dict, d15: dict) -> list[str]:
    """V0 — 문항 ID·입력·구분이 같고, 최종 평가용 라벨이 같고, 바뀐 라벨은 changed_ids_v15 안에만 있으며,
    changed_ids_v15 의 문항은 모두 개발용이고 라벨이 실제로 바뀌었다(v1.4 파일을 v1.5로 잘못 지정한 경우를 잡는다)."""
    a, b = items_of(d14), items_of(d15)
    problems = []
    if set(a) != set(b):
        problems.append(f"문항 ID가 다릅니다 (v1.4에만 {sorted(set(a) - set(b))}, v1.5에만 {sorted(set(b) - set(a))})")
    changed = set(d15.get("changed_ids_v15", []))
    for k in sorted(set(a) & set(b)):
        problems += [f"{k} {f} 이(가) 다릅니다" for f in SAME_ITEM if a[k].get(f) != b[k].get(f)]
        if a[k]["labels"] != b[k]["labels"]:
            if a[k]["split"] == "final":
                problems.append(f"최종 평가용 {k} 라벨이 바뀌었습니다")
            if k not in changed:
                problems.append(f"{k} 라벨이 바뀌었는데 changed_ids_v15 에 없습니다")
    for k in sorted(changed):
        if k not in a or k not in b:
            problems.append(f"changed_ids_v15 의 {k} 가 평가셋에 없습니다")
        elif b[k]["split"] != "dev":
            problems.append(f"changed_ids_v15 의 {k} 가 개발용이 아닙니다")
        elif a[k]["labels"] == b[k]["labels"]:
            problems.append(f"changed_ids_v15 의 {k} 라벨이 v1.4와 같습니다")
    return problems


def score_rows(log: dict, items: dict, model: str | None = None) -> list[dict]:
    return [score_one(r, items[r["item_id"]]) for r in log["results"] if model is None or r["model"] == model]


def by_set(rows: list[dict], items: dict) -> dict:
    """score.main 과 같은 묶음 (전체·대표 세트·집중 세트)."""
    return {"전체": aggregate(rows, items),
            "대표 세트": aggregate([r for r in rows if r["set"] == "representative"], items),
            "집중 세트": aggregate([r for r in rows if r["set"] == "focused"], items)}


def without_allowed(row: dict) -> dict:
    """채점 행에서 정답표 자체(allowed)를 뺀다. 남은 값이 다르면 "채점 결과가 달라진" 응답이다."""
    return {**row, "fields": {f: {k: v for k, v in x.items() if k != "allowed"} for f, x in row["fields"].items()}}


def label_diff(rows14: list[dict], rows15: list[dict], changed: set[str],
               seeds: list[int | None] | None = None) -> tuple[dict, list[str]]:
    """V3 — (변화 상세, 문제 목록). 같은 응답을 두 라벨로 채점한 행을 순서대로 맞대어 본다.
    seeds 는 행마다의 seed다. eval_id 에는 seed가 없어, seed 여러 개를 합친 목록에서 응답을 구분하려고 함께 적는다."""
    problems = []
    fields = {f: {"fixed": 0, "broken": 0, "best_gained": 0, "best_lost": 0} for f in JUDGED}
    risk, items_changed = [], set()
    all_ok = {"fixed": 0, "broken": 0}
    over = {"added": 0, "removed": 0}
    for a, b, seed in zip(rows14, rows15, seeds or [None] * len(rows14), strict=True):
        key = a["eval_id"] if seed is None else f"seed {seed} {a['eval_id']}"
        for k in LABEL_FREE:
            if a[k] != b[k]:
                problems.append(f"{key} 라벨과 무관한 값 {k} 가 다릅니다")
        if without_allowed(a) == without_allowed(b):
            continue
        items_changed.add(a["item_id"])
        if a["item_id"] not in changed:
            problems.append(f"{key} 라벨이 바뀌지 않은 문항인데 채점 결과가 다릅니다")
        for f in JUDGED:
            x, y = a["fields"][f], b["fields"][f]
            fields[f]["fixed"] += (not x["ok"]) and y["ok"]
            fields[f]["broken"] += x["ok"] and not y["ok"]
            fields[f]["best_gained"] += (not x["best"]) and y["best"]
            fields[f]["best_lost"] += x["best"] and not y["best"]
        all_ok["fixed"] += (not a["all_fields_ok"]) and b["all_fields_ok"]
        all_ok["broken"] += a["all_fields_ok"] and not b["all_fields_ok"]
        over["added"] += (not a["over_escalation"]) and b["over_escalation"]
        over["removed"] += a["over_escalation"] and not b["over_escalation"]
        if a["risk_miss"] != b["risk_miss"]:
            risk.append({"seed": seed, "eval_id": a["eval_id"], "item_id": a["item_id"], "v14": a["risk_miss"], "v15": b["risk_miss"],
                         "predicted_처리": a["fields"]["처리"]["predicted"]})
    detail = {"items_changed": sorted(items_changed), "fields": fields, "all_fields_ok": all_ok,
              "over_escalation": over, "risk_changes": risk}
    return detail, problems


# ── 대상별 재채점 ────────────────────────────────────────────

def rescore_final(log: dict, stored: dict, i14: dict, i15: dict, changed: set[str]) -> tuple[dict, list[str]]:
    """최종 평가용 — V1(저장된 score 파일과 같음), V2(두 라벨 결과가 같음), F08 예측 기록."""
    problems, out = [], {"models": {}}
    rows_all14 = score_rows(log, i14)
    if norm(rows_all14) != stored["per_response"]:
        problems.append("V1 최종 평가용: v1.4 라벨 응답별 채점이 저장된 score 파일과 다릅니다")
    for model in log["metadata"]["models"]:
        r14, r15 = score_rows(log, i14, model), score_rows(log, i15, model)
        s14 = by_set(r14, i14)
        if norm(s14) != stored["summary"].get(model):
            problems.append(f"V1 최종 평가용 {model}: v1.4 라벨 집계가 저장된 score 파일과 다릅니다")
        if norm(r14) != norm(r15):
            problems.append(f"V2 최종 평가용 {model}: v1.4 라벨과 v1.5 라벨 채점 결과가 다릅니다")
        detail, p = label_diff(r14, r15, changed)
        problems += p
        note = [{"eval_id": r["eval_id"], "우선순위": r["fields"]["우선순위"]["predicted"],
                 "처리": r["fields"]["처리"]["predicted"], "label_우선순위": r["fields"]["우선순위"]["allowed"],
                 "label_처리": r["fields"]["처리"]["allowed"]} for r in r14 if r["item_id"] == NOTE_ITEM]
        out["models"][model] = {"v14": s14["전체"], "v15": by_set(r15, i15)["전체"], "diff": detail, "note_item": note}
    return out, problems


def rescore_gate_baseline(crit: dict, stored: dict, i14: dict, i15: dict, changed: set[str]) -> tuple[dict, list[str]]:
    """채택 구성(게이트 기준선) — V1(gate_result_v14.json 의 seed별 기준선 집계와 같음), seed별·합계 값."""
    problems, seeds = [], {}
    model = crit["meta"]["target_model"]
    stored_by_seed = {s["seeds"]["1"]: s["baseline"] for s in stored["per_seed"]}
    all14, all15, all_seeds = [], [], []
    for path in crit["meta"]["baseline"]:
        log = g.load_log(ROOT / path)
        seed = cmp.seed_of(log)
        counted = g.counts(g.score_log(log, model, i14))
        if norm(counted) != stored_by_seed.get(seed):
            problems.append(f"V1 게이트 기준선 seed {seed}: v1.4 라벨 집계가 gate_result_v14.json 과 다릅니다")
        r14, r15 = score_rows(log, i14, model), score_rows(log, i15, model)
        detail, p = label_diff(r14, r15, changed, [seed] * len(r14))
        problems += p
        seeds[seed] = {"log": path, "v14": aggregate(r14, i14), "v15": aggregate(r15, i15), "diff": detail}
        all14 += r14
        all15 += r15
        all_seeds += [seed] * len(r14)
    total_diff, _ = label_diff(all14, all15, changed, all_seeds)
    return {"model": model, "seeds": seeds, "total": {"v14": aggregate(all14, i14), "v15": aggregate(all15, i15),
                                                      "diff": total_diff}}, problems


def judge_compare(by_arm: dict, baseline: str, items: dict, crit: dict) -> dict:
    """compare.run 의 판정 부분 — 파일·화면 출력 없이 값만 돌려준다 (설계 의도 1)."""
    seeds = crit["meta"]["seeds"]
    results = {arm: [cmp.compare_seed(by_arm[baseline][s], runs[s], baseline, arm, items, crit) for s in seeds]
               for arm, runs in by_arm.items() if arm != baseline}
    summaries = {arm: sel.summarize(r) for arm, r in results.items()}
    chosen, reasons = sel.decide(summaries)
    return {"results": results, "summaries": summaries, "chosen": chosen, "reasons": reasons}


def rescore_compare(label: str, stored: dict, crit: dict, i14: dict, i15: dict,
                    changed: set[str]) -> tuple[dict, list[str]]:
    """실험 A·B — V1(저장된 compare 결과와 같음), 비교 대상별 seed 합계 값, v1.5 라벨 참고 재판정 (설계 의도 5)."""
    problems = []
    logs = [g.load_log(ROOT / p) for p in stored["runs"]]
    baseline = stored["baseline"]
    by_arm = cmp.check_runs(logs, crit, baseline)
    j14, j15 = judge_compare(by_arm, baseline, i14, crit), judge_compare(by_arm, baseline, i15, crit)
    for key in ("results", "summaries", "chosen", "reasons"):
        if norm(j14[key]) != stored[key]:
            problems.append(f"V1 실험 {label.upper()}: v1.4 라벨 {key} 가 compare_{label}.json 과 다릅니다")
    arms = {}
    for arm, runs in by_arm.items():
        model = arm.split("/", 1)[1]
        r14, r15, seeds = [], [], []
        for s in crit["meta"]["seeds"]:
            r14 += score_rows(runs[s], i14, model)
            r15 += score_rows(runs[s], i15, model)
            seeds += [s] * (len(r14) - len(seeds))
        detail, p = label_diff(r14, r15, changed, seeds)
        problems += p
        arms[arm] = {"v14": aggregate(r14, i14), "v15": aggregate(r15, i15), "diff": detail}
    reference = {"chosen_v14": j14["chosen"], "chosen_v15": j15["chosen"],
                 "reasons_v14": j14["reasons"], "reasons_v15": j15["reasons"],
                 "summaries_v15": j15["summaries"],
                 "per_seed_v15": {arm: [{"seed": r["seeds"]["1"], "passed": r["passed"],
                                         "failed_checks": [c for c in r["checks"] if not c["passed"]]} for r in per]
                                  for arm, per in j15["results"].items()}}
    return {"baseline": baseline, "runs": stored["runs"], "arms": arms, "reference_rejudge": reference}, problems


# ── 출력 ─────────────────────────────────────────────────────

def _pair(a, b) -> str:
    if a is None or b is None:
        return f"{a} → {b}"
    d = b - a
    if a == b:
        return f"{a} (같음)"
    return f"{a} → {b} ({d:+.1f})" if isinstance(d, float) else f"{a} → {b} ({d:+d})"


def _metric_rows(v14: dict, v15: dict) -> list[str]:
    L = []
    for f in JUDGED:
        L.append(f"| {f} 정답률 허용 (%) | {_pair(v14['field_ok_rate'][f], v15['field_ok_rate'][f])} |")
        L.append(f"| {f} 정답률 최선 (%) | {_pair(v14['field_best_rate'][f], v15['field_best_rate'][f])} |")
    L.append(f"| 전 칸 정답 (%) | {_pair(v14['all_fields_ok_rate'], v15['all_fields_ok_rate'])} |")
    for k in ("X-1", "X-2", "X-3"):
        L.append(f"| {k} 누락 / 측정 응답 | {v14['risk_miss'][k]} / {v14['risk_items'][k]} → "
                 f"{v15['risk_miss'][k]} / {v15['risk_items'][k]} |")
    L.append(f"| 과잉 상신 | {_pair(v14['over_escalation'], v15['over_escalation'])} |")
    return L


def _metric_table(title: str, v14: dict, v15: dict) -> list[str]:
    return [f"**{title}** (응답 {v14['n']}건)", "", "| 지표 | v1.4 라벨 → v1.5 라벨 |", "| :--- | :--- |",
            *_metric_rows(v14, v15), ""]


def _diff_lines(d: dict) -> list[str]:
    L = ["| 칸 | 오답→정답 | 정답→오답 | 최선 답 일치 얻음 | 최선 답 일치 잃음 |", "| :--- | ---: | ---: | ---: | ---: |"]
    L += [f"| {f} | {v['fixed']} | {v['broken']} | {v['best_gained']} | {v['best_lost']} |" for f, v in d["fields"].items()]
    L += ["", f"- 채점이 달라진 문항: {', '.join(d['items_changed']) or '없음'}",
          f"- 전 칸 정답 응답: 오답→정답 {d['all_fields_ok']['fixed']}, 정답→오답 {d['all_fields_ok']['broken']}",
          f"- 과잉 상신 응답: 새로 생김 {d['over_escalation']['added']}, 없어짐 {d['over_escalation']['removed']}"]
    if d["risk_changes"]:
        L += ["", "| seed | 응답 | v1.4 위험 건 | v1.5 위험 건 | 예측 처리 |", "| ---: | :--- | :--- | :--- | :--- |"]
        L += [f"| {'—' if r['seed'] is None else r['seed']} | {r['eval_id']} | {', '.join(r['v14']) or '—'} | {', '.join(r['v15']) or '—'} | {r['predicted_처리']} |"
              for r in d["risk_changes"]]
    return L + [""]


def render(result: dict) -> str:
    gb = result["adopted"]
    L = ["# v1.4 실행 기록 v1.5 라벨 재채점 결과", "",
         "> 계획 [`report/rescore_v15_plan.md`](rescore_v15_plan.md) · `src/triage_eval/pipeline/rescore.py` 생성 · "
         "모델은 다시 호출하지 않았다. 모델 응답은 같고 정답표만 v1.4 → v1.5로 바뀌었다.", "",
         "도구 검증(계획 §5 V0~V3): 모두 통과 — v1.4 라벨로 다시 채점한 값이 저장된 결과와 같고, "
         "채점이 달라진 응답은 모두 라벨이 바뀐 문항에 있다.", "",
         f"## 1. 채택 구성 (게이트 기준선, `{gb['model']}` + M2, 개발용)", ""]
    L += _metric_table("seed 1·11·21 합계", gb["total"]["v14"], gb["total"]["v15"])
    L += ["**라벨 변경으로 달라진 응답 (seed 3개 합계)**", ""] + _diff_lines(gb["total"]["diff"])
    L += ["**seed별 주요 지표** (v1.4 라벨 → v1.5 라벨, 전 지표는 `data/results/v15/rescore/rescore_v15.json`)", "",
          "| seed | 우선순위 허용 (%) | 처리 허용 (%) | 전 칸 정답 (%) | X-1 누락 / 측정 응답 | 과잉 상신 |",
          "| ---: | :--- | :--- | :--- | :--- | :--- |"]
    L += [f"| {s} | {_pair(v['v14']['field_ok_rate']['우선순위'], v['v15']['field_ok_rate']['우선순위'])} | "
          f"{_pair(v['v14']['field_ok_rate']['처리'], v['v15']['field_ok_rate']['처리'])} | "
          f"{_pair(v['v14']['all_fields_ok_rate'], v['v15']['all_fields_ok_rate'])} | "
          f"{v['v14']['risk_miss']['X-1']} / {v['v14']['risk_items']['X-1']} → "
          f"{v['v15']['risk_miss']['X-1']} / {v['v15']['risk_items']['X-1']} | "
          f"{_pair(v['v14']['over_escalation'], v['v15']['over_escalation'])} |" for s, v in gb["seeds"].items()]
    L.append("")
    for n, (label, title) in enumerate((("a", "실험 A (방법 비교)"), ("b", "실험 B (모델 비교)")), start=2):
        e = result["experiments"][label]
        ref = e["reference_rejudge"]
        L += [f"## {n}. {title}", "", f"기준 `{e['baseline']}`, seed 3개 합계.", ""]
        for arm, v in e["arms"].items():
            L += _metric_table(f"`{arm}`", v["v14"], v["v15"])
        L += ["**참고 재판정** — `method_selection_v14.toml` 기준을 그대로 써서 v1.5 라벨로 다시 계산한 값이다. "
              "사전 등록 판정은 v1.4 라벨 판정이며 바꾸지 않는다(계획 §6).", "",
              "| 후보 | v1.4 라벨 판정 (사전 등록) | v1.5 라벨 판정 (참고) |", "| :--- | :--- | :--- |"]
        L += [f"| `{arm}` | {ref['reasons_v14'][arm]} | {ref['reasons_v15'][arm]} |" for arm in ref["reasons_v14"]]
        L += ["", f"- 결정: v1.4 라벨 **{ref['chosen_v14'] or '기준 유지'}** / v1.5 라벨(참고) **{ref['chosen_v15'] or '기준 유지'}**"]
        fails = [(arm, s) for arm, per in ref["per_seed_v15"].items() for s in per if s["failed_checks"]]
        if fails:
            L += ["", "| 후보 | seed | v1.5 라벨에서 못 넘은 기준 | 기준 | 후보 | 허용 |", "| :--- | ---: | :--- | ---: | ---: | :--- |"]
            L += [f"| `{arm}` | {s['seed']} | {c['name']} | {'—' if c['baseline'] is None else c['baseline']} | "
                  f"{'—' if c['candidate'] is None else c['candidate']} | {c['limit']} |" for arm, s in fails for c in s["failed_checks"]]
        L.append("")
    L += ["## 4. 최종 평가용", ""]
    for model, v in result["final"]["models"].items():
        same = "같다" if v["v14"] == v["v15"] else "다르다"
        L += [f"`{model}` — 최종 평가용 25건은 라벨을 바꾸지 않았으므로 v1.4 라벨과 v1.5 라벨 채점 결과가 {same}"
              f" (전 칸 정답 {v['v14']['all_fields_ok_rate']}%, X-1 누락 {v['v14']['risk_miss']['X-1']} / "
              f"{v['v14']['risk_items']['X-1']}).", ""]
        if v["note_item"]:
            L += [f"**{NOTE_ITEM} 주의** — v1.4 라벨은 Major·등록(재현 대기)이지만, v1.5 기준서의 앱 재실행 규칙으로는 "
                  "Critical·긴급 사인 요청에 해당한다. 이미 사용한 세트라 라벨을 고치지 않았으므로 다시 채점하지 않고, "
                  "모델 응답만 사실로 적는다.", "",
                  "| 응답 | 예측 우선순위 | 예측 처리 | v1.4 라벨 우선순위 | v1.4 라벨 처리 |", "| :--- | :--- | :--- | :--- | :--- |"]
            L += [f"| {r['eval_id']} | {r['우선순위']} | {r['처리']} | {' / '.join(r['label_우선순위'])} | "
                  f"{' / '.join(r['label_처리'])} |" for r in v["note_item"]]
            L.append("")
    return "\n".join(L) + "\n"


# ── 실행 ─────────────────────────────────────────────────────

def run(write: bool = True) -> int:
    d14, d15 = load_dataset(DATASET_V14), load_dataset(DATASET_V15)
    problems = ["V0 " + p for p in check_datasets(d14, d15)]
    if problems:
        raise RescoreError("; ".join(problems))
    i14, i15, changed = items_of(d14), items_of(d15), set(d15.get("changed_ids_v15", []))

    gate_crit = gv.load_criteria(gv.CRITERIA)
    adopted, p = rescore_gate_baseline(gate_crit, g.load_log(GATE_RESULT), i14, i15, changed)
    problems += p
    method_crit = cmp.load_criteria(cmp.CRITERIA)
    experiments = {}
    for label, path in COMPARES.items():
        experiments[label], p = rescore_compare(label, g.load_log(path), method_crit, i14, i15, changed)
        problems += p
    final, p = rescore_final(g.load_log(FINAL_LOG), g.load_log(FINAL_SCORE), i14, i15, changed)
    problems += p
    if problems:
        raise RescoreError("; ".join(problems))

    result = {"plan": "report/rescore_v15_plan.md", "labels": {"v14": g.rel(DATASET_V14), "v15": g.rel(DATASET_V15)},
              "verification": "V0~V3 통과", "adopted": adopted, "experiments": experiments, "final": final}
    t = adopted["total"]
    print(f"[채택 구성 seed 합계] 전 칸 정답 {t['v14']['all_fields_ok_rate']} → {t['v15']['all_fields_ok_rate']}% · "
          f"X-1 누락 {t['v14']['risk_miss']['X-1']}/{t['v14']['risk_items']['X-1']} → "
          f"{t['v15']['risk_miss']['X-1']}/{t['v15']['risk_items']['X-1']}")
    for label, e in experiments.items():
        ref = e["reference_rejudge"]
        print(f"[실험 {label.upper()}] 결정 v1.4 {ref['chosen_v14'] or '기준 유지'} / v1.5(참고) {ref['chosen_v15'] or '기준 유지'}")
    if write:
        text = render(result)   # 보고서를 먼저 만들어, 만들다 실패하면 두 파일 모두 쓰지 않는다
        OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
        OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORT.write_text(text, encoding="utf-8")
        print(f"저장: {g.rel(OUT_JSON)}, {g.rel(REPORT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="v1.4 실행 기록을 v1.5 라벨로 재채점 (모델 호출 없음)")
    p.add_argument("--no-write", action="store_true", help="결과 파일을 쓰지 않음")
    args = p.parse_args(argv)
    try:
        return run(write=not args.no_write)
    except (RescoreError, g.GateError) as e:
        print(f"도구 검증 실패 — 결과를 쓰지 않습니다: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
