"""회귀 게이트 — 프롬프트·규칙·모델을 바꾼 후보가 채택 구성(기준선)보다 나빠졌는지 seed 3개로 판정한다.

사용법:
    uv run triage-run --seed 1 --strict-env      # seed 11, 21 도 같은 방법으로
    uv run triage-gate --candidate <seed1 기록> --candidate <seed11 기록> --candidate <seed21 기록>

판정: seed 3개 모두 통과해야 PASS(종료 코드 0), 하나라도 기준을 넘으면 FAIL(1), 판정할 수 없으면 2.
기준 값은 합격 기준 파일(config.GATE_CRITERIA)에 있다.

[설계 의도]
1. seed 하나로는 효과와 우연을 구분할 수 없어서 seed 3개(각 63문항 × 2회 = 126응답)가 모두 통과해야 PASS다.
2. 기준은 세 종류다. 절대 기준(호출 성공률, 날조 0, 출력 언어·허용 값 위반 0, 평균 지연 상한)은 후보 단독으로 지킨다.
   기준선 대비 기준(X-2·X-3 누락이 늘지 않음)은 기준선도 0건이 아니어서 "늘지 않을 것"으로 본다.
   회귀 기준(X-1 증가 0, 과잉 상신·형식·칸 정답 하락 허용폭, 문항 단위로 새로 틀린 응답 수)은 같은 seed 기준선과 비교한다.
3. 칸 정답 수가 같아도 문항이 바뀌어 맞고 틀릴 수 있다. 그래서 칸마다 "기준선 정답 → 후보 오답" 응답을 따로 센다.
4. 비교할 수 없는 실행은 판정하지 않는다(종료 코드 2): 평가셋 지문·문항·생성 옵션·반복 횟수·모델 digest가 기준선과 다르거나,
   seed 구성이 기준 파일과 다르거나, 실행 도중 모델이 다시 로드된 응답(로드 1초 초과)이 있으면 그 뒤 응답은 같은 seed로
   재현되지 않으므로 측정 환경 오염으로 본다. 판정하려는 변경(프롬프트·방법·안전장치·정책)은 막지 않고 알려만 준다.
5. 기준 파일에 모르는 키가 있거나 필요한 키가 빠지면 판정하지 않는다. 오타 난 기준이 조용히 빠진 채 PASS 되는 일을 막는다.
6. 정답표가 기준 파일이 정한 판(meta.labels)인지 확인하고, 결과에 정답표의 판·지문을 남긴다. 실행 입력은 같고 라벨만 다른
   옛 판 정답표로 채점하는 실수는 판정 결과만 봐서는 드러나지 않기 때문이다.
"""

import argparse
import hashlib
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path
from statistics import mean

from triage_eval.config import DEV_INPUTS, DEV_LABELS, GATE_CRITERIA, GATE_DIR, ROOT, rel
from triage_eval.evaluation.score import JUDGED, score_one

FIELD_KEYS = JUDGED + ["6칸 정답"]
RELOAD_SEC = 1.0
KNOWN = {
    "meta": {"version", "dataset", "labels", "split", "seeds", "repeat", "target_model", "baseline"},
    "absolute": {"success_rate_min", "hallucination_max", "language_violation_max", "enum_violation_max",
                 "latency_max_sec"},
    "relative_to_baseline": {"x2_miss_increase_max", "x3_miss_increase_max"},
    "regression": {"x1_miss_increase_max", "over_escalation_increase_max", "format_drop_max_responses",
                   "latency_increase_max_pct", "field_drop_max_responses", "item_regression_max_responses"},
}
SAME_DATA = (("dataset_sha256", "평가셋"), ("item_ids", "문항 구성"), ("options", "생성 옵션"), ("repeat_count", "반복 횟수"))
CHANGE_NOTES = (("prompt_version", "프롬프트 버전"), ("system_prompt_sha256", "프롬프트"), ("method", "방법"),
                ("method_assets_sha256", "방법 구성(체크리스트 질문)"), ("guardrail_version", "후처리 안전장치"),
                ("retry_policy", "형식 재요청 정책"), ("discard_check_policy", "폐기 확인 정책"),
                ("critical_check_policy", "Critical 체크리스트 정책"), ("think", "생각 끄기 설정"))


