"""v1.5 9단계 — 봉인된 새 최종 평가용 세트에서 라우팅(S1+S2)을 한 번 실행·판정한다.

계획(사전 등록): report/final_run_v15_plan.md · 기준: final_run_v15.toml · 신호 정의: routing_v15.toml

사용법
------
    uv run triage-final run                  # guard·봉인 확인 뒤 seed 1·11·21을 차례로 실행한다 (문항당 6응답)
    uv run triage-final run --redo-incomplete 21   # 기록 파일 없이 멈춘 seed 만 다시 (마커에 남는다)
    uv run triage-final verdict              # 날조 사람 확인이 커밋된 뒤 라우팅·채점을 계산하고 판정한다
    uv run triage-final verdict --no-write   # 계산·출력만

종료 코드
---------
    0  완료   2  실행·판정 불가 (guard·봉인·구성·입력 검증 실패, 이미 실행됨)

[설계 의도]
1. 한 번만 쓴다. 첫 실행 전에 STARTED 마커를 만들고 seed마다 상태(시작·완료·기록 파일 해시)를 적는다. 완료된 seed 는 다시 돌지 않는다.
   기록 파일 없이 멈춘 seed 만 --redo-incomplete 로 다시 할 수 있고, 그 사실을 마커에 남긴다. 결과를 본 뒤 재실행으로 결과를
   바꾸는 길을 막는 장치다(CLAUDE.md: 최종 평가용 세트는 한 번 실행하면 다시 평가용으로 쓰지 않는다).
2. 계획·기준·도구·채점 코드·봉인 기록이 커밋되어 있고 변경이 없을 때만 실행·판정한다. 평가셋이 봉인 기록의 SHA-256과 같아야 한다.
3. 이 도구는 실행하고 판정할 뿐 프롬프트·방법·안전장치·옵션을 바꾸지 않는다. 실행은 triage-run(run.py)을 그대로 호출한다.
4. 판정 도구는 새 실행 기록의 구성(옵션·프롬프트·안전장치·재시도·폐기 점검·방법·모델 digest)이 개발용 기준선과 같은지 확인한다.
   하나라도 다르거나 측정 환경 경고가 있으면 "판정하지 않음"으로 기록한다(OBS-005).
5. 날조는 사람이 확인한 결과(data/final_v15/fabrication_check_v15_final.json)를 쓴다. 이 파일이 커밋되어 있어야 판정한다. 탐지기(S4)와
   사람 판정의 일치는 참고로만 보고한다. 7단계 날조 통과는 S4와 같은 탐지기라 정의상 0이어서 이 단계에서는 쓰지 않는다.
6. 위험 누락은 score.score_one 을 그대로 쓰고 신호·후보 평가는 routing.py 의 build_rows·evaluate 를 그대로 쓴다. 판정용 채점 규칙을
   새로 만들면 게이트·재채점·7단계와 같은 자로 비교할 수 없다. 날조만 사람 확인 결과로 바꿔 넣는다.
7. 판정 순서와 임계값은 final_run_v15.toml(= routing_v15_results.md §6)에 고정되어 있다. verdict() 는 순수 함수라 경계값을 시험으로
   확인한다. 비율은 반올림 전 정수 비율(Fraction)로 비교한다.
8. 날조 확인 단위는 "같은 문항에서 요약 문장이 글자까지 같은 응답 묶음"이다. 묶음 식별자는 입력 문항과 정규화한 요약에서 정하므로
   확인 화면과 판정 도구가 같은 묶음을 얻는다.
9. 결과는 출력하기 전에 먼저 저장한다(ISSUE-014).
10. 실행 전에 설치된 모델 digest 가 개발용 기준선과 같은지 확인한다(모델 호출 아님). 다르면 360응답을 다 돌린 뒤에야 구성 불일치로 판정이
   막히고 한 번뿐인 실행이 소진되기 때문이다. 환경 경고는 triage-run 의 --strict-env 가 첫 응답 전에 막는다.
11. 마커가 없는데 기록 폴더에 실행 기록이 있으면 실행하지 않는다(마커를 지워 한 번만 쓰는 장치를 피하는 길 차단). 마커는 임시 파일로
   쓴 뒤 바꿔 끼우고, 읽을 수 없으면 불가로 알린다. 재실행(--redo-incomplete)은 그 seed 의 기록 파일이 history 에 이미 있으면 거부한다.
12. 판정 도구는 기록의 평가셋 해시(run.py 가 쓰는 방식)·문항 목록·seed 목록이 계획과 같고 세 기록 파일이 서로 다른지도 확인한다.
13. 요약은 [요약] 라벨 줄부터 다음 필드 라벨 전까지 이어 붙인 전체다. 줄 파서가 라벨 없는 줄을 따로 두므로, 여러 줄 요약의 둘째 줄부터를
   확인 화면과 날조 판정이 놓치지 않게 한다.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import ollama

from triage_eval.common.ollama_runtime import get_model_digests
from triage_eval.common.paths import ROOT
from triage_eval.common.score_format import parse_lines
from triage_eval.pipeline import routing as rt
from triage_eval.pipeline import rescore as rs
from triage_eval.pipeline import run as run_mod
from triage_eval.pipeline.contract import parse_v2
from triage_eval.pipeline.gate import GateError
from triage_eval.pipeline.score import rel

CRITERIA = ROOT / "final_run_v15.toml"
KNOWN = {
    "meta": {"version", "dataset", "target_model", "method", "split", "seeds", "repeat", "baseline", "history_dir",
             "started_marker", "fabrication_check", "result_json", "report", "config_keys"},
    "verdict": {"signals", "min_bad_responses", "min_bad_items", "auto_rate_floor", "adopt_pass_max", "partial_pass_max_pct"},
}
GUARDED = ["src/triage_eval/pipeline/final_v15.py", "final_run_v15.toml", "report/final_run_v15_plan.md", "tests/test_final_run_v15.py",
           "routing_v15.toml", "src/triage_eval/pipeline/routing.py", "src/triage_eval/pipeline/run.py",
           "src/triage_eval/pipeline/score.py", "src/triage_eval/common/detect_hallucination.py",
           "src/triage_eval/pipeline/contract.py", "src/triage_eval/pipeline/prompt.py", "src/triage_eval/pipeline/methods.py",
           "src/triage_eval/pipeline/guardrail.py", "src/triage_eval/pipeline/rescore.py", "src/triage_eval/pipeline/gate.py",
           "src/triage_eval/pipeline/gate_v14.py", "src/triage_eval/pipeline/gate_v15.py", "src/triage_eval/pipeline/compare.py",
           "src/triage_eval/pipeline/selection.py", "src/triage_eval/common/score_format.py", "src/triage_eval/common/ollama_runtime.py",
           "src/triage_eval/common/preflight.py", "src/triage_eval/common/paths.py",   # 설계 의도 2 — 실행·채점이 import 하는 코드 전부(시험이 확인)
           "tools/final_v15/rater_server.py", "tools/final_v15/rater_ui.html", "pyproject.toml",
           "data/eval_v15_final/aether_raid_v15_final.json", "data/eval_v15_final/sealed_v15.json",
           "data/eval_v15_final/review_answers_v15_final.json"]
FABRICATION = ("날조 있음", "날조 없음")
DEV_RESULT = ROOT / "data" / "results" / "v15" / "routing" / "routing_dev_v15.json"


# ── 기준·guard·봉인 ─────────────────────────────────────────

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


def git_lines(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return [x for x in out.stdout.decode("utf-8").splitlines() if x.strip()]


def committed_and_clean(paths: list[str]) -> list[str]:
    """커밋되어 있지 않거나 바뀐 파일을 이유와 함께 돌려준다."""
    problems = [f"{f} 가 저장소에 커밋되어 있지 않습니다" for f in paths if not git_lines("ls-files", "--", f)]
    dirty = git_lines("status", "--porcelain", "--", *paths)
    if dirty:
        problems.append("커밋하지 않은 변경이 있습니다: " + ", ".join(dirty))
    return problems


def guard() -> None:
    """설계 의도 2."""
    problems = committed_and_clean(GUARDED)
    if problems:
        raise GateError("; ".join(problems))


def sha256_lf(path: Path) -> str:
    """줄바꿈(CRLF·LF)과 무관한 해시 — 봉인 기록과 같은 방식(seal_final_v15.py 설계 의도 7)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def check_seal(crit: dict) -> None:
    """설계 의도 2 — 평가셋·승인 기록이 봉인 기록과 같아야 한다."""
    sealed_path = ROOT / "data" / "eval_v15_final" / "sealed_v15.json"
    if not sealed_path.exists():
        raise GateError("봉인 기록이 없습니다")
    rec = json.loads(sealed_path.read_text(encoding="utf-8"))
    ds = ROOT / crit["meta"]["dataset"]
    problems = []
    if rec["dataset"]["path"] != crit["meta"]["dataset"]:
        problems.append("기준의 평가셋 경로가 봉인 기록과 다릅니다")
    if sha256_lf(ds) != rec["dataset"]["sha256"]:
        problems.append("평가셋의 SHA-256이 봉인 기록과 다릅니다")
    answers = ROOT / rec["review_record"]["path"]
    if sha256_lf(answers) != rec["review_record"]["sha256"]:
        problems.append("승인 기록의 SHA-256이 봉인 기록과 다릅니다")
    doc = json.loads(ds.read_text(encoding="utf-8"))
    if len(doc["items"]) != rec["dataset"]["items"]:
        problems.append("문항 수가 봉인 기록과 다릅니다")
    if not str(doc.get("status", "")).startswith("sealed") or any(i.get("review") != "approved" for i in doc["items"]):
        problems.append("평가셋이 봉인·승인 상태가 아닙니다")
    if problems:
        raise GateError("봉인 확인 실패 → " + "; ".join(problems))


