"""회귀 게이트(evaluation/gate.py) 시험 — 저장된 실제 실행 기록으로 판정을 고정한다. 모델은 호출하지 않는다."""

import copy
import json
from pathlib import Path

import pytest

from triage_eval.config import GATE_CRITERIA, ROOT
from triage_eval.evaluation import gate as g

COMMITTED = json.loads((ROOT / "data/results/v15/gate_result_v15.json").read_text(encoding="utf-8"))
CANDIDATES = [ROOT / p for p in COMMITTED["candidate"]]


@pytest.fixture(scope="module")
def crit():
    return g.load_criteria()


@pytest.fixture(scope="module")
def baselines(crit):
    return [ROOT / p for p in crit["meta"]["baseline"]]


def test_committed_gate_result_is_reproduced(crit):
    """구조 정리 전 v1.5 게이트가 낸 판정(seed별 기준 값·문항 단위 변화)을 새 게이트가 그대로 낸다."""
    result = json.loads(json.dumps(g.evaluate(CANDIDATES, crit), ensure_ascii=False))
    assert result["verdict"] == COMMITTED["verdict"] == "PASS"
    assert result["per_seed"] == COMMITTED["per_seed"]
    assert result["item_change_totals"] == COMMITTED["item_change_totals"]
    assert result["notes"] == []


def test_baseline_against_itself_passes_with_no_change(crit, baselines):
    result = g.evaluate(baselines, crit)
    assert result["verdict"] == "PASS"
    assert all(not t["regressed"] and not t["fixed"] for t in result["item_change_totals"].values())


def _write(tmp_path: Path, name: str, payload) -> Path:
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _changed(tmp_path, baselines, edit) -> list[Path]:
    """기준선 3건을 복사해 edit(seed, log)로 고친 후보."""
    out = []
    for path in baselines:
        log = copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
        edit(g.seed_of(log), log)
        out.append(_write(tmp_path, path.name, log))
    return out


def test_missed_critical_fails_on_x1(crit, baselines, tmp_path):
    """Critical 문항 하나를 개발 배정으로 바꾸면 그 seed 가 X-1 회귀로 FAIL."""
    labels = {i["id"]: i for i in json.loads(g.DEV_LABELS.read_text(encoding="utf-8"))["items"]}

    def edit(seed, log):
        if seed != 11:
            return
        r = next(r for r in log["results"] if "X-1" in labels[r["item_id"]]["labels"]["risk"]
                 and "[처리]: 긴급 사인 요청" in r["response_text"])
        r["response_text"] = r["response_text"].replace("[처리]: 긴급 사인 요청", "[처리]: 개발 배정")

    result = g.evaluate(_changed(tmp_path, baselines, edit), crit)
    assert result["verdict"] == "FAIL"
    failed = [c["name"] for c in result["per_seed"][1]["checks"] if not c["passed"]]
    assert "X-1 Critical 미상신" in failed
    assert result["per_seed"][0]["passed"] and result["per_seed"][2]["passed"]


def test_prompt_change_is_reported_not_refused(crit, baselines, tmp_path):
    def edit(seed, log):
        log["metadata"]["run_config"]["system_prompt_sha256"] = "changed"
    result = g.evaluate(_changed(tmp_path, baselines, edit), crit)
    assert result["notes"] == ["프롬프트 변경됨 (이 게이트가 판정하려는 변경)"]


@pytest.mark.parametrize("key, value, message", [
    ("dataset_sha256", "other", "평가셋 지문"),
    ("options", {"temperature": 1.0}, "생성 옵션"),
    ("repeat_count", 1, "반복 횟수"),
])
def test_incomparable_runs_are_refused(crit, baselines, tmp_path, key, value, message):
    def edit(seed, log):
        log["metadata"]["run_config"][key] = value
    with pytest.raises(g.GateError, match=message):
        g.evaluate(_changed(tmp_path, baselines, edit), crit)


def test_model_reload_during_run_is_refused(crit, baselines, tmp_path):
    """실행 도중 모델이 다시 로드되면 그 뒤 응답은 같은 seed로 재현되지 않는다 — 측정 환경 오염."""
    def edit(seed, log):
        if seed == 21:
            log["results"][40]["load_duration_sec"] = 8.2
    with pytest.raises(g.GateError, match="측정 환경 오염"):
        g.evaluate(_changed(tmp_path, baselines, edit), crit)


def test_missing_seed_is_refused(crit, baselines):
    with pytest.raises(g.GateError, match="seed 구성"):
        g.evaluate(baselines[:2], crit)


def test_labels_for_other_inputs_are_refused(crit, tmp_path):
    labels = json.loads(g.DEV_LABELS.read_text(encoding="utf-8"))
    labels["items"][0]["input"]["body"] += " (수정)"
    with pytest.raises(g.GateError, match="정답표"):
        g.evaluate(CANDIDATES, crit, labels_path=_write(tmp_path, "labels.json", labels))


def test_criteria_typo_is_refused(tmp_path):
    text = GATE_CRITERIA.read_text(encoding="utf-8").replace("hallucination_max", "halucination_max")
    with pytest.raises(g.GateError, match="halucination_max"):
        g.load_criteria(_write(tmp_path, "criteria.toml", text))


def test_cli_exit_codes(capsys):
    args = sum((["--candidate", str(p)] for p in CANDIDATES), []) + ["--no-write"]
    assert g.main(args) == 0 and "PASS" in capsys.readouterr().out
    assert g.main(["--candidate", str(CANDIDATES[0]), "--no-write"]) == 2


def test_older_labels_file_is_refused(crit, tmp_path):
    """설계 의도 6 — 입력이 같아도 기준 파일이 정한 판(v1.5)이 아닌 정답표로는 판정하지 않는다."""
    labels = json.loads(g.DEV_LABELS.read_text(encoding="utf-8"))
    labels["version"] = "v1.4"
    with pytest.raises(g.GateError, match="정답표 판"):
        g.evaluate(CANDIDATES, crit, labels_path=_write(tmp_path, "labels.json", labels))


def test_result_records_labels_fingerprint(crit):
    result = g.evaluate(CANDIDATES, crit)
    assert result["labels"] == COMMITTED["labels"]


def test_real_method_change_fails_and_is_reported(crit):
    """체크리스트 없이(M0) 실행한 실제 기록 3건 — 방법 변경으로 알리고, Critical 을 더 놓쳐 FAIL 이다."""
    m0 = sorted((ROOT / "data/results/v14/history").glob("v14_dev_m0_*.json"))
    result = g.evaluate(m0, crit)
    assert result["verdict"] == "FAIL"
    assert any("방법 변경됨" in n for n in result["notes"])
    assert any(not c["passed"] and c["name"] == "X-1 Critical 미상신" for r in result["per_seed"] for c in r["checks"])
