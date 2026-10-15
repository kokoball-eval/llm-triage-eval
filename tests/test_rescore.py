"""v1.5 라벨 재채점 도구(rescore.py) 테스트 — 계획 report/rescore_v15_plan.md §5 검증 기준.

실제 실행 기록은 v1.4 라벨로만 채점한다(저장된 결과 재현 확인). v1.5 라벨의 동작은 v1.4 평가셋을 메모리에서
고친 가짜 v1.5 라벨로 확인한다. 재채점 결과(v1.5 라벨 값)는 계획 커밋 뒤 도구를 실행해 처음 본다.
"""

import copy
import json

import pytest

from triage_eval.pipeline import compare as cmp
from triage_eval.pipeline import gate as g
from triage_eval.pipeline import gate_v14 as gv
from triage_eval.pipeline import rescore as rs


@pytest.fixture(scope="module")
def d14():
    return rs.load_dataset(rs.DATASET_V14)


@pytest.fixture(scope="module")
def i14(d14):
    return rs.items_of(d14)


@pytest.fixture(scope="module")
def seed1_log():
    crit = gv.load_criteria(gv.CRITERIA)
    return g.load_log(rs.ROOT / crit["meta"]["baseline"][0])


def relabel(items: dict, item_id: str, field: str, allowed: list) -> dict:
    out = copy.deepcopy(items)
    out[item_id]["labels"][field] = allowed
    return out


# ── V0 평가셋 확인 ────────────────────────────────────────────

def test_committed_v15_dataset_passes_v0(d14):
    """커밋된 v1.5 평가셋은 입력·최종 평가용이 v1.4와 같고, 바뀐 라벨이 모두 changed_ids_v15 안에 있다."""
    assert rs.check_datasets(d14, rs.load_dataset(rs.DATASET_V15)) == []


@pytest.mark.parametrize("mutate,expected", [
    (lambda d: d["items"][0]["input"].update(title="바뀐 제목"), "input 이(가) 다릅니다"),
    (lambda d: next(i for i in d["items"] if i["split"] == "final")["labels"].update(처리=["폐기"]), "최종 평가용"),
    (lambda d: next(i for i in d["items"] if i["split"] == "dev")["labels"].update(처리=["폐기"]), "changed_ids_v15 에 없습니다"),
    (lambda d: d["items"].pop(), "문항 ID가 다릅니다"),
    (lambda d: d["items"][0].update(set="focused" if d["items"][0]["set"] != "focused" else "representative"), "set 이(가) 다릅니다"),
    (lambda d: d.update(changed_ids_v15=[next(i["id"] for i in d["items"] if i["split"] == "dev")]), "라벨이 v1.4와 같습니다"),
    (lambda d: d.update(changed_ids_v15=[next(i["id"] for i in d["items"] if i["split"] == "final")]), "개발용이 아닙니다"),
])
def test_v0_rejects_dataset_mismatch(d14, mutate, expected):
    d15 = copy.deepcopy(d14)
    mutate(d15)
    assert any(expected in p for p in rs.check_datasets(d14, d15))


# ── V1 저장된 결과 재현 (실제 실행 기록, v1.4 라벨만) ──────────

def test_v14_labels_reproduce_gate_baseline(i14):
    crit = gv.load_criteria(gv.CRITERIA)
    out, problems = rs.rescore_gate_baseline(crit, g.load_log(rs.GATE_RESULT), i14, i14, set())
    assert problems == []
    assert sorted(out["seeds"]) == [1, 11, 21]
    assert out["total"]["v14"] == out["total"]["v15"] and out["total"]["v14"]["n"] == 3 * 126


@pytest.mark.parametrize("label", ["a", "b"])
def test_v14_labels_reproduce_experiments(i14, label):
    stored = g.load_log(rs.COMPARES[label])
    out, problems = rs.rescore_compare(label, stored, cmp.load_criteria(cmp.CRITERIA), i14, i14, set())
    assert problems == []
    ref = out["reference_rejudge"]
    assert ref["chosen_v14"] == ref["chosen_v15"] == stored["chosen"]