# ── STARTED 마커 (설계 의도 1) ───────────────────────────────

def read_marker(crit: dict) -> dict | None:
    p = ROOT / crit["meta"]["started_marker"]
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise GateError(f"실행 마커를 읽을 수 없습니다({e}). 마커를 지우거나 고치지 말고 상태를 확인하세요") from e


def write_marker(crit: dict, marker: dict) -> None:
    """임시 파일에 쓴 뒤 바꿔 끼워 도중에 끊겨도 마커가 깨지지 않게 한다."""
    p = ROOT / crit["meta"]["started_marker"]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def current_digests(model: str) -> dict:
    """설치된 모델의 digest(모델 호출 아님). 시험에서 바꿔 끼울 수 있게 함수로 둔다."""
    return get_model_digests(ollama.Client(), [model])


def check_model_digest(crit: dict) -> None:
    """설계 의도 10 — 실행 전에 모델 digest 가 개발용 기준선과 같은지 본다(한 번뿐인 실행을 다 쓴 뒤 알게 되지 않도록)."""
    model = crit["meta"]["target_model"]
    base = json.loads((ROOT / crit["meta"]["baseline"][0]).read_text(encoding="utf-8"))["metadata"]["run_config"]["model_digests"]
    now = current_digests(model)
    if now.get(model) != base.get(model):
        raise GateError(f"모델 digest 가 개발용 기준선과 다릅니다(기준선 {base.get(model)}, 지금 {now.get(model)}). 같은 모델을 쓰도록 맞춘 뒤 실행하세요")


