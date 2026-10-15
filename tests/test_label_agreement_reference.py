"""v1.5 라벨 일치율 — 채점자끼리 갈린 문항·v1.5 라벨 참고 일치율 도구 테스트 (tools/label_agreement/reference_v15.py)."""

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "label_agreement"))
import reference_v15 as ref  # noqa: E402

from triage_eval.pipeline import agreement as ag  # noqa: E402


@pytest.fixture(scope="module")
def stored():
    return json.loads(ref.AGREEMENT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def items():
    items14, qmap = ag.load_items(ag.load_criteria())
    by_id15 = {i["id"]: i for i in json.loads(ref.DATASET_V15.read_text(encoding="utf-8"))["items"]}
    return items14, {q: by_id15[i] for q, i in qmap.items()}, qmap


def test_committed_result_is_reproduced(monkeypatch, tmp_path):
    """저장된 판정 결과를 v1.4 라벨로 재현한 뒤 계산하고, 커밋된 결과 파일과 같은 내용을 만든다."""
    out = tmp_path / "reference.json"
    monkeypatch.setattr(ref, "OUT", out)
    assert ref.main([]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(
        (ROOT / "data/label_agreement/reference_v15.json").read_text(encoding="utf-8"))


def _bump_stats(d):
    d["stats"]["채점자1"]["처리"]["allowed_ok"] += 1


def _bump_kappa(d):
    d["between_raters_kappa"]["처리"] = 0.99


def _swap_raters(d):
    d["answers"] = {"채점자2": d["answers"]["채점자2"], "채점자1": d["answers"]["채점자1"]}


def _drop_item(d):
    d["answers"]["채점자1"].pop("Q01")


@pytest.mark.parametrize("tamper", [_bump_stats, _bump_kappa, _swap_raters, _drop_item])
def test_tampered_stored_result_writes_nothing(monkeypatch, tmp_path, stored, tamper):
    """설계 의도 2 — 저장된 판정 결과를 재현하지 못하거나 구성이 다르면 계산하지 않는다."""
    bad = copy.deepcopy(stored)
    tamper(bad)
    src = tmp_path / "agreement.json"
    src.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ref, "AGREEMENT", src)
    monkeypatch.setattr(ref, "OUT", tmp_path / "out.json")
    assert ref.main([]) == 2
    assert not (tmp_path / "out.json").exists()


def test_module_order_does_not_count_as_split():
    assert ref.same_answer("모듈", "네트워크 / 클라이언트 안정성", "클라이언트 안정성 / 네트워크")
    assert not ref.same_answer("모듈", "네트워크", "네트워크 / 클라이언트 안정성")
    assert not ref.same_answer("처리", "폐기", "CS 응대")


def _answers_from_labels(items14):
    """두 채점자가 모두 v1.4 최선 답을 고른 가짜 답."""
    one = {q: {f: ag.best(it, f) for f in ref.JUDGED} | {"모듈1": ag.best(it, "모듈"), "확신도": "높음", "메모": None}
           for q, it in items14.items()}
    return {"채점자1": one, "채점자2": copy.deepcopy(one)}


def test_identical_answers_have_no_split(items):
    items14, items15, qmap = items
    b = ref.between_raters(_answers_from_labels(items14), items14, items15, qmap)
    assert b["items_with_any_split"] == 0 and b["costly_items"] == []


def test_split_is_classified_by_v15_allowed_answers(items):
    """설계 의도 3 — 두 답이 모두 v1.5 허용 답 안이면 '원래 갈리는 판단', 한쪽이라도 밖이면 '기준 적용 차이'."""
    items14, items15, qmap = items
    both = next(q for q, it in items15.items() if len(it["labels"]["처리"]) >= 2)
    gap = next(q for q, it in items15.items() if len(it["labels"]["처리"]) == 1 and q != both)
    answers = _answers_from_labels(items14)
    answers["채점자1"][both]["처리"], answers["채점자2"][both]["처리"] = items15[both]["labels"]["처리"][:2]
    outside = next(v for v in ("폐기", "CS 응대", "개발 배정") if v not in items15[gap]["labels"]["처리"])
    answers["채점자1"][gap]["처리"], answers["채점자2"][gap]["처리"] = items15[gap]["labels"]["처리"][0], outside
    b = ref.between_raters(answers, items14, items15, qmap)
    assert b["costly_both_allowed_v15"]["처리"] == [both]
    assert b["costly_rule_gap_v15"]["처리"] == [gap]
    assert b["costly_both_allowed_items"] == [both]


def test_v15_input_mismatch_is_refused(monkeypatch, tmp_path):
    data = json.loads(ref.DATASET_V15.read_text(encoding="utf-8"))
    data["items"][0]["input"]["body"] += " (수정)"
    fake = tmp_path / "v15.json"
    fake.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ref, "DATASET_V15", fake)
    monkeypatch.setattr(ref, "OUT", tmp_path / "out.json")
    assert ref.main([]) == 2 and not (tmp_path / "out.json").exists()


def test_both_outside_and_non_costly_splits(items):
    """두 답이 모두 허용 답 밖이면 기준 적용 차이, 모듈만 갈리면 비용 큰 칸 목록에 들어가지 않는다."""
    items14, items15, qmap = items
    both_out = next(q for q, it in items15.items() if len(it["labels"]["처리"]) == 1)
    outside = [v for v in ("폐기", "CS 응대", "개발 배정", "정보 요청 후 보류") if v not in items15[both_out]["labels"]["처리"]][:2]
    module_only = next(q for q, it in items15.items() if q != both_out and it["labels"]["모듈"])
    answers = _answers_from_labels(items14)
    answers["채점자1"][both_out]["처리"], answers["채점자2"][both_out]["처리"] = outside
    answers["채점자2"][module_only]["모듈"] = "해당 없음"
    b = ref.between_raters(answers, items14, items15, qmap)
    assert b["costly_rule_gap_v15"]["처리"] == [both_out] and b["costly_both_allowed_items"] == []
    assert module_only not in b["costly_items"] and b["field_counts"]["모듈"] == 1


def test_tool_does_not_read_model_results():
    """설계 의도 5 — 도구 소스에 모델 실행 기록 경로가 없다."""
    src = (ROOT / "tools/label_agreement/reference_v15.py").read_text(encoding="utf-8")
    assert '"results"' not in src and "history" not in src


def test_output_has_no_model_response():
    """설계 의도 5 — 모델 응답을 읽지 않는다."""
    text = (ROOT / "data/label_agreement/reference_v15.json").read_text(encoding="utf-8")
    assert "response_text" not in text and "qwen" not in text
