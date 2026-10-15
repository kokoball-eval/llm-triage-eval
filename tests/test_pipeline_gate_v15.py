"""채택 구성 회귀 게이트 v1.5 라벨(gate_v15.py, triage-gate --dataset v15) 테스트.

계획 report/regression_gate_v15_plan.md. 실제 실행 기록으로 한 재현 점검(G1·G2)은 계획 커밋 뒤 도구로 실행하고,
그 결과를 회귀 테스트로 고정한다(계획 §5). 이 파일은 기준 파일, 안전장치, v1.4 게이트 불변(G3),
가짜 모델 실행 기록으로 한 판정만 다룬다.
"""

import copy
import json
import tomllib
from pathlib import Path

import pytest

from test_pipeline_gate_v14 import EXP_B, FINAL, M0, SEEDS, fake_crit, fake_runs, write
from test_pipeline_methods import FakeClient
from triage_eval.pipeline import gate as g
from triage_eval.pipeline import gate_v14 as gv
from triage_eval.pipeline import gate_v15 as g15
from triage_eval.pipeline import rescore as rs

ROOT = Path(__file__).resolve().parent.parent
H = ROOT / "data/results/v14/history"
GATE_V14_CANDIDATES = [H / "v14_dev_m2_20261007_113808.json", H / "v14_dev_m2_20261008_212316.json",
                       H / "v14_dev_m2_20261008_212807.json"]   # v1.4 게이트 PASS 판정의 후보 (gate_result_v14.json)


@pytest.fixture
def crit():
    return g15.load_criteria(g15.CRITERIA)


# ── 기준 파일 ────────────────────────────────────────────────

def test_criteria_values_equal_v14_except_labels(crit):
    """계획 §3 — 기준 값은 v1.4 게이트와 같고, [meta] labels 하나만 더했다."""
    v14 = tomllib.loads(gv.CRITERIA.read_text(encoding="utf-8"))
    v15 = tomllib.loads(g15.CRITERIA.read_text(encoding="utf-8"))
    assert v15["meta"].pop("labels") == "v15"
    assert v15 == v14


