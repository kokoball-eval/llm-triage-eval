"""v1.5 확신도 기반 라우팅 — 개발용 기준선 단계. 저장된 M2 실행 기록으로 신호별·후보별 라우팅 성능을 잰다.

계획(사전 등록): report/routing_v15_plan.md · 기준: routing_v15.toml
모델을 호출하지 않는다. 입력은 저장된 기준선 실행 기록(seed 1·11·21 × 2회)과 v1.5 정답표다.

사용법
------
    uv run triage-routing              # 후보 R0~R4를 재고 선택 규칙을 적용한다
    uv run triage-routing --no-write   # 계산·출력만 하고 파일을 쓰지 않는다

종료 코드
---------
    0  완료   2  측정 불가 (입력 검증 실패, 기준 파일 오류, 계획·도구·기준 파일이 커밋되지 않음)

출력
----
    data/results/v15/routing/routing_dev_v15.json   기계 판독용 (응답별 신호, 후보별 지표, 선택 과정)
    report/routing_dev_v15.md                       사람이 읽는 비교표

[설계 의도]
1. 모델이 확신도를 출력하지 않는다(출력 8칸에 없다). 출력 형식을 바꾸면 한 번에 하나만 바꾼다는 원칙이 깨지고, 7B 모델의
   자기 보고 확신도를 믿을 근거도 없다. 그래서 응답·입력·같은 문항의 다른 응답에서 얻는 신호를 확신도 대신 쓴다.
2. 신호는 정답 라벨을 보지 않는다. 실제 운영에서는 정답을 모르기 때문이다. 같은 응답에 라벨을 바꿔 넣어도 신호가 같음을
   tests/test_routing_v15.py 가 확인한다. 정답은 신호를 평가하는 쪽(위험 누락·정확도)에서만 쓴다.
3. 채점은 score.score_one·detect 를 그대로 쓴다(score.py 설계 의도, rescore.py 설계 의도 1). 라우팅용 채점 규칙을 새로 만들면
   게이트·재채점 결과와 같은 자로 비교할 수 없다.
4. 신호는 비용이 낮은 순서(S1 → S2 → S4 → S3)로, 지금 선택한 신호 집합에 하나씩 더해 본다. 한 번에 여러 신호를 더하면 어느 신호가
   위험 누락을 줄였는지 알 수 없고, 거절한 신호가 다음 후보에 섞여 들어가면 선택 규칙이 모호해진다(독립 검토 지적).
   S3(반복 불일치)는 운영에서 같은 리포트를 여러 번 돌려야 해 비용이 가장 커서 마지막에 둔다.
5. 선택 규칙: 신호를 더한 집합으로 바꾸려면 자동 처리율이 하한 이상이고, 위험·날조 통과 응답 수가 줄거나, 같으면서 자동 처리분
   정확도가 기준 이상 올라야 한다. 비교는 반올림 전의 정수 비율(Fraction)로 한다. 반올림한 값끼리 빼면 정확히 2.0%p 차이가
   부동소수 오차로 거절될 수 있다. 규칙은 결과를 보기 전에 기준 파일에 적었다. 개발용 문항은 프롬프트를 다듬는 데 쓴 문항이라
   이 단계는 신호 집합을 고르기만 하고 채택 판정은 하지 않는다.
6. 날조 통과는 S4와 같은 탐지기로 센다. 그래서 S4가 들어간 집합에서는 정의상 0이다. 이 지표로 날조 0건을 증명할 수 없고,
   9단계의 날조 판정은 탐지기와 독립된 수단으로 해야 한다(계획 §8). 위험·날조 통과는 응답 단위로 센다. 같은 문항의 응답 6개는 서로 독립이 아니므로 응답 수가 아니라 "그런 응답이 있는 문항 수"도
   함께 남긴다.
7. 계획·도구·기준 파일이 커밋되어 있고 변경이 없을 때만 측정한다. 결과를 보고 규칙을 고치는 일을 막는 장치다. 계산은 결정적이라
   다시 돌려도 같은 결과가 나온다.
"""

import argparse
import json
import subprocess
import sys
import tomllib
from collections import defaultdict
from fractions import Fraction
from pathlib import Path

