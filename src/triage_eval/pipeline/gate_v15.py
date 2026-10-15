"""채택 구성 회귀 게이트 (v1.5 라벨) — 입력은 aether_raid_v14 개발용, 채점 정답표는 aether_raid_v15 / 방법 M2.

v1.4 게이트(gate_v14.py)와 같은 기준선·같은 기준 값으로, 정답표만 v1.5 평가셋으로 바꿔 판정한다.
계획: report/regression_gate_v15_plan.md · 기준 파일: gate_criteria_v15.toml

사용법
------
    uv run triage-run --dataset v14 --method m2 --seed 1 --strict-env      # seed 11, 21 도 같은 방법으로
    uv run triage-gate --dataset v15 --candidate <seed1.json> --candidate <seed11.json> --candidate <seed21.json>

종료 코드
---------
    0  PASS (seed 3개 모두 통과)   1  FAIL   2  판정 불가 (v1.4 게이트의 판정 불가 조건 + 아래 설계 의도 3·4)

출력
----
    data/results/v15/gate_result_v15.json   기계 판독용 판정 결과
    report/regression_gate_v15.md           사람이 읽는 판정 근거

[설계 의도]
1. 판정은 gate_v14.run 을 그대로 부르고 정답표·출력 경로·보고서 머리말만 넘긴다(gate_v14.py 설계 의도 8).
   판정 규칙을 복사하면 두 게이트의 판정이 어긋날 수 있다.
2. 모델을 다시 실행하지 않는다. v1.4와 v1.5 평가셋은 입력이 같으므로, v1.4 게이트의 기준선 실행 기록을 v1.5 정답표로
   채점한 값이 곧 v1.5 기준선이다. 기준선을 다시 실행하면 응답과 정답표가 함께 바뀌어 무엇이 기준선을 바꿨는지
   구분할 수 없다(계획 §2, OBS-004·OBS-005). 후보 실행도 지금처럼 --dataset v14 로 만든다.
3. 판정 전에 v1.5 정답표의 문항 ID·입력·구분이 v1.4 평가셋과 같은지 확인한다(rescore.check_datasets, 재채점 검증 V0).
   입력이 다르면 v1.4 실행 기록에 v1.5 정답을 대조할 수 없으므로 판정하지 않는다. 정답표 파일이 실제로 v1.5인지
   (version, changed_ids_v15)도 확인한다. v1.4 파일을 가리키면 V0는 통과하므로, 조용히 v1.4 정답표로 판정하게 된다.
4. 기준선·후보 실행 기록마다 평가셋 지문(run_config.dataset_sha256)이 지금 저장소의 v1.4 평가셋과 같은지 확인한다.
   gate_v14 는 기준선과 후보의 지문이 서로 같은지만 보므로, 둘 다 다른 입력으로 만든 실행이어도 통과할 수 있다.
   v1.5 정답표를 대조하려면 모델이 본 입력이 v1.5 정답표의 입력과 같아야 한다.
5. 기준 파일은 gate_criteria_v14.toml 과 키가 같고 [meta] labels 하나만 더한다. labels 가 "v15", dataset 이 "v14" 가
   아니면 판정하지 않는다. 기준 값은 v1.4 게이트와 같다(계획 §3).
6. 기준선과 후보의 응답 동일성(파싱된 6칸 값)을 정답표 없이 세어 판정 결과에 함께 남긴다(계획 §5 G0). 문항 단위 변화가
   "정답표가 바뀌어서"인지 "응답이 달라서"인지 구분하기 위해서다. 판정 결과에는 채점에 쓴 정답표의 경로·버전·지문도 남긴다.
"""

import hashlib
import json
from pathlib import Path

from triage_eval.common.paths import ROOT
from triage_eval.pipeline import compare as cmp
from triage_eval.pipeline import gate as g
from triage_eval.pipeline import gate_v14 as gv
from triage_eval.pipeline import rescore as rs
from triage_eval.pipeline.contract import parse_v2
from triage_eval.pipeline.score import JUDGED

CRITERIA = ROOT / "gate_criteria_v15.toml"
INPUTS = ROOT / "data" / "eval_v14" / "aether_raid_v14.json"    # 실행 기록의 평가셋 (모델이 본 입력)
LABELS = ROOT / "data" / "eval_v15" / "aether_raid_v15.json"    # 채점 정답표
RESULT_JSON = ROOT / "data" / "results" / "v15" / "gate_result_v15.json"
REPORT = ROOT / "report" / "regression_gate_v15.md"
HEADER = ("채택 구성 회귀 게이트 판정 (v1.5 정답표 · 입력 v1.4 평가셋 · M2)", "gate_criteria_v15.toml",
          "src/triage_eval/pipeline/gate_v15.py")
KNOWN = {**gv.KNOWN, "meta": gv.KNOWN["meta"] | {"labels"}}   # 설계 의도 5