def test_wrong_labels_value_is_rejected(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text(g15.CRITERIA.read_text(encoding="utf-8").replace('labels = "v15"', 'labels = "v14"'), encoding="utf-8")
    with pytest.raises(g.GateError, match="labels"):
        g15.load_criteria(bad)


def test_v14_loader_rejects_v15_file():
    """v1.4 게이트에 v1.5 기준 파일을 넣으면 모르는 키(labels)로 거부한다 — 두 게이트가 섞이지 않는다."""
    with pytest.raises(g.GateError, match="labels"):
        gv.load_criteria(g15.CRITERIA)


def test_unknown_key_is_rejected(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text(g15.CRITERIA.read_text(encoding="utf-8") + "\n[absolute2]\nx = 1\n", encoding="utf-8")
    with pytest.raises(g.GateError, match="모르는 섹션"):
        g15.load_criteria(bad)


# ── 안전장치 (계획 §4) ───────────────────────────────────────

def test_labels_with_different_input_are_refused(monkeypatch, tmp_path, crit):
    labels = rs.load_dataset(g15.LABELS)
    labels["items"][0]["input"]["body"] += " (수정)"
    fake = tmp_path / "labels.json"
    fake.write_text(json.dumps(labels, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(g15, "LABELS", fake)
    with pytest.raises(g.GateError, match="정답표"):
        g15.run(GATE_V14_CANDIDATES, crit, write=False)


def test_run_with_other_dataset_fingerprint_is_refused(tmp_path, crit):
    """기준선과 후보의 지문이 같아도, 지금의 v1.4 평가셋과 다르면 판정하지 않는다 (설계 의도 4)."""
    paths = []
    for s, p in zip(SEEDS, GATE_V14_CANDIDATES):
        log = g.load_log(p)
        log["metadata"]["run_config"]["dataset_sha256"] = "다른 평가셋"
        paths.append(write(tmp_path, f"c{s}.json", log))
    with pytest.raises(g.GateError, match="평가셋 지문"):
        g15.run(paths, crit, write=False)


def test_v14_gate_refusals_still_apply(crit):
    with pytest.raises(g.GateError, match="seed 구성"):
        g15.run(GATE_V14_CANDIDATES[:2], crit, write=False)
    with pytest.raises(g.GateError, match="개발용이 아닌"):
        g15.run([FINAL] + EXP_B[1:], crit, write=False)
    with pytest.raises(g.GateError, match="후보 실행끼리"):
        g15.run([M0[0]] + EXP_B[1:], crit, write=False)


def test_cli_dispatch():
    """triage-gate --dataset v15 는 v1.5 라벨 게이트로 (gate.py 설계 의도 8)."""
    assert g.main(["--dataset", "v15", "--candidate", str(GATE_V14_CANDIDATES[0])]) == 2   # seed 누락
    assert g.main(["--dataset", "v15"]) == 2                                               # 후보 없음


# ── G3 v1.4 게이트 불변 ──────────────────────────────────────

def test_v14_gate_output_unchanged(tmp_path):
    """매개변수화 뒤에도 v1.4 게이트가 커밋된 판정 결과·보고서와 같은 파일을 만든다 (gate_v14.py 설계 의도 8)."""
    out_json, out_md = tmp_path / "gate.json", tmp_path / "gate.md"
    assert gv.run(GATE_V14_CANDIDATES, gv.load_criteria(gv.CRITERIA), result_json=out_json, report=out_md) == 0
    assert json.loads(out_json.read_text(encoding="utf-8")) == g.load_log(gv.RESULT_JSON)
    committed = gv.REPORT.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert out_md.read_text(encoding="utf-8").replace("\r\n", "\n") == committed


# ── 가짜 모델로 실행 → 게이트 ────────────────────────────────

def test_fake_pipeline_identical_runs_pass(monkeypatch, tmp_path, crit):
    base, cand = fake_runs(monkeypatch, tmp_path), fake_runs(monkeypatch, tmp_path)
    assert g15.run(cand, fake_crit(crit), base_paths=base, write=False) == 0


def test_fake_pipeline_missed_critical_fails(monkeypatch, tmp_path, crit):
    """v1.5에서도 Critical 측정 문항인 S04b를 놓치면 FAIL."""
    base = fake_runs(monkeypatch, tmp_path)
    miss = {"S04b": {"우선순위": "Major", "처리": "등록(재현 대기)"}}
    cand = fake_runs(monkeypatch, tmp_path, lambda: FakeClient(override=miss))
    assert g15.run(cand, fake_crit(crit), base_paths=base, write=False) == 1


def test_v15_labels_are_used(monkeypatch, tmp_path, crit):
    """B17은 v1.4 정답이 Major·개발 배정, v1.5 정답이 Critical·긴급 사인 요청이다. 기준선이 v1.4 정답, 후보가 v1.5 정답으로
    답하면 v1.4 게이트는 처리 하락으로 FAIL, v1.5 게이트는 Critical을 잡았으므로 PASS여야 한다."""
    base = fake_runs(monkeypatch, tmp_path)
    to_v15 = {"B17": {"우선순위": "Critical", "처리": "긴급 사인 요청"}}
    cand = fake_runs(monkeypatch, tmp_path, lambda: FakeClient(override=to_v15))
    v14_crit = copy.deepcopy(gv.load_criteria(gv.CRITERIA))
    assert gv.run(cand, fake_crit(v14_crit), base_paths=base, write=False) == 1
    assert g15.run(cand, fake_crit(crit), base_paths=base, write=False) == 0


# ── 검토 반영 ────────────────────────────────────────────────

def test_v14_labels_file_is_refused(monkeypatch, crit):
    """정답표가 v1.4 파일을 가리키면 입력 검사(V0)는 통과하지만, v1.5 정답표가 아니므로 판정하지 않는다."""
    monkeypatch.setattr(g15, "LABELS", g15.INPUTS)
    with pytest.raises(g.GateError, match="v1.5 평가셋이 아닙니다"):
        g15.run(GATE_V14_CANDIDATES, crit, write=False)


def test_baseline_fingerprint_is_checked(tmp_path, crit):
    """후보가 정상이어도 기준선(--baseline 지정 포함)의 평가셋 지문이 다르면 판정하지 않는다."""
    base = []
    for s, p in zip(SEEDS, crit["meta"]["baseline"]):
        log = g.load_log(ROOT / p)
        log["metadata"]["run_config"]["dataset_sha256"] = "다른 평가셋"
        base.append(write(tmp_path, f"b{s}.json", log))
    with pytest.raises(g.GateError, match="평가셋 지문"):
        g15.run(GATE_V14_CANDIDATES, crit, base_paths=base, write=False)


def _fake_write(monkeypatch, tmp_path, crit, cand, base):
    monkeypatch.setattr(g15, "RESULT_JSON", tmp_path / "out" / "gate.json")
    monkeypatch.setattr(g15, "REPORT", tmp_path / "out" / "gate.md")
    code = g15.run(cand, fake_crit(crit), base_paths=base, write=True)
    return code, json.loads((tmp_path / "out" / "gate.json").read_text(encoding="utf-8"))


def test_missed_critical_fails_on_x1(monkeypatch, tmp_path, crit):
    """S04b 누락의 FAIL 이유에 X-1 Critical 미상신이 들어 있다 (처리 하락만으로 FAIL 난 것이 아님)."""
    base = fake_runs(monkeypatch, tmp_path)
    miss = {"S04b": {"우선순위": "Major", "처리": "등록(재현 대기)"}}
    cand = fake_runs(monkeypatch, tmp_path, lambda: FakeClient(override=miss))
    code, result = _fake_write(monkeypatch, tmp_path, crit, cand, base)
    assert code == 1
    failed = {c["name"] for s in result["per_seed"] for c in s["checks"] if not c["passed"]}
    assert "X-1 Critical 미상신" in failed


def test_result_records_labels_and_response_identity(monkeypatch, tmp_path, crit):
    """설계 의도 6 — 판정 결과에 정답표 출처와 응답 동일성(G0)이 남고, 다른 응답은 문항·칸까지 적힌다."""
    base = fake_runs(monkeypatch, tmp_path)
    cand = fake_runs(monkeypatch, tmp_path, lambda: FakeClient(override={"B17": {"우선순위": "Critical", "처리": "긴급 사인 요청"}}))
    code, result = _fake_write(monkeypatch, tmp_path, crit, cand, base)
    assert code == 0
    assert result["labels"]["version"] == "v1.5" and result["labels"]["path"].endswith("aether_raid_v15.json")
    for s in SEEDS:
        ident = result["response_identity"][str(s)]
        assert ident["compared"] == 126 and ident["different"] == 2
        assert {d["item_id"] for d in ident["items"]} == {"B17"}
        assert all(set(d["fields"]) == {"우선순위", "처리"} for d in ident["items"])
    assert "응답 동일성 (계획 §5 G0" in (tmp_path / "out" / "gate.md").read_text(encoding="utf-8")


def test_identical_runs_have_no_response_difference(monkeypatch, tmp_path, crit):
    base, cand = fake_runs(monkeypatch, tmp_path), fake_runs(monkeypatch, tmp_path)
    ident = g15.response_identity(base, cand, fake_crit(crit))
    assert all(v["different"] == 0 and v["compared"] == 126 for v in ident.values())