from triage_eval.common.paths import ROOT
from triage_eval.pipeline import gate_v15 as gv15
from triage_eval.pipeline import rescore as rs
from triage_eval.pipeline.contract import parse_v2
from triage_eval.pipeline.gate import GateError
from triage_eval.pipeline.score import pct, rel, score_one

CRITERIA = ROOT / "routing_v15.toml"
KNOWN = {
    "meta": {"version", "target_model", "inputs", "labels", "split", "baseline"},
    "signals": {"review_actions", "review_priorities", "agreement_fields", "expected_responses_per_item"},
    "selection": {"accuracy_fields", "accuracy_gain_min", "auto_rate_floor"},
}
RESULT_JSON = ROOT / "data" / "results" / "v15" / "routing" / "routing_dev_v15.json"
REPORT = ROOT / "report" / "routing_dev_v15.md"
GUARDED = ["src/triage_eval/pipeline/routing.py", "routing_v15.toml", "report/routing_v15_plan.md", "tests/test_routing_v15.py",
           "src/triage_eval/pipeline/score.py", "src/triage_eval/common/detect_hallucination.py",
           "src/triage_eval/pipeline/contract.py", "src/triage_eval/pipeline/prompt.py"]   # 설계 의도 7 — 계산에 쓰는 채점 코드도 포함

ORDER = ["S1", "S2", "S4", "S3"]   # 설계 의도 4 — 비용이 낮은 순서
SIGNAL_NAMES = {"S1": "판정값", "S2": "파이프라인 흔적", "S3": "반복 불일치", "S4": "날조 탐지기"}


def load_criteria(path: Path = CRITERIA) -> dict:
    if not path.exists():
        raise GateError(f"기준 파일이 없습니다: {path}")
    c = tomllib.loads(path.read_text(encoding="utf-8"))
    problems = [f"모르는 섹션 [{s}]" for s in c if s not in KNOWN]
    problems += [f"[{s}] 모르는 키 {k!r}" for s, body in c.items() if s in KNOWN for k in body if k not in KNOWN[s]]
    problems += [f"[{s}] {k} 가 없습니다" for s, keys in KNOWN.items() for k in keys if k not in c.get(s, {})]
    if problems:
        raise GateError("기준 파일 오류 → " + "; ".join(problems))
    return c


# ── 신호 (정답 라벨을 보지 않는다 — 설계 의도 2) ─────────────

def trace_reasons(record: dict, scored: dict) -> list[str]:
    """S2 — 응답에 파이프라인이 개입한 흔적."""
    reasons = []
    if not record.get("success"):
        reasons.append("실행 실패")
    if record.get("retried"):
        reasons.append("형식 재요청")
    if (record.get("guardrail") or {}).get("applied"):
        reasons.append("안전장치 적용")
    if record.get("critical_upgraded"):
        reasons.append("Critical 상향")
    if record.get("discard_held"):
        reasons.append("폐기 보류")
    if not scored["format"]["strict_pass"]:
        reasons.append("형식 위반")
    if record.get("hit_num_predict"):
        reasons.append("출력 잘림")
    return reasons


def pair_of(record: dict, fields: list[str]) -> tuple:
    pred = parse_v2(record.get("response_text", "") if record.get("success") else "")
    return tuple(pred.get(f) for f in fields)


def build_rows(records: list[dict], items: dict, crit: dict) -> list[dict]:
    """응답마다 신호(S1~S4)와 평가용 결과(위험 누락·날조·정확도 칸)를 만든다."""
    sig = crit["signals"]
    groups: dict[str, list[tuple]] = defaultdict(list)
    for r in records:
        groups[r["item_id"]].append(pair_of(r, sig["agreement_fields"]))
    bad = {i: len(v) for i, v in groups.items() if len(v) != sig["expected_responses_per_item"]}
    if bad:
        raise GateError(f"문항당 응답 수가 {sig['expected_responses_per_item']}개가 아닙니다: {bad}")
    rows = []
    for r in records:
        scored = score_one(r, items[r["item_id"]])
        pred = parse_v2(r.get("response_text", "") if r.get("success") else "")
        reasons = trace_reasons(r, scored)
        flags = {
            "S1": pred.get("처리") in sig["review_actions"] or pred.get("우선순위") in sig["review_priorities"],
            "S2": bool(reasons),
            "S3": len(set(groups[r["item_id"]])) > 1,
            "S4": scored["hallucinated"],
        }
        rows.append({"eval_id": f"s{r.get('seed')}-{r['eval_id']}", "item_id": r["item_id"], "seed": r.get("seed"), "run": r["run_index"],
                     "flags": flags, "trace": reasons,
                     "risk_miss": scored["risk_miss"], "hallucinated": scored["hallucinated"],
                     "fields_ok": {f: scored["fields"][f]["ok"] for f in crit["selection"]["accuracy_fields"]}})
    return rows