def test_v14_labels_reproduce_final(i14):
    out, problems = rs.rescore_final(g.load_log(rs.FINAL_LOG), g.load_log(rs.FINAL_SCORE), i14, i14, set())
    assert problems == []
    (model,) = out["models"].values()
    assert len(model["note_item"]) == 2   # F08 × 2회 응답을 사실로 기록


def test_tampered_stored_score_fails_v1(i14):
    """저장된 값과 다시 채점한 값이 다르면 도구 결함으로 본다."""
    stored = copy.deepcopy(g.load_log(rs.FINAL_SCORE))
    model = next(iter(stored["summary"]))
    stored["summary"][model]["전체"]["over_escalation"] += 1
    _, problems = rs.rescore_final(g.load_log(rs.FINAL_LOG), stored, i14, i14, set())
    assert any(p.startswith("V1 최종 평가용") for p in problems)


# ── V2·V3 라벨 변경 범위 ──────────────────────────────────────

def _correct_item(log, items, field="처리"):
    """v1.4 라벨에서 이 칸을 맞힌 응답이 있는 개발용 문항 하나."""
    return next(r["item_id"] for r in rs.score_rows(log, items) if r["fields"][field]["ok"])


def test_changed_label_moves_only_that_item(seed1_log, i14):
    item = _correct_item(seed1_log, i14)
    i15 = relabel(i14, item, "처리", ["존재하지 않는 처리"])
    r14, r15 = rs.score_rows(seed1_log, i14), rs.score_rows(seed1_log, i15)
    detail, problems = rs.label_diff(r14, r15, {item})
    assert problems == []
    assert detail["items_changed"] == [item]
    assert detail["fields"]["처리"]["broken"] >= 1 and detail["fields"]["처리"]["fixed"] == 0


def test_change_outside_changed_ids_fails_v3(seed1_log, i14):
    item = _correct_item(seed1_log, i14)
    i15 = relabel(i14, item, "처리", ["존재하지 않는 처리"])
    _, problems = rs.label_diff(rs.score_rows(seed1_log, i14), rs.score_rows(seed1_log, i15), set())
    assert problems and all("라벨이 바뀌지 않은 문항" in p for p in problems)


def test_label_free_values_must_match(seed1_log, i14):
    r14 = rs.score_rows(seed1_log, i14)
    r15 = copy.deepcopy(r14)
    r15[0]["hallucinated"] = not r15[0]["hallucinated"]
    _, problems = rs.label_diff(r14, r15, set())
    assert any("hallucinated" in p for p in problems)


def test_final_label_change_fails_v2(i14):
    log = g.load_log(rs.FINAL_LOG)
    item = _correct_item(log, i14)
    i15 = relabel(i14, item, "처리", ["존재하지 않는 처리"])
    _, problems = rs.rescore_final(log, g.load_log(rs.FINAL_SCORE), i14, i15, {item})
    assert any(p.startswith("V2 최종 평가용") for p in problems)


def test_risk_change_is_listed(seed1_log, i14):
    """위험 표시가 바뀌면 측정 응답 수(분모)와 위험 건 목록이 함께 바뀐다."""
    item = next(r["item_id"] for r in rs.score_rows(seed1_log, i14)
                if "X-1" not in i14[r["item_id"]]["labels"]["risk"] and r["fields"]["처리"]["predicted"] != "긴급 사인 요청")
    i15 = relabel(i14, item, "risk", i14[item]["labels"]["risk"] + ["X-1"])
    r14, r15 = rs.score_rows(seed1_log, i14), rs.score_rows(seed1_log, i15)
    detail, problems = rs.label_diff(r14, r15, {item})
    assert problems == []
    assert detail["risk_changes"] and all(c["item_id"] == item and "X-1" in c["v15"] for c in detail["risk_changes"])
    assert rs.aggregate(r15, i15)["risk_items"]["X-1"] > rs.aggregate(r14, i14)["risk_items"]["X-1"]


