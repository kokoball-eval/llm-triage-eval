"""채점(evaluation/score.py) 시험 — 정답 라벨로 만든 응답과 일부러 틀린 응답으로 확인한다."""

import json
from pathlib import Path

from triage_eval.evaluation.score import field_correct, score_one, summarize

ROOT = Path(__file__).resolve().parent.parent
ITEMS = {i["id"]: i for i in json.loads((ROOT / "data/eval_v15/aether_raid_v15.json").read_text(encoding="utf-8"))["items"]}


def ideal(item, **override):
    """정답 라벨의 첫 값(최선 답)으로 응답을 만든다."""
    lb = item["labels"]
    v = {f: lb[f][0] if lb[f] else "해당 없음" for f in ["분류", "모듈", "우선순위", "재현 정보", "발생 빈도", "처리"]}
    v.update(override)
    note = " / ".join(lb.get("must_request", []) + lb.get("must_recommend", [])) or "없음"
    return "\n".join([f"[요약]: {item['input']['title']}"] + [f"[{k}]: {x}" for k, x in v.items()]
                     + [f"[누락 정보 및 권장 조치]: {note}"])


def rec(item, text, model="m"):
    return {"eval_id": f"{model}_{item['id']}", "model": model, "run_index": 1, "item_id": item["id"],
            "success": True, "response_text": text, "elapsed_sec": 1.0, "prompt_eval_count": 1000}


def test_ideal_responses_score_perfectly_on_all_items():
    """정답 라벨의 최선 답으로 만든 응답은 모든 문항(개발용 63 + 최종 25)에서 만점이고 위험 누락이 없다."""
    rows = [score_one(rec(i, ideal(i)), i) for i in ITEMS.values()]
    s = summarize(rows, ITEMS)
    assert s["n"] == 88 and s["all_fields_ok"] == 88 and s["strict"] == 88
    assert s["risk_miss"] == {"X-1": 0, "X-2": 0, "X-3": 0} and s["over_escalation"] == 0
    assert s["required_coverage_pct"] == 100.0


def test_x1_miss_when_critical_not_sent_to_sign_request():
    item = ITEMS["B01"]  # Critical, 긴급 사인 요청
    r = score_one(rec(item, ideal(item, 처리="개발 배정")), item)
    assert r["risk_miss"] == ["X-1"]


def test_x2_miss_when_defect_discarded():
    item = ITEMS["A02"]  # 단문 크래시 — 결함, 정보 요청 후 보류
    assert "X-2" in score_one(rec(item, ideal(item, 처리="폐기")), item)["risk_miss"]


def test_x3_miss_when_boundary_case_closed():
    item = ITEMS["A20"]  # 경계 P-B4, 사람 검토 필요
    assert "X-3" in score_one(rec(item, ideal(item, 처리="폐기")), item)["risk_miss"]
    assert score_one(rec(item, ideal(item, 처리="정보 요청 후 보류")), item)["risk_miss"] == []


def test_over_escalation_counted():
    item = ITEMS["B10"]  # Trivial 오타
    r = score_one(rec(item, ideal(item, 처리="긴급 사인 요청", 우선순위="Critical")), item)
    assert r["over_escalation"] and r["risk_miss"] == []


def test_module_rules():
    assert field_correct("모듈", "해당 없음", []) == (True, True)
    assert field_correct("모듈", "결제·재화", []) == (False, False)
    assert field_correct("모듈", "플랫폼 호환성 / UI·텍스트", ["플랫폼 호환성", "UI·텍스트"]) == (True, True)
    assert field_correct("모듈", "UI·텍스트", ["플랫폼 호환성", "UI·텍스트"]) == (True, False)
    assert field_correct("모듈", "네트워크 / UI·텍스트", ["플랫폼 호환성", "UI·텍스트"])[0] is False


def test_unparsable_response_counts_as_wrong():
    item = ITEMS["B04"]
    r = score_one(rec(item, "모르겠습니다"), item)
    assert not r["all_fields_ok"] and not r["format"]["parsable_pass"]


def test_hallucinated_device_detected_but_context_quote_is_not():
    item = ITEMS["A03"]  # "pc로 로그인 안되는데" — 기기 정보 없음
    bad = ideal(item).replace(f"[요약]: {item['input']['title']}", "[요약]: 갤럭시 S25에서 로그인 불가")
    assert score_one(rec(item, bad), item)["hallucinated"]
    b14 = ITEMS["B14"]  # 입력에 '갤럭시 Z 폴드5'가 있음
    ok = ideal(b14).replace(f"[요약]: {b14['input']['title']}", "[요약]: 갤럭시 Z 폴드5 펼친 화면에서 스킬 버튼 잘림")
    assert not score_one(rec(b14, ok), b14)["hallucinated"]
