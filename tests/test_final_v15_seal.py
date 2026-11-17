"""v1.5 새 최종 평가용 세트 봉인(tools/dataset_v15_final/seal_final_v15.py) 정합성 시험.

모델을 호출하지 않으므로 CI에서 자동 실행된다. 봉인 뒤 평가셋·승인 기록이 한 글자라도 바뀌면 여기서 실패한다.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "dataset_v15_final"))

import review_server as rs  # noqa: E402
import seal_final_v15 as seal  # noqa: E402
import validate as v  # noqa: E402


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_sealed_files_match_record():
    """해시·문항 수·검증이 봉인 기록과 같다 — 봉인 파일을 고치면 실패한다."""
    assert seal.verify() == []


def test_sealed_record_contents():
    rec = load(seal.SEALED)
    assert rec["dataset"]["items"] == 60 and len(rec["dataset"]["sha256"]) == 64
    assert rec["counts"]["track"] == {"A": 40, "B": 20}
    assert rec["counts"]["risk"] == {"X-1": 16, "X-2": 4, "X-3": 10, "None": 30}
    assert rec["similarity_check"]["max_ratio"] < rec["similarity_check"]["limit"]
    assert rec["review_record"]["approved"] == 60
    assert set(rec["guideline_different_judgments"]) == {"NA16", "NA18", "NB11"}
    assert rec["limits"]


def test_sealed_items_are_the_approved_draft_items():
    """봉인본의 입력·라벨·근거는 승인된 초안과 같고, 승인 해시와 일치한다."""
    draft, final = load(seal.DRAFT)["items"], load(seal.FINAL)["items"]
    answers = load(seal.ANSWERS)["answers"]
    assert [i["id"] for i in draft] == [i["id"] for i in final]
    for d, f in zip(draft, final):
        assert answers[d["id"]]["결정"] == "승인" and answers[d["id"]]["hash"] == rs.item_hash(d)
        assert {k: d[k] for k in ("input", "labels", "rationale")} == {k: f[k] for k in ("input", "labels", "rationale")}
        assert f["review"] == "approved" and f["split"] == "final"


def test_sealed_set_has_no_model_output_fields():
    for it in load(seal.FINAL)["items"]:
        assert not {"response", "responses", "model", "prediction", "output"} & set(it)
    assert load(seal.FINAL)["status"].startswith("sealed")


def test_seal_refuses_to_overwrite():
    with pytest.raises(SystemExit):
        seal.seal()


def test_validator_review_state_parameter():
    items = load(seal.FINAL)["items"]
    assert v.validate(items, v.existing_items(), full=True, review="approved")[0] == []
    assert any("규칙" in p for p in v.check_item(items[0]))          # 초안 규칙(pending)으로 보면 걸린다
    assert v.check_item(items[0], review="approved") == []


def test_tampering_is_detected(tmp_path, monkeypatch):
    """평가셋 복사본의 라벨 한 칸만 바꿔도 해시 불일치를 잡는다."""
    items = load(seal.FINAL)
    items["items"][0]["labels"]["우선순위"] = ["Trivial"]
    copy = tmp_path / "final.json"
    copy.write_text(json.dumps(items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    monkeypatch.setattr(seal, "FINAL", copy)
    assert any("SHA-256" in p for p in seal.verify())


# 봉인 기록과 별개로 코드에 고정한 값 — 봉인 기록과 평가셋을 함께 고쳐도 이 시험이 막는다
PINNED_DATASET_SHA256 = "418ea37d7448e002c934b172877b190151ed868fe696b9ec7536cca44ac729d3"
PINNED_ANSWERS_SHA256 = "1bf66fd0997c4925e98b147d1df45824656f1e86b70e3779d42439cf5d60ba2b"


def test_hashes_are_pinned_outside_the_seal_record():
    assert seal.sha256_of(seal.FINAL) == PINNED_DATASET_SHA256
    assert seal.sha256_of(seal.ANSWERS) == PINNED_ANSWERS_SHA256


def test_hash_ignores_line_endings(tmp_path):
    crlf = tmp_path / "crlf.json"
    crlf.write_bytes(seal.FINAL.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert seal.sha256_of(crlf) == PINNED_DATASET_SHA256


def test_tampered_input_is_detected(tmp_path, monkeypatch):
    doc = load(seal.FINAL)
    doc["items"][1]["input"]["body"] += " "
    copy = tmp_path / "final.json"
    copy.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    monkeypatch.setattr(seal, "FINAL", copy)
    assert any("SHA-256" in p for p in seal.verify())
