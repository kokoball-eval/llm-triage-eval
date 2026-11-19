"""평가셋 실행기(evaluation/runner.py)와 성능표(evaluation/performance.py) 시험 — 가짜 모델로 실행한다."""

import glob
import json

import pytest

from conftest import ROOT, FakeClient
from triage_eval.evaluation import gate as g
from triage_eval.evaluation import performance, runner


class UnloadRecordingClient(FakeClient):
    """모델을 내리는 호출(keep_alive=0)과 일반 호출의 순서를 기록한다."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = []

    def generate(self, model, prompt, options=None, think=None, keep_alive=None, format=None):
        self.calls.append("unload" if keep_alive == 0 else "generate")
        assert keep_alive == 0 or think is False   # config.THINK
        return super().generate(model, prompt, options, think, keep_alive, format)


@pytest.fixture
def run_fake(monkeypatch, tmp_path):
    def _run(client=None, args=()):
        client = client or FakeClient()
        monkeypatch.setattr(runner.llm, "client", lambda: client)
        monkeypatch.setattr(runner, "snapshot", lambda c: {"warnings": [], "notes": []})
        assert runner.main(["--seed", "1", "--out-dir", str(tmp_path), *args]) == 0
        (log,) = tmp_path.glob("run_*.json")
        return json.loads(log.read_text(encoding="utf-8")), log
    return _run


def test_final_split_needs_final_flag():
    """설계 의도 1 — 최종 평가용 문항은 --final 없이 실행되지 않는다(모델을 부르기 전에 막힌다)."""
    assert runner.blocked_split("final", final=False) and runner.blocked_split("all", final=False)
    assert runner.blocked_split("final", final=True) is None and runner.blocked_split("dev", final=False) is None
    assert runner.main(["--split", "final"]) == 2


def test_seed_per_run():
    assert runner.options_for(1, 2)["seed"] == 2 and "seed" not in runner.OPTIONS


def test_run_writes_log_comparable_with_gate_baseline(run_fake):
    """새 실행기가 남기는 실행 조건이 게이트 기준선과 같은 값이어야 기준선과 비교할 수 있다 (설계 의도 5)."""
    log, _ = run_fake()
    cfg = log["metadata"]["run_config"]
    base = json.loads((ROOT / "data/results/v14/baseline/v14_dev_m2_seed1_baseline.json").read_text(encoding="utf-8"))
    base_cfg = base["metadata"]["run_config"]
    for key, _label in g.SAME_DATA + g.CHANGE_NOTES:
        assert cfg[key] == base_cfg[key], key
    assert cfg["seeds"] == base_cfg["seeds"] == {"1": 1, "2": 2}
    assert len(log["results"]) == 126 and all(r["split"] == "dev" for r in log["results"])


def test_run_unloads_model_before_and_after(run_fake):
    """설계 의도 2 — 시작 전과 마지막 응답 뒤에 모델을 내린다."""
    client = UnloadRecordingClient()
    run_fake(client)
    assert client.calls[0] == "unload" and client.calls[-1] == "unload"
    assert client.calls.count("unload") == 2


def test_run_then_score_end_to_end(run_fake):
    """가짜 모델이 v1.4 정답 라벨로 답하면, 같은 라벨로 채점한 성능표가 만점이고 위험 누락이 없다."""
    _, path = run_fake()
    rows, s = performance.score_logs([path], ROOT / "data/eval_v14/aether_raid_v14.json")
    assert s["n"] == 126 and s["all_fields_ok"] == 126
    assert s["risk_miss"] == {"X-1": 0, "X-2": 0, "X-3": 0}


def test_performance_reproduces_final_run_scoring():
    """새 최종 평가용 세트 실행(360응답)의 응답별 채점이 9단계 판정 때와 같다 — 위험 누락·칸 정답·날조 탐지."""
    logs = sorted(glob.glob(str(ROOT / "data/results/v15_final/history/*.json")))
    rows, s = performance.score_logs(logs, ROOT / "data/eval_v15_final/aether_raid_v15_final.json")
    old = {(r["seed"], r["item_id"], r["run"]): r
           for r in json.loads((ROOT / "data/results/v15_final/routing_final_v15.json").read_text(encoding="utf-8"))["rows"]}
    assert len(rows) == len(old) == 360
    for r in rows:
        o = old[(r["seed"], r["item_id"], r["run_index"])]
        assert r["risk_miss"] == o["risk_miss"]
        assert {f: r["fields"][f]["ok"] for f in o["fields_ok"]} == o["fields_ok"]
        assert r["hallucinated"] == o["hallucinated_detector"]
    assert s["risk_miss"] == {"X-1": 19, "X-2": 0, "X-3": 15} and s["hallucinated"] == 0


def test_performance_refuses_labels_without_the_item():
    """설계 의도 3 — 다른 평가셋의 정답표로 채점하지 않는다."""
    with pytest.raises(SystemExit):
        performance.score_logs([ROOT / "data/results/v14/baseline/v14_dev_m2_seed1_baseline.json"],
                               ROOT / "data/eval_v15_final/aether_raid_v15_final.json")


def test_missing_dataset_file_is_reported(capsys):
    assert runner.main(["--dataset", "v14"]) == 2
    assert "평가셋 파일이 없습니다" in capsys.readouterr().out