# ── 후보 평가 ────────────────────────────────────────────────

def routed(row: dict, signals: list[str]) -> bool:
    return any(row["flags"][s] for s in signals)


def is_bad(row: dict) -> bool:
    """위험 누락이거나 날조인 응답 — 검토 없이 자동 등록되면 안 되는 응답."""
    return bool(row["risk_miss"]) or row["hallucinated"]


def acc_ok(row: dict) -> bool:
    return all(row["fields_ok"].values())


def evaluate(rows: list[dict], signals: list[str]) -> dict:
    """설계 의도 6."""
    auto = [r for r in rows if not routed(r, signals)]
    review = [r for r in rows if routed(r, signals)]
    bad_auto = [r for r in auto if is_bad(r)]
    return {
        "signals": signals, "n": len(rows), "review": len(review), "auto": len(auto),
        "auto_rate": pct(len(auto), len(rows)),
        "risk_auto": {k: sum(k in r["risk_miss"] for r in auto) for k in ("X-1", "X-2", "X-3")},
        "risk_total": {k: sum(k in r["risk_miss"] for r in rows) for k in ("X-1", "X-2", "X-3")},
        "hallucinated_auto": sum(r["hallucinated"] for r in auto), "hallucinated_total": sum(r["hallucinated"] for r in rows),
        "bad_auto": len(bad_auto), "bad_total": sum(is_bad(r) for r in rows),
        "bad_auto_items": len({r["item_id"] for r in bad_auto}),
        "auto_ok": sum(acc_ok(r) for r in auto),
        "accuracy_auto": pct(sum(acc_ok(r) for r in auto), len(auto)),
        "accuracy_all": pct(sum(acc_ok(r) for r in rows), len(rows)),
        "review_precision": pct(sum(is_bad(r) or not acc_ok(r) for r in review), len(review)),
    }


def single_signal(rows: list[dict]) -> dict:
    """기록 — 신호 하나만 썼을 때 위험·날조 응답을 얼마나 검토로 보내는가."""
    bad = [r for r in rows if is_bad(r)]
    return {s: {"review": sum(r["flags"][s] for r in rows), "bad_caught": sum(r["flags"][s] for r in bad), "bad_total": len(bad)}
            for s in SIGNAL_NAMES}


def ratio(a: int, b: int) -> Fraction | None:
    return Fraction(100 * a, b) if b else None


def select(rows: list[dict], crit: dict) -> dict:
    """설계 의도 4·5 — S1에서 시작해 S2, S4, S3 순서로, 지금 선택한 집합에 하나씩 더해 볼지 정한다."""
    sel = crit["selection"]
    floor, gain = Fraction(str(sel["auto_rate_floor"])), Fraction(str(sel["accuracy_gain_min"]))
    cur = [ORDER[0]]
    cur_m = evaluate(rows, cur)
    floor_ok = ratio(cur_m["auto"], cur_m["n"]) >= floor
    steps = []
    for sig in ORDER[1:] if floor_ok else []:
        nxt = cur + [sig]
        m = evaluate(rows, nxt)
        acc_a, acc_b = ratio(cur_m["auto_ok"], cur_m["auto"]), ratio(m["auto_ok"], m["auto"])
        if ratio(m["auto"], m["n"]) < floor:
            why, take = f"자동 처리율 {m['auto_rate']}% 가 하한 {sel['auto_rate_floor']}% 미만", False
        elif m["bad_auto"] < cur_m["bad_auto"]:
            why, take = f"위험·날조 통과 {cur_m['bad_auto']} → {m['bad_auto']}", True
        elif m["bad_auto"] == cur_m["bad_auto"] and (acc_b or 0) - (acc_a or 0) >= gain:
            why, take = f"통과 같음, 자동 처리분 정확도 {cur_m['accuracy_auto']} → {m['accuracy_auto']}", True
        else:
            why, take = "위험·날조 통과가 줄지 않고 정확도 향상도 기준 미만", False
        steps.append({"signal": sig, "from": list(cur), "to": nxt, "adopted": take, "reason": why,
                      "metrics_from": cur_m, "metrics_to": m})
        if take:
            cur, cur_m = nxt, m
    return {"selected": cur, "selected_metrics": cur_m, "floor_ok": floor_ok, "steps": steps}


