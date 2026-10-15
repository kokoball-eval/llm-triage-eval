"""v1.5 평가셋(data/eval_v15/aether_raid_v15.json) 검증.

모델을 호출하지 않으므로 CI에서 자동 실행된다.
- 저장소에 올라간 JSON이 생성 도구(tools/dataset_v15/)의 결과와 같은가
- 변경표에 없는 문항과 최종 평가용 25건이 v1.4와 같은가
- 허용 답 추가는 뒤에 덧붙이기만 했는가 (첫 정답을 바꾼 문항은 라벨 변경 2개뿐)
- 변경표의 v1.4 값이 다르거나 최종 평가용 문항을 고치려 하면 생성을 거부하는가
- 모듈 "해당 없음" 명시 허용이 채점 함수와 맞물리는가
"""

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "dataset_v15"))

import build_v15 as m  # noqa: E402
from triage_eval.pipeline.score import JUDGED, field_correct  # noqa: E402

V15 = ROOT / "data" / "eval_v15" / "aether_raid_v15.json"
V14 = ROOT / "data" / "eval_v14" / "aether_raid_v14.json"
RELABELED = {"B17", "S08"}   # 첫 정답이 바뀐 문항 (검토 판정 ③ 라벨 변경)


def load(path):
    return {i["id"]: i for i in json.loads(path.read_text(encoding="utf-8"))["items"]}


def changes():
    return json.loads(m.CHANGES.read_text(encoding="utf-8"))


def test_committed_file_matches_generator():
    built, errors = m.build()
    assert errors == []
    assert json.loads(V15.read_text(encoding="utf-8")) == json.loads(m.v14.dump(built))


def test_untouched_items_equal_v14():
    v14, v15 = load(V14), load(V15)
    assert set(v14) == set(v15)
    touched = {c["id"] for c in changes()["labels"]} | {c["id"] for c in changes()["items"]}
    for i, it in v15.items():
        if i not in touched:
            assert it == v14[i], i
    assert all(v15[i] == v14[i] for i in v15 if v15[i]["split"] == "final")


def test_allowed_answers_only_appended_except_relabeled():
    v14, v15 = load(V14), load(V15)
    for c in changes()["labels"]:
        old, new = v14[c["id"]]["labels"][c["field"]], v15[c["id"]]["labels"][c["field"]]
        if c["id"] in RELABELED:
            continue
        if old:
            assert new[:len(old)] == old, (c["id"], c["field"])
        else:   # 모듈이 없던 문항: "해당 없음"을 첫 정답으로 명시
            assert c["field"] == "모듈" and new[0] == m.NO_MODULE
    first_changed = {c["id"] for c in changes()["labels"]
                     if v14[c["id"]]["labels"][c["field"]] and v14[c["id"]]["labels"][c["field"]][0] != v15[c["id"]]["labels"][c["field"]][0]}
    assert first_changed == RELABELED


def test_risk_counts():
    built, _ = m.build()
    dev = next(r for r in built["split_summary"] if r["split"] == "dev")
    assert (dev["Critical(X-1)"], dev["결함 폐기 금지(X-2)"], dev["사람 검토(X-3)"]) == (17, 12, 10)
    v15 = load(V15)
    assert "X-1" in v15["B17"]["labels"]["risk"]
    assert v15["S08"]["labels"]["risk"] == [] and v15["S08"]["labels"]["human_review"] is False
    assert all("X-1" not in v15[i]["labels"]["risk"] for i in ("S04", "S09", "B12"))


def test_wrong_old_value_is_rejected():
    items = copy.deepcopy(list(load(V14).values()))
    ch = changes()
    ch["labels"][0]["old"] = ["엉뚱한 값"]
    _, errors = m.apply_changes(items, ch)
    assert any("변경표 old" in e for e in errors)


def test_final_item_change_is_rejected():
    items = copy.deepcopy(list(load(V14).values()))
    ch = {"labels": [{"id": "F08", "field": "우선순위", "old": ["Major"], "new": ["Critical"], "basis": "-"}], "items": []}
    _, errors = m.apply_changes(items, ch)
    assert errors == ["F08: 개발용이 아닌 문항은 바꾸지 않음"]


def test_explicit_no_module_scores_as_before():
    """S19: v1.4 허용 답 [] → v1.5 ["해당 없음", "게임플레이·밸런스"]. 기존 정답(해당 없음)은 계속 정답이고 최선 답이다."""
    lab = load(V15)["S19"]["labels"]["모듈"]
    assert field_correct("모듈", "해당 없음", lab) == (True, True)
    assert field_correct("모듈", "게임플레이·밸런스", lab) == (True, False)
    assert field_correct("모듈", "콘텐츠 진행", lab) == (False, False)


def test_no_module_must_be_first():
    it = copy.deepcopy(load(V15)["S19"])
    it["labels"]["모듈"] = ["게임플레이·밸런스", "해당 없음"]
    assert any("첫 정답일 때만" in e for e in m.validate([it]))


def test_every_change_has_basis_and_known_field():
    for c in changes()["labels"]:
        assert c["field"] in JUDGED and c["basis"].startswith("v1.5 검토"), c


def test_x1_items_allow_only_critical():
    it = copy.deepcopy(load(V15)["B17"])
    assert m.validate([it]) == []
    it["labels"]["처리"].append("등록(재현 대기)")
    assert any("X-1 측정 문항은" in e for e in m.validate([it]))