def new_marker(crit: dict) -> dict:
    return {"version": crit["meta"]["version"], "started_at": datetime.now().isoformat(timespec="seconds"),
            "plan": "report/final_run_v15_plan.md", "redo": [],
            "seeds": {str(s): {"status": "pending"} for s in crit["meta"]["seeds"]}}


def seed_argv(crit: dict, seed: int) -> list[str]:
    m = crit["meta"]
    return ["--dataset", "v15final", "--split", m["split"], "--method", m["method"], "--seed", str(seed),
            "--repeat", str(m["repeat"]), "--final", "--strict-env"]


def cmd_run(args) -> int:
    crit = load_criteria()
    guard()
    check_seal(crit)
    hist = ROOT / crit["meta"]["history_dir"]
    marker = read_marker(crit)
    if marker is None:
        if hist.exists() and any(hist.glob("*.json")):   # 설계 의도 1 — 마커를 지워 한 번만 쓰는 장치를 피하는 길을 막는다
            raise GateError(f"실행 마커가 없는데 {rel(hist)} 에 실행 기록이 있습니다. 마커를 지운 것으로 보고 실행하지 않습니다")
        marker = new_marker(crit)
    redo = set(args.redo_incomplete or [])
    if any(marker["seeds"][str(s)]["status"] != "done" for s in crit["meta"]["seeds"]):
        check_model_digest(crit)
    for seed in crit["meta"]["seeds"]:
        st = marker["seeds"][str(seed)]
        if st["status"] == "done":
            print(f"seed {seed}: 이미 완료 ({st['file']}) — 다시 실행하지 않습니다.")
            continue
        if st["status"] in ("started", "failed"):
            if seed not in redo:
                raise GateError(f"seed {seed} 실행이 기록 파일 없이 멈췄습니다. 환경 오염·도구 오류가 확인된 경우에만 "
                                f"--redo-incomplete {seed} 로 다시 실행하세요(마커에 남습니다).")
            names_now = {p.name for p in hist.glob("*.json")} if hist.exists() else set()
            if names_now - set(st.get("history_before", [])):   # 설계 의도 1 — 그 seed 의 기록 파일이 이미 있으면 다시 하지 않는다
                raise GateError(f"seed {seed} 의 기록 파일이 이미 있습니다({sorted(names_now - set(st.get('history_before', [])))}). 다시 실행하지 않습니다")
            marker["redo"].append({"seed": seed, "at": datetime.now().isoformat(timespec="seconds"), "previous": st})
        before = set(hist.glob("*.json")) if hist.exists() else set()
        marker["seeds"][str(seed)] = {"status": "started", "at": datetime.now().isoformat(timespec="seconds"),
                                      "history_before": sorted(p.name for p in before)}
        write_marker(crit, marker)   # 실행 전에 먼저 남긴다
        rc = run_mod.main(seed_argv(crit, seed))
        new = sorted(set(hist.glob("*.json")) - before) if hist.exists() else []
        if rc != 0 or len(new) != 1:
            marker["seeds"][str(seed)] = {"status": "failed", "return_code": rc, "new_files": [rel(p) for p in new],
                                          "history_before": sorted(p.name for p in before)}
            write_marker(crit, marker)
            print(f"seed {seed} 실행이 완료되지 않았습니다(종료 코드 {rc}). 마커에 failed 로 남겼습니다.")
            return 2
        marker["seeds"][str(seed)] = {"status": "done", "file": rel(new[0]), "sha256": sha256_lf(new[0]),
                                      "finished_at": datetime.now().isoformat(timespec="seconds")}
        write_marker(crit, marker)
    print("세 seed 실행이 모두 끝났습니다. 다음: 날조 사람 확인 화면(tools/final_v15/rater_server.py) → 결과 커밋 → triage-final verdict")
    return 0