# ── 입력·출력 ────────────────────────────────────────────────

def load_records(paths: list[Path], crit: dict, items: dict) -> list[dict]:
    model, split = crit["meta"]["target_model"], crit["meta"]["split"]
    out = []
    for p in paths:
        log = json.loads(p.read_text(encoding="utf-8"))
        out += [r for r in log["results"] if r["model"] == model and items[r["item_id"]]["split"] == split]
    return out


def check_coverage(records: list[dict], items: dict, crit: dict) -> None:
    """개발용 문항이 모두 있고, 문항마다 (seed, 회차)가 겹치지 않는지 확인한다."""
    dev = {i for i, it in items.items() if it["split"] == crit["meta"]["split"]}
    got = {r["item_id"] for r in records}
    pairs = [(r["item_id"], r.get("seed"), r["run_index"]) for r in records]
    problems = []
    if got != dev:
        problems.append(f"개발용 문항이 맞지 않습니다 (없음 {sorted(dev - got)}, 정답표에 없음 {sorted(got - dev)})")
    if len(pairs) != len(set(pairs)):
        problems.append("같은 (문항, seed, 회차) 응답이 겹칩니다 — 기준선 파일이 중복되었는지 확인")
    if problems:
        raise GateError("; ".join(problems))


def run_analysis(crit: dict) -> dict:
    paths = [ROOT / p for p in crit["meta"]["baseline"]]
    inputs, labels = rs.load_dataset(ROOT / crit["meta"]["inputs"]), rs.load_dataset(ROOT / crit["meta"]["labels"])
    try:
        gv15.check_inputs(paths, inputs, labels)
    except Exception as e:   # GateError — 입력·정답표가 어긋나면 측정하지 않는다
        raise GateError(str(e)) from e
    items = rs.items_of(labels)
    records = load_records(paths, crit, items)
    check_coverage(records, items, crit)
    rows = build_rows(records, items, crit)
    return {"criteria": crit, "n": len(rows), "baseline": {"R0": evaluate(rows, []), "R1": evaluate(rows, [ORDER[0]])},
            "single_signal": single_signal(rows), "selection": select(rows, crit), "rows": rows}