# ── 실행 전체 ────────────────────────────────────────────────

def test_run_writes_new_files_only(monkeypatch, tmp_path):
    """v1.5 자리에 v1.4 평가셋을 넣으면 모든 값이 같고, 결과는 지정한 새 경로에만 쓴다."""
    monkeypatch.setattr(rs, "DATASET_V15", rs.DATASET_V14)
    monkeypatch.setattr(rs, "OUT_JSON", tmp_path / "v15" / "rescore_v15.json")
    monkeypatch.setattr(rs, "REPORT", tmp_path / "rescore_v15.md")
    assert rs.main([]) == 0
    result = json.loads((tmp_path / "v15" / "rescore_v15.json").read_text(encoding="utf-8"))
    assert result["adopted"]["total"]["v14"] == result["adopted"]["total"]["v15"]
    for e in result["experiments"].values():
        assert e["reference_rejudge"]["chosen_v14"] == e["reference_rejudge"]["chosen_v15"]
    report = (tmp_path / "rescore_v15.md").read_text(encoding="utf-8")
    assert "참고 재판정" in report and "F08 주의" in report


def test_run_with_changed_labels_reports_differences(monkeypatch, tmp_path, d14, seed1_log, i14):
    """가짜 v1.5(문항 하나의 처리 정답 변경, 다른 문항 하나에 X-1 추가)로 끝까지 돌리면 증감과 seed별 위험 건 표가 나온다."""
    fixed = _correct_item(seed1_log, i14)
    risky = next(r["item_id"] for r in rs.score_rows(seed1_log, i14)
                 if r["item_id"] != fixed and "X-1" not in i14[r["item_id"]]["labels"]["risk"]
                 and r["fields"]["처리"]["predicted"] != "긴급 사인 요청")
    d15 = copy.deepcopy(d14)
    for it in d15["items"]:
        if it["id"] == fixed:
            it["labels"]["처리"] = ["존재하지 않는 처리"]
        if it["id"] == risky:
            it["labels"]["risk"] = it["labels"]["risk"] + ["X-1"]
    d15["changed_ids_v15"] = sorted({fixed, risky})
    fake = tmp_path / "v15.json"
    fake.write_text(json.dumps(d15, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rs, "DATASET_V15", fake)
    monkeypatch.setattr(rs, "OUT_JSON", tmp_path / "out.json")
    monkeypatch.setattr(rs, "REPORT", tmp_path / "out.md")
    assert rs.main([]) == 0
    result = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    total = result["adopted"]["total"]
    assert set(total["diff"]["items_changed"]) <= {fixed, risky}
    assert total["diff"]["fields"]["처리"]["broken"] >= 1
    assert sorted({c["seed"] for c in total["diff"]["risk_changes"]}) == [1, 11, 21]
    report = (tmp_path / "out.md").read_text(encoding="utf-8")
    assert "| seed | 응답 | v1.4 위험 건 |" in report and " → " in report and "(-" in report


def test_failed_verification_writes_nothing(monkeypatch, tmp_path, capsys):
    stored = g.load_log(rs.FINAL_SCORE)
    stored["per_response"][0]["over_escalation"] = not stored["per_response"][0]["over_escalation"]
    bad = tmp_path / "score.json"
    bad.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rs, "FINAL_SCORE", bad)
    monkeypatch.setattr(rs, "DATASET_V15", rs.DATASET_V14)   # 실제 v1.5 라벨은 쓰지 않는다 (파일 설명)
    monkeypatch.setattr(rs, "OUT_JSON", tmp_path / "out.json")
    monkeypatch.setattr(rs, "REPORT", tmp_path / "out.md")
    assert rs.main([]) == 2
    assert "V1 최종 평가용" in capsys.readouterr().out
    assert not (tmp_path / "out.json").exists() and not (tmp_path / "out.md").exists()