def load_criteria(path: Path) -> dict:
    """설계 의도 5."""
    c = gv.load_criteria(path, KNOWN)
    meta = c["meta"]
    if meta["labels"] != "v15" or meta["dataset"] != "v14":
        raise g.GateError(f"합격 기준 파일 오류 → labels 는 'v15', dataset 은 'v14' 여야 합니다 "
                          f"(labels={meta['labels']!r}, dataset={meta['dataset']!r})")
    return c


def dataset_sha256(dataset: dict) -> str:
    """run.py 가 실행 기록에 남기는 평가셋 지문과 같은 계산."""
    return hashlib.sha256(json.dumps(dataset, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def check_inputs(paths: list[Path], inputs: dict, labels: dict) -> None:
    """설계 의도 3·4 — 어긋나면 GateError."""
    problems = [f"정답표 {p}" for p in rs.check_datasets(inputs, labels)]
    if labels.get("version") != "v1.5" or not labels.get("changed_ids_v15"):
        problems.append("정답표가 v1.5 평가셋이 아닙니다 (version·changed_ids_v15 확인) — v1.4 정답표로 판정하는 실수를 막는다")
    sha = dataset_sha256(inputs)
    problems += [f"{g.rel(p)} 의 평가셋 지문이 지금의 v1.4 평가셋과 다릅니다"
                 for p in paths if g.load_log(p)["metadata"]["run_config"].get("dataset_sha256") != sha]
    if problems:
        raise g.GateError("v1.5 정답표로 채점할 수 없음 → " + "; ".join(problems))


def parsed_fields(record: dict) -> tuple:
    """응답에서 읽은 6칸 값. 정답표를 쓰지 않는다 (설계 의도 6)."""
    pred = parse_v2(record.get("response_text", "") if record.get("success") else "")
    return tuple(pred.get(f) for f in JUDGED)


def response_identity(base_paths: list[Path], cand_paths: list[Path], crit: dict) -> dict:
    """설계 의도 6 (계획 §5 G0) — seed마다 기준선과 후보의 같은 (문항, 회차) 응답에서 6칸 값이 다른 것을 센다."""
    model = crit["meta"]["target_model"]

    def by_seed(paths):
        out = {}
        for p in paths:
            log = g.load_log(p)
            out[cmp.seed_of(log)] = {(r["item_id"], r["run_index"]): r for r in log["results"] if r["model"] == model}
        return out

    base, cand = by_seed(base_paths), by_seed(cand_paths)
    result = {}
    for seed in crit["meta"]["seeds"]:
        b, c = base.get(seed, {}), cand.get(seed, {})
        diff = []
        for key in sorted(set(b) & set(c)):
            fb, fc = parsed_fields(b[key]), parsed_fields(c[key])
            if fb != fc:
                diff.append({"item_id": key[0], "run": key[1],
                             "fields": [f for f, x, y in zip(JUDGED, fb, fc) if x != y]})
        result[seed] = {"compared": len(set(b) & set(c)), "different": len(diff), "items": diff}
    return result


def render_identity(identity: dict) -> str:
    L = ["", "## 응답 동일성 (계획 §5 G0, 정답표와 무관)", "",
         "기준선과 후보의 같은 (문항, 회차) 응답에서 파싱된 6칸 값이 다른 응답 수. 0이 아닌 응답의 문항 단위 변화는 "
         "도구 결함이 아니라 응답 차이다.", "", "| seed | 비교 응답 | 6칸 값이 다른 응답 |", "| ---: | ---: | ---: |"]
    L += [f"| {s} | {v['compared']} | {v['different']} |" for s, v in identity.items()]
    for s, v in identity.items():
        if v["items"]:
            L += ["", f"**seed {s}**", ""] + [f"- {d['item_id']} {d['run']}회차: {', '.join(d['fields'])}" for d in v["items"]]
    return "\n".join(L) + "\n"


def run(cand_paths: list[Path], crit: dict, base_paths: list[Path] | None = None, write: bool = True) -> int:
    base_paths = base_paths or [ROOT / p for p in crit["meta"]["baseline"]]
    inputs, labels = rs.load_dataset(INPUTS), rs.load_dataset(LABELS)
    check_inputs(list(base_paths) + list(cand_paths), inputs, labels)
    code = gv.run(cand_paths, crit, base_paths, write, items=rs.items_of(labels),
                  result_json=RESULT_JSON, report=REPORT, header=HEADER)
    identity = response_identity(base_paths, cand_paths, crit)
    print("  응답 동일성(G0): " + ", ".join(f"seed {s} 6칸 값이 다른 응답 {v['different']}/{v['compared']}"
                                          for s, v in identity.items()))
    if write:   # 설계 의도 6 — 판정 결과에 정답표 출처와 응답 동일성을 덧붙인다
        result = json.loads(RESULT_JSON.read_text(encoding="utf-8"))
        result["labels"] = {"path": g.rel(LABELS), "version": labels["version"], "sha256": dataset_sha256(labels)}
        result["response_identity"] = identity
        RESULT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        REPORT.write_text(REPORT.read_text(encoding="utf-8") + render_identity(identity), encoding="utf-8")
    return code