def render(res: dict) -> str:
    sel, base = res["selection"], res["baseline"]
    name = "+".join(sel["selected"])
    L = ["# v1.5 확신도 기반 라우팅 — 개발용 기준선", "",
         "> 계획 [`routing_v15_plan.md`](routing_v15_plan.md) · 기준 `routing_v15.toml` · `src/triage_eval/pipeline/routing.py` 생성 · "
         f"저장된 M2 실행 기록 {res['n']}응답(개발용 문항 × seed 3개 × 2회), 모델 호출 없음", "",
         "개발용 문항은 프롬프트를 다듬는 데 쓴 문항이라 이 결과는 낙관적일 수 있다. 이 표는 신호 집합을 고르는 데만 쓰고 채택 판정은 새 최종 평가용 세트에서 한다. "
         "날조 통과는 S4와 같은 탐지기로 세므로 S4가 들어간 집합에서는 정의상 0이다.", "",
         f"## 선택: **{name}**", ""]
    if not sel["floor_ok"]:
        L += ["> S1만으로도 자동 처리율이 하한 미만이다. 규칙에 따라 S1을 개발용 기준으로 기록하고 더 무거운 신호는 보지 않았다.", ""]

    def line(label, m):
        ra, rt = m["risk_auto"], m["risk_total"]
        return (f"| {label} | {'+'.join(m['signals']) or '—'} | {m['auto_rate']} | {ra['X-1']}/{rt['X-1']} · {ra['X-2']}/{rt['X-2']} · "
                f"{ra['X-3']}/{rt['X-3']} | {m['hallucinated_auto']}/{m['hallucinated_total']} | {m['bad_auto']}/{m['bad_total']} "
                f"({m['bad_auto_items']}) | {m['accuracy_auto']} | {m['review_precision']} |")

    L += ["| 구성 | 신호 | 자동 처리율(%) | 위험 X-1/X-2/X-3 통과 | 날조 통과 | 위험·날조 통과 응답(문항) | 자동 처리분 정확도(%) | 검토 큐 적중(%) |",
          "| :--- | :--- | ---: | :--- | ---: | ---: | ---: | ---: |", line("R0 전부 자동", base["R0"]), line("R1", base["R1"])]
    for st in sel["steps"]:
        L.append(line(f"+{st['signal']} {'(채택)' if st['adopted'] else '(거절)'}", st["metrics_to"]))
    L += ["", "위험 통과 열은 \"검토 없이 자동 등록된 응답 / 전체 위험 응답\"이다. 자동 처리분 정확도는 우선순위·모듈·처리가 모두 허용 답 안인 응답의 "
          f"비율이고, 전체 응답 기준은 {base['R0']['accuracy_all']}%다.", "", "## 선택 과정", "",
          "| 단계 | 비교 | 결과 | 이유 |", "| :--- | :--- | :---: | :--- |"]
    L += [f"| +{s['signal']} | {'+'.join(s['from'])} → {'+'.join(s['to'])} | {'채택' if s['adopted'] else '거절'} | {s['reason']} |"
          for s in sel["steps"]]
    L += ["", "## 기록: 신호 하나만 썼을 때", "", "| 신호 | 검토로 보낸 응답 | 위험·날조 응답 중 검토로 보낸 수 |", "| :--- | ---: | ---: |"]
    L += [f"| {s} {SIGNAL_NAMES[s]} | {v['review']} | {v['bad_caught']}/{v['bad_total']} |" for s, v in res["single_signal"].items()]
    missed = [r for r in res["rows"] if not routed(r, sel["selected"]) and is_bad(r)]
    L += ["", f"## 기록: {name}에서 검토 없이 자동 등록된 위험·날조 응답 ({len(missed)}건)", ""]
    if missed:
        L += ["| 응답 | 위험 | 날조 |", "| :--- | :--- | :---: |"]
        L += [f"| {r['eval_id']} | {', '.join(r['risk_miss']) or '—'} | {'있음' if r['hallucinated'] else '—'} |" for r in missed]
    else:
        L.append("없음")
    return "\n".join(L) + "\n"


def git_lines(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return [x for x in out.stdout.decode("utf-8").splitlines() if x.strip()]


def guard() -> None:
    """설계 의도 7."""
    problems = [f"{f} 가 저장소에 커밋되어 있지 않습니다" for f in GUARDED if not git_lines("ls-files", "--", f)]
    dirty = git_lines("status", "--porcelain", "--", *GUARDED)
    if dirty:
        problems.append("커밋하지 않은 변경이 있습니다: " + ", ".join(dirty))
    if problems:
        raise GateError("; ".join(problems))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="v1.5 확신도 기반 라우팅 개발용 기준선")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        crit = load_criteria()
        guard()
        res = run_analysis(crit)
    except GateError as e:
        print(f"측정 불가: {e}")
        return 2
    if not args.no_write:   # 출력보다 먼저 저장한다 (ISSUE-014)
        RESULT_JSON.parent.mkdir(parents=True, exist_ok=True)
        RESULT_JSON.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        REPORT.write_text(render(res), encoding="utf-8")
    sel = res["selection"]
    for k, v in res["baseline"].items():
        print(f"  {k} 자동 처리율 {v['auto_rate']}% · 위험·날조 통과 {v['bad_auto']}/{v['bad_total']} · 자동 처리분 정확도 {v['accuracy_auto']}%")
    for st in sel["steps"]:
        print(f"  +{st['signal']} {'채택' if st['adopted'] else '거절'}: {st['reason']}")
    print(f"선택: {'+'.join(sel['selected'])}")
    if not args.no_write:
        print(f"저장: {rel(RESULT_JSON)}, {rel(REPORT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