# ── 날조 확인 묶음 (설계 의도 8) ─────────────────────────────

def norm_summary(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def response_key(record: dict) -> str:
    return f"s{record.get('seed')}-{record['eval_id']}"


def summary_of(record: dict) -> str:
    """[요약]의 전체 — 요약이 여러 줄이면 다음 필드 라벨 전까지 이어 붙인다(설계 의도 8).
    줄 파서(parse_lines)는 라벨이 없는 줄을 따로 두므로 parse_v2 만 쓰면 둘째 줄부터 사람이 보지 못하고 지나간다."""
    if not record.get("success"):
        return ""
    parts, inside = [], False
    for label, value, _ in parse_lines((record.get("response_text") or "").strip()):
        if label == "요약" and not inside and not parts:
            inside = True
            parts.append(value)
        elif inside and label in ("__TEXT__", "__BLANK__"):
            parts.append(value)
        elif inside:
            break
    return norm_summary(" ".join(parts))


def group_id(item_id: str, summary: str) -> str:
    return hashlib.sha256(f"{item_id}\n{summary}".encode("utf-8")).hexdigest()[:12]


def fabrication_groups(records: list[dict]) -> tuple[dict, list[str]]:
    """(묶음 식별자 → {item_id, summary, members}, 요약이 없는 응답 키 목록)."""
    groups: dict[str, dict] = {}
    empty = []
    for r in records:
        s = summary_of(r)
        key = response_key(r)
        if not s:
            empty.append(key)
            continue
        g = groups.setdefault(group_id(r["item_id"], s), {"item_id": r["item_id"], "summary": s, "members": []})
        g["members"].append(key)
    return groups, empty


def display_order(group_ids: list[str]) -> list[str]:
    """문항·seed 순서에서 오는 단서를 줄이도록 식별자 해시로 섞는다(결정적)."""
    return sorted(group_ids, key=lambda g: hashlib.sha256(f"order:{g}".encode("utf-8")).hexdigest())


def load_run_records(crit: dict, marker: dict) -> tuple[list[dict], list[dict]]:
    """마커에 적힌 기록 파일을 읽는다. (응답 목록, 파일별 메타데이터)"""
    records, metas = [], []
    for seed in crit["meta"]["seeds"]:
        st = marker["seeds"][str(seed)]
        if st["status"] != "done":
            raise GateError(f"seed {seed} 실행이 완료되지 않았습니다")
        path = ROOT / st["file"]
        if sha256_lf(path) != st["sha256"]:
            raise GateError(f"{st['file']} 의 SHA-256이 마커와 다릅니다(실행 기록이 바뀌었습니다)")
        log = json.loads(path.read_text(encoding="utf-8"))
        metas.append(log["metadata"])
        records += [r for r in log["results"] if r["model"] == crit["meta"]["target_model"]]
    return records, metas


# ── 구성 동일성 (설계 의도 4) ────────────────────────────────

def config_problems(crit: dict, metas: list[dict]) -> list[str]:
    keys = crit["meta"]["config_keys"]
    base = [json.loads((ROOT / p).read_text(encoding="utf-8"))["metadata"]["run_config"] for p in crit["meta"]["baseline"]]
    problems = []
    for i, meta in enumerate(metas):
        cfg = meta["run_config"]
        for k in keys:
            if cfg.get(k) != base[0].get(k):
                problems.append(f"seed 기록 {i + 1}: {k} 가 개발용 기준선과 다릅니다")
        if cfg.get("environment", {}).get("warnings"):
            problems.append(f"seed 기록 {i + 1}: 측정 환경 경고가 있습니다 {cfg['environment']['warnings']}")
    for b in base[1:]:
        if any(b.get(k) != base[0].get(k) for k in keys):
            problems.append("기준선 파일끼리 구성이 다릅니다")
    return problems


def run_shape_problems(crit: dict, marker: dict, metas: list[dict], dataset: dict, item_ids: set[str]) -> list[str]:
    """설계 의도 4 — 실행 기록이 계획한 모양인지(평가셋·문항·seed·서로 다른 파일)."""
    problems = []
    ds_hash = hashlib.sha256(json.dumps(dataset, ensure_ascii=False, sort_keys=True).encode()).hexdigest()   # run.py 가 기록하는 방식과 같다
    files = [marker["seeds"][str(s)]["file"] for s in crit["meta"]["seeds"]]
    if len(set(files)) != len(files):
        problems.append("마커의 세 seed 기록 파일이 서로 다르지 않습니다")
    for seed, meta in zip(crit["meta"]["seeds"], metas):
        cfg = meta["run_config"]
        want = {str(i): seed + i - 1 for i in range(1, crit["meta"]["repeat"] + 1)}
        if cfg.get("seeds") != want:
            problems.append(f"seed {seed} 기록의 seed 목록이 {want} 가 아닙니다({cfg.get('seeds')})")
        if cfg.get("dataset_sha256") != ds_hash:
            problems.append(f"seed {seed} 기록의 평가셋 해시가 봉인된 평가셋과 다릅니다")
        if set(cfg.get("item_ids", [])) != item_ids:
            problems.append(f"seed {seed} 기록의 문항 목록이 평가셋과 다릅니다")
    return problems


# ── 판정 (설계 의도 7) ───────────────────────────────────────

def verdict(m: dict, thr: dict) -> tuple[str, str]:
    """routing_v15_results.md §6 순서. m 은 bad_total·bad_items·auto·n·bad_auto·bad_auto_total 를 가진다."""
    if m["bad_total"] < thr["min_bad_responses"] or m["bad_items"] < thr["min_bad_items"]:
        return "표본 부족", (f"위험·날조 응답 {m['bad_total']}건·{m['bad_items']}문항 "
                          f"(기준 {thr['min_bad_responses']}건·{thr['min_bad_items']}문항 이상)")
    auto_pct = Fraction(100 * m["auto"], m["n"])
    if auto_pct < Fraction(str(thr["auto_rate_floor"])):
        return "미채택", f"자동 처리율 {float(auto_pct):.1f}% 가 하한 {thr['auto_rate_floor']}% 미만"
    if m["bad_auto"] <= thr["adopt_pass_max"]:
        return "채택", f"위험·날조 통과 {m['bad_auto']}건"
    pass_pct = Fraction(100 * m["bad_auto"], m["bad_total"])
    if pass_pct <= Fraction(str(thr["partial_pass_max_pct"])):
        return "부분 채택", f"위험·날조 통과 {m['bad_auto']}/{m['bad_total']} = {float(pass_pct):.1f}% (기준 {thr['partial_pass_max_pct']}% 이하)"
    return "미채택", f"위험·날조 통과 {m['bad_auto']}/{m['bad_total']} = {float(pass_pct):.1f}% (기준 {thr['partial_pass_max_pct']}% 이하 아님)"


def load_fabrication(crit: dict, groups: dict, empty: list[str]) -> dict[str, bool]:
    """사람 확인 결과 → 응답 키별 날조 여부. 모든 묶음이 판정돼 있고 요약이 확인 당시와 같아야 한다."""
    path = ROOT / crit["meta"]["fabrication_check"]
    if not path.exists():
        raise GateError(f"날조 사람 확인 결과가 없습니다: {rel(path)}")
    problems = committed_and_clean([crit["meta"]["fabrication_check"]])
    if problems:
        raise GateError("날조 확인 결과가 잠겨 있지 않습니다 → " + "; ".join(problems))
    try:
        answers = json.loads(path.read_text(encoding="utf-8"))["answers"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise GateError(f"날조 확인 결과 파일을 읽을 수 없습니다({e!r})") from e
    out = {k: False for k in empty}
    missing = []
    for gid, g in groups.items():
        a = answers.get(gid)
        if not a or a.get("결정") not in FABRICATION:
            missing.append(gid)
            continue
        if sorted(a.get("members", [])) != sorted(g["members"]) or a.get("summary_sha") != hashlib.sha256(g["summary"].encode("utf-8")).hexdigest():
            raise GateError(f"묶음 {gid} 의 응답·요약이 확인 당시와 다릅니다")
        for k in g["members"]:
            out[k] = a["결정"] == "날조 있음"
    if missing:
        raise GateError(f"날조 확인이 끝나지 않은 묶음이 {len(missing)}개 있습니다")
    return out


def run_verdict(crit: dict) -> dict:
    marker = read_marker(crit)
    if not marker:
        raise GateError("실행 마커가 없습니다 — 먼저 triage-final run")
    records, metas = load_run_records(crit, marker)
    dataset = rs.load_dataset(ROOT / crit["meta"]["dataset"])
    items = rs.items_of(dataset)
    problems = config_problems(crit, metas) + run_shape_problems(crit, marker, metas, dataset, set(items))
    got = {r["item_id"] for r in records}
    if got != set(items):
        problems.append(f"문항이 맞지 않습니다 (없음 {sorted(set(items) - got)}, 정답표에 없음 {sorted(got - set(items))})")
    groups, empty = fabrication_groups(records)
    human = load_fabrication(crit, groups, empty)
    routing_crit = rt.load_criteria()
    rows = rt.build_rows(records, items, routing_crit)
    for r in rows:
        r["hallucinated_detector"] = r["hallucinated"]
        r["hallucinated"] = human[r["eval_id"]]
    sig = crit["verdict"]["signals"]
    ev = rt.evaluate(rows, sig)
    bad = [r for r in rows if rt.is_bad(r)]
    m = {"n": ev["n"], "auto": ev["auto"], "bad_total": ev["bad_total"], "bad_auto": ev["bad_auto"],
         "bad_items": len({r["item_id"] for r in bad}), "bad_auto_items": ev["bad_auto_items"]}
    if problems:
        label, reason = "판정하지 않음", "구성·환경·입력 문제: " + "; ".join(problems)
    else:
        label, reason = verdict(m, crit["verdict"])
    det_vs_human = {"탐지기 날조·사람 날조": sum(r["hallucinated_detector"] and r["hallucinated"] for r in rows),
                    "탐지기 날조·사람 없음": sum(r["hallucinated_detector"] and not r["hallucinated"] for r in rows),
                    "탐지기 없음·사람 날조": sum(not r["hallucinated_detector"] and r["hallucinated"] for r in rows)}
    dev = None
    if DEV_RESULT.exists():
        d = json.loads(DEV_RESULT.read_text(encoding="utf-8"))["selection"]["selected_metrics"]
        dev = {k: d[k] for k in ("auto_rate", "bad_auto", "bad_total", "bad_auto_items", "accuracy_auto")}
    return {"criteria": crit, "verdict": label, "reason": reason, "config_problems": problems, "metrics": m, "selected": ev,
            "baseline": {"R0": rt.evaluate(rows, []), "S1": rt.evaluate(rows, ["S1"])},
            "reference_with_S3": rt.evaluate(rows, sig + ["S3"]), "single_signal": rt.single_signal(rows),
            "fabrication": {"groups": len(groups), "responses_without_summary": len(empty),
                            "human_fabricated_responses": sum(r["hallucinated"] for r in rows), **det_vs_human},
            "dev_reference": dev, "rows": rows}


def render(res: dict) -> str:
    m, ev = res["metrics"], res["selected"]
    sig = "+".join(res["criteria"]["verdict"]["signals"])
    L = ["# v1.5 9단계 — 새 최종 평가용 세트 라우팅 판정 (기계 생성 표)", "",
         "> 계획 [`final_run_v15_plan.md`](final_run_v15_plan.md) · 기준 `final_run_v15.toml` · `src/triage_eval/pipeline/final_v15.py` 생성. "
         "해석과 한계는 결과 보고서 `final_run_v15_results.md`에 적는다.", "",
         f"## 판정: **{res['verdict']}**", "", f"- 이유: {res['reason']}", f"- 신호 집합: {sig} (고정)", ""]
    ra, rt_ = ev["risk_auto"], ev["risk_total"]
    L += ["| 항목 | 값 |", "| :--- | ---: |",
          f"| 응답 수 | {m['n']} |", f"| 자동 처리율(%) | {ev['auto_rate']} ({m['auto']}/{m['n']}) |",
          f"| 위험·날조 응답 (문항) | {m['bad_total']} ({m['bad_items']}) |",
          f"| 위험·날조 통과 (문항) | {m['bad_auto']} ({m['bad_auto_items']}) |",
          f"| 위험 X-1 통과/전체 | {ra['X-1']}/{rt_['X-1']} |", f"| 위험 X-2 통과/전체 | {ra['X-2']}/{rt_['X-2']} |",
          f"| 위험 X-3 통과/전체 | {ra['X-3']}/{rt_['X-3']} |",
          f"| 날조(사람 확인) 통과/전체 | {ev['hallucinated_auto']}/{ev['hallucinated_total']} |",
          f"| 자동 처리분 정확도(%) (보고만) | {ev['accuracy_auto']} |", f"| 전체 정확도(%) (보고만) | {ev['accuracy_all']} |", ""]
    b = res["baseline"]
    L += ["## 기록: 구성별 비교", "", "| 구성 | 자동 처리율(%) | 위험·날조 통과/전체 | 자동 처리분 정확도(%) |", "| :--- | ---: | ---: | ---: |"]
    for name, e in (("R0 전부 자동", b["R0"]), ("S1", b["S1"]), (sig, ev), (sig + "+S3 (참고)", res["reference_with_S3"])):
        L.append(f"| {name} | {e['auto_rate']} | {e['bad_auto']}/{e['bad_total']} | {e['accuracy_auto']} |")
    d = res["dev_reference"]
    if d:
        L += ["", f"개발용 기준선(7단계, {sig}): 자동 처리율 {d['auto_rate']}%, 위험·날조 통과 {d['bad_auto']}/{d['bad_total']}(문항 {d['bad_auto_items']}), "
              f"자동 처리분 정확도 {d['accuracy_auto']}%."]
    L += ["", "## 기록: 신호 하나만 썼을 때", "", "| 신호 | 검토로 보낸 응답 | 위험·날조 응답 중 검토로 보낸 수 |", "| :--- | ---: | ---: |"]
    L += [f"| {s} {rt.SIGNAL_NAMES[s]} | {v['review']} | {v['bad_caught']}/{v['bad_total']} |" for s, v in res["single_signal"].items()]
    f = res["fabrication"]
    L += ["", "## 기록: 날조 사람 확인", "", f"- 확인 묶음 {f['groups']}개, 요약 없는 응답 {f['responses_without_summary']}건(날조 없음으로 계산)",
          f"- 사람이 날조로 본 응답 {f['human_fabricated_responses']}건",
          f"- 탐지기와 비교(참고): 둘 다 날조 {f['탐지기 날조·사람 날조']}건, 탐지기만 {f['탐지기 날조·사람 없음']}건, 사람만 {f['탐지기 없음·사람 날조']}건"]
    missed = [r for r in res["rows"] if not rt.routed(r, res["criteria"]["verdict"]["signals"]) and rt.is_bad(r)]
    L += ["", f"## 기록: 검토 없이 자동 등록된 위험·날조 응답 ({len(missed)}건)", ""]
    if missed:
        L += ["| 응답 | 위험 | 날조(사람) |", "| :--- | :--- | :---: |"]
        L += [f"| {r['eval_id']} | {', '.join(r['risk_miss']) or '—'} | {'있음' if r['hallucinated'] else '—'} |" for r in missed]
    else:
        L.append("없음")
    return "\n".join(L) + "\n"


def cmd_verdict(args) -> int:
    crit = load_criteria()
    guard()
    check_seal(crit)
    res = run_verdict(crit)
    if not args.no_write:   # 설계 의도 9
        out = ROOT / crit["meta"]["result_json"]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (ROOT / crit["meta"]["report"]).write_text(render(res), encoding="utf-8")
    m = res["metrics"]
    print(f"판정: {res['verdict']} — {res['reason']}")
    print(f"  자동 처리율 {res['selected']['auto_rate']}% · 위험·날조 통과 {m['bad_auto']}/{m['bad_total']}")
    if not args.no_write:
        print(f"저장: {crit['meta']['result_json']}, {crit['meta']['report']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="v1.5 9단계 — 새 최종 평가용 세트 라우팅 판정")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="seed 1·11·21 실행")
    r.add_argument("--redo-incomplete", type=int, action="append", help="기록 파일 없이 멈춘 seed 만 다시 실행")
    v = sub.add_parser("verdict", help="날조 확인 결과를 읽어 라우팅·채점을 계산하고 판정")
    v.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        return cmd_run(args) if args.cmd == "run" else cmd_verdict(args)
    except GateError as e:
        print(f"불가: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