LABEL_VERSION = {"v15": "v1.5"}   # 기준 파일 meta.labels → 정답표 파일의 version (설계 의도 6)


class GateError(Exception):
    """판정 자체가 성립하지 않음 → 종료 코드 2."""


def load_json(path: Path) -> dict:
    if not Path(path).exists():
        raise GateError(f"파일이 없습니다: {path}")
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dataset_sha256(dataset: dict) -> str:
    """실행기(runner.py)가 실행 기록에 남기는 평가셋 지문과 같은 계산."""
    return hashlib.sha256(json.dumps(dataset, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_criteria(path: Path = GATE_CRITERIA) -> dict:
    """설계 의도 5."""
    c = tomllib.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else None
    if c is None:
        raise GateError(f"기준 파일이 없습니다: {path}")
    problems = [f"모르는 섹션 [{s}]" for s in c if s not in KNOWN]
    problems += [f"[{s}] 모르는 키 {k!r}" for s, body in c.items() if s in KNOWN for k in body if k not in KNOWN[s]]
    problems += [f"[{s}] {k} 가 없습니다" for s, keys in KNOWN.items() for k in keys if k not in c.get(s, {})]
    reg = c.get("regression", {})
    problems += [f"[regression.field_drop_max_responses] 모르는 칸 {f!r}"
                 for f in reg.get("field_drop_max_responses", {}) if f not in FIELD_KEYS]
    problems += [f"[regression.item_regression_max_responses] 모르는 칸 {f!r}"
                 for f in reg.get("item_regression_max_responses", {}) if f not in JUDGED]
    if len(c.get("meta", {}).get("baseline", [])) != len(c.get("meta", {}).get("seeds", [])):
        problems.append("[meta] baseline 실행 기록 수가 seeds 수와 다릅니다")
    if problems:
        raise GateError("합격 기준 파일 오류 → " + "; ".join(problems))
    return c


def check_labels(inputs: dict, labels: dict, crit: dict) -> None:
    """설계 의도 6 — 정답표가 기준 파일이 정한 판이고, 실행 입력과 같은 문항·같은 입력을 가리키는지 확인한다."""
    a, b = ({i["id"]: i for i in d["items"]} for d in (inputs, labels))
    problems = [] if set(a) == set(b) else ["문항 ID가 다릅니다"]
    expected = LABEL_VERSION.get(crit["meta"]["labels"])
    if labels.get("version") != expected:
        problems.append(f"정답표 판 {labels.get('version')!r} 이(가) 기준 파일이 정한 {expected!r} 이(가) 아닙니다")
    problems += [f"{k} {f} 이(가) 다릅니다" for k in sorted(set(a) & set(b))
                 for f in ("track", "split", "set", "input") if a[k].get(f) != b[k].get(f)]
    if problems:
        raise GateError("정답표가 실행 입력과 맞지 않음 → " + "; ".join(problems))


def seed_of(log: dict) -> int | None:
    seeds = log["metadata"]["run_config"].get("seeds")
    return seeds.get("1") if seeds else None


def by_seed(logs: list[tuple[Path, dict]], name: str, crit: dict, inputs_sha: str) -> dict[int, tuple[Path, dict]]:
    """설계 의도 4 — 실행 기록을 seed 로 묶는다."""
    meta, out, problems, dirty = crit["meta"], {}, [], []
    for path, log in logs:
        md = log["metadata"]
        if md["run_config"].get("dataset_sha256") != inputs_sha:
            problems.append(f"{name} {rel(path)} 의 평가셋 지문이 지금의 개발용 입력과 다릅니다")
        if any(r.get("split") != meta["split"] for r in log["results"]):
            raise GateError(f"{name} {rel(path)} 에 개발용이 아닌 문항이 있습니다. 게이트는 개발용 실행만 판정합니다.")
        if meta["target_model"] not in md["models"]:
            problems.append(f"{name} {rel(path)} 에 {meta['target_model']} 결과가 없습니다")
        if md["run_config"].get("repeat_count") != meta["repeat"]:
            problems.append(f"{name} {rel(path)} 의 반복 횟수가 기준 파일({meta['repeat']})과 다릅니다")
        reloads = [r for r in log["results"]
                   if r["model"] == meta["target_model"] and (r.get("load_duration_sec") or 0) > RELOAD_SEC]
        if reloads:
            where = ", ".join(f"{r['run_index']}회차 {r['item_id']}(로드 {r['load_duration_sec']}초)" for r in reloads)
            dirty.append(f"{rel(path)} [{where}]")
        seed = seed_of(log)
        if seed is None:
            problems.append(f"{name} {rel(path)} 이 seed 고정 모드가 아닙니다")
        elif seed in out:
            problems.append(f"{name} seed {seed} 실행이 중복되었습니다")
        else:
            out[seed] = (path, log)
    if dirty:
        raise GateError(f"측정 환경 오염 → {name} 실행 도중 모델이 다시 로드됨: " + "; ".join(dirty)
                        + ". 그 뒤 응답은 같은 seed로 재현되지 않습니다. 다른 Ollama 사용을 끄고 다시 실행하세요.")
    missing = [s for s in meta["seeds"] if s not in out]
    extra = [s for s in out if s not in meta["seeds"]]
    if missing or extra:
        problems.append(f"{name} seed 구성이 기준 파일과 다릅니다 (빠짐 {missing}, 남음 {extra})")
    if problems:
        raise GateError("비교 조건 불일치 → " + "; ".join(problems))
    return out


def check_comparable(base: dict, cand: dict, crit: dict) -> list[str]:
    """설계 의도 4 — 비교할 수 없으면 GateError. 판정하려는 변경은 notes 로 돌려준다."""
    model, problems = crit["meta"]["target_model"], []
    c_cfgs = [log["metadata"]["run_config"] for _, log in cand.values()]
    first = c_cfgs[0]
    for key, label in SAME_DATA + CHANGE_NOTES:
        if any(c.get(key) != first.get(key) for c in c_cfgs[1:]):
            problems.append(f"후보 실행끼리 {label}이(가) 다릅니다")
    for seed, (_, b_log) in base.items():
        b, c = b_log["metadata"]["run_config"], cand[seed][1]["metadata"]["run_config"]
        for key, label in SAME_DATA:
            if b.get(key) != c.get(key):
                problems.append(f"seed {seed}: {label}이(가) 기준선과 다릅니다")
        if b["model_digests"].get(model) != c["model_digests"].get(model):
            problems.append(f"seed {seed}: {model} 모델 digest가 기준선과 다릅니다 (재pull 등)")
    if problems:
        raise GateError("비교 조건 불일치 → " + "; ".join(dict.fromkeys(problems)))
    b0 = next(iter(base.values()))[1]["metadata"]["run_config"]
    return [f"{label} 변경됨 (이 게이트가 판정하려는 변경)" for key, label in CHANGE_NOTES if b0.get(key) != first.get(key)]


def score_log(log: dict, model: str, items: dict) -> dict:
    """(문항, 회차) → 채점 행."""
    return {(r["item_id"], r["run_index"]): score_one(r, items[r["item_id"]])
            for r in log["results"] if r["model"] == model}


def ok(row: dict, field: str) -> bool:
    return row["all_fields_ok"] if field == "6칸 정답" else row["fields"][field]["ok"]


def counts(rows: dict) -> dict:
    vals = list(rows.values())
    lat = [r["elapsed_sec"] for r in vals if r["elapsed_sec"] is not None]
    return {
        "n": len(vals),
        "success": sum(r["success"] for r in vals),
        "strict": sum(r["format"]["strict_pass"] for r in vals),
        "parsable": sum(r["format"]["parsable_pass"] for r in vals),
        "fields_ok": {f: sum(ok(r, f) for r in vals) for f in FIELD_KEYS},
        "x1": sum("X-1" in r["risk_miss"] for r in vals),
        "x2": sum("X-2" in r["risk_miss"] for r in vals),
        "x3": sum("X-3" in r["risk_miss"] for r in vals),
        "over": sum(r["over_escalation"] for r in vals),
        "halluc": sum(r["hallucinated"] for r in vals),
        "lang": sum(not r["format"]["rules"]["R7_output_language"] for r in vals),
        "enum": sum(not r["format"]["rules"]["R6_enum_valid"] for r in vals),
        "latency": round(mean(lat), 3) if lat else None,
    }


def item_changes(base_rows: dict, cand_rows: dict) -> dict:
    """설계 의도 3 — 칸마다 기준선 정답→후보 오답(regressed), 오답→정답(fixed) 응답 목록."""
    out = {}
    for f in FIELD_KEYS:
        reg, fix = [], []
        for key in sorted(base_rows):
            if key not in cand_rows:
                continue
            b, c = ok(base_rows[key], f), ok(cand_rows[key], f)
            entry = {"item_id": key[0], "run": key[1]}
            if f != "6칸 정답":
                entry.update(before=base_rows[key]["fields"][f]["predicted"], after=cand_rows[key]["fields"][f]["predicted"])
            if b and not c:
                reg.append(entry)
            elif c and not b:
                fix.append(entry)
        out[f] = {"regressed": reg, "fixed": fix}
    return out


def judge(bc: dict, cc: dict, changes: dict, crit: dict) -> list[dict]:
    """설계 의도 2 — 기준마다 (이름, 종류, 기준선 값, 후보 값, 허용, 통과)."""
    a, rb, r = crit["absolute"], crit["relative_to_baseline"], crit["regression"]
    checks = []

    def add(name, kind, base_v, cand_v, limit, passed):
        checks.append({"name": name, "kind": kind, "baseline": base_v, "candidate": cand_v,
                       "limit": limit, "passed": bool(passed)})

    rate = round(cc["success"] / cc["n"] * 100, 2) if cc["n"] else 0.0
    add("호출 성공률(%)", "절대", None, rate, f"≥ {a['success_rate_min']}", rate >= a["success_rate_min"])
    for key, label, field in (("hallucination_max", "날조 응답", "halluc"),
                              ("language_violation_max", "출력 언어 위반(R7)", "lang"),
                              ("enum_violation_max", "허용 값 위반(R6)", "enum")):
        add(label, "절대", bc[field], cc[field], f"≤ {a[key]}", cc[field] <= a[key])
    lim = r["x1_miss_increase_max"]
    add("X-1 Critical 미상신", "회귀", bc["x1"], cc["x1"], f"증가 ≤ {lim}", cc["x1"] - bc["x1"] <= lim)
    lim = r["over_escalation_increase_max"]
    add("과잉 상신", "회귀", bc["over"], cc["over"], f"증가 ≤ {lim}", cc["over"] - bc["over"] <= lim)
    lim = r["format_drop_max_responses"]
    for key, label in (("strict", "형식 STRICT(응답 수)"), ("parsable", "형식 PARSABLE(응답 수)")):
        add(label, "회귀", bc[key], cc[key], f"하락 ≤ {lim}", bc[key] - cc[key] <= lim)
    if bc["latency"] and cc["latency"]:
        lim = r["latency_increase_max_pct"]
        pct = round((cc["latency"] - bc["latency"]) / bc["latency"] * 100, 2)
        add("평균 지연(초)", "회귀", bc["latency"], cc["latency"], f"증가 ≤ {lim}% (실제 {pct:+}%)", pct <= lim)
    for f, lim in r["field_drop_max_responses"].items():
        b, c = bc["fields_ok"][f], cc["fields_ok"][f]
        label = "6칸 모두 정답(응답 수)" if f == "6칸 정답" else f"{f} 정답(응답 수)"
        add(label, "회귀", b, c, f"하락 ≤ {lim}", b - c <= lim)
    for f, lim in r["item_regression_max_responses"].items():
        n = len(changes[f]["regressed"])
        add(f"{f} 문항 단위 회귀(응답 수)", "회귀", 0, n, f"≤ {lim}", n <= lim)
    for key, label, field in (("x2_miss_increase_max", "X-2 결함 폐기", "x2"), ("x3_miss_increase_max", "X-3 경계 건 확정 처리", "x3")):
        lim = rb[key]
        add(label, "기준선 대비", bc[field], cc[field], f"증가 ≤ {lim}", cc[field] - bc[field] <= lim)
    lat = a["latency_max_sec"]
    add("평균 지연(초)", "절대", bc["latency"], cc["latency"], f"≤ {lat}", cc["latency"] is not None and cc["latency"] <= lat)
    return checks


def compare_seed(base_log: dict, cand_log: dict, model: str, items: dict, crit: dict) -> dict:
    b_rows, c_rows = score_log(base_log, model, items), score_log(cand_log, model, items)
    bc, cc = counts(b_rows), counts(c_rows)
    changes = item_changes(b_rows, c_rows)
    checks = judge(bc, cc, changes, crit)
    return {"seeds": cand_log["metadata"]["run_config"]["seeds"], "passed": all(c["passed"] for c in checks),
            "checks": checks, "baseline": bc, "candidate": cc, "item_changes": changes}


def evaluate(cand_paths: list[Path], crit: dict, base_paths: list[Path] | None = None,
             inputs_path: Path = DEV_INPUTS, labels_path: Path = DEV_LABELS) -> dict:
    """판정 결과(dict). 판정할 수 없으면 GateError."""
    inputs, labels = load_json(inputs_path), load_json(labels_path)
    check_labels(inputs, labels, crit)
    sha = dataset_sha256(inputs)
    base_paths = base_paths or [ROOT / p for p in crit["meta"]["baseline"]]
    base = by_seed([(p, load_json(p)) for p in base_paths], "기준선", crit, sha)
    cand = by_seed([(p, load_json(p)) for p in cand_paths], "후보", crit, sha)
    notes = check_comparable(base, cand, crit)
    items, model, seeds = {i["id"]: i for i in labels["items"]}, crit["meta"]["target_model"], crit["meta"]["seeds"]
    per_seed = [compare_seed(base[s][1], cand[s][1], model, items, crit) for s in seeds]
    totals = {f: {k: sum(len(r["item_changes"][f][k]) for r in per_seed) for k in ("regressed", "fixed")} for f in FIELD_KEYS}
    return {"verdict": "PASS" if all(r["passed"] for r in per_seed) else "FAIL", "target_model": model, "notes": notes,
            "baseline": [rel(base[s][0]) for s in seeds], "candidate": [rel(cand[s][0]) for s in seeds],
            "labels": {"path": rel(labels_path), "version": labels["version"], "sha256": dataset_sha256(labels)},
            "item_change_totals": totals, "per_seed": per_seed}


def print_result(result: dict, seeds: list[int]) -> None:
    print(f"\n[{result['target_model']}] {result['verdict']}  (seed " +
          " / ".join(f"{s} {'PASS' if r['passed'] else 'FAIL'}" for s, r in zip(seeds, result["per_seed"])) + ")")
    for n in result["notes"]:
        print(f"  · {n}")
    for s, r in zip(seeds, result["per_seed"]):
        for c in r["checks"]:
            if not c["passed"]:
                print(f"  ❌ seed {s}: {c['name']} {c['baseline']} → {c['candidate']} (허용 {c['limit']})")
    changed = {f: t for f, t in result["item_change_totals"].items() if t["regressed"] or t["fixed"]}
    print("  문항 단위 변화(seed 합계): " + (", ".join(f"{f} 새로 틀림 {t['regressed']}·새로 맞힘 {t['fixed']}"
                                              for f, t in changed.items()) or "없음"))


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="회귀 게이트 — 후보 실행 3건(seed 1·11·21)을 기준선과 비교")
    p.add_argument("--candidate", type=Path, action="append", required=True, help="후보 실행 기록 (seed마다 1개)")
    p.add_argument("--criteria", type=Path, default=GATE_CRITERIA)
    p.add_argument("--no-write", action="store_true", help="판정 결과 파일을 쓰지 않음")
    args = p.parse_args(argv)
    try:
        crit = load_criteria(args.criteria)
        result = evaluate(args.candidate, crit)
    except GateError as e:
        print(f"판정 불가: {e}")
        return 2
    print_result(result, crit["meta"]["seeds"])
    if not args.no_write:
        GATE_DIR.mkdir(parents=True, exist_ok=True)
        out = GATE_DIR / f"gate_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {rel(out)}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
