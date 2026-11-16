"""v1.5 확신도 기반 라우팅 — 신호·후보 평가·선택 규칙(pipeline/routing.py) 테스트. 모델을 부르지 않는다."""

import copy
import json
from pathlib import Path

import pytest

from triage_eval.pipeline import routing as rt
from triage_eval.pipeline.gate import GateError

ROOT = Path(__file__).resolve().parent.parent
DATASET = json.loads((ROOT / "data/eval_v15/aether_raid_v15.json").read_text(encoding="utf-8"))
ITEM = next(i for i in DATASET["items"] if i["split"] == "dev")


def text(priority="Major", action="개발 배정", module="결제·재화", cls="결함", repro="충분"):
    return (f"[요약]: 결제 후 재화 미반영\n[분류]: {cls}\n[모듈]: {module}\n[우선순위]: {priority}\n[재현 정보]: {repro}\n"
            f"[발생 빈도]: 항상\n[처리]: {action}\n[누락 정보 및 권장 조치]: 없음")


def record(n, response, **kw):
    base = {"eval_id": f"E{n}", "model": "qwen2.5:7b", "run_index": n % 2, "item_id": ITEM["id"], "seed": 1, "success": True,
            "response_text": response, "elapsed_sec": 1.0, "prompt_eval_count": 100, "retried": False,
            "guardrail": {"applied": []}, "hit_num_predict": False}
    return {**base, **kw}


@pytest.fixture(scope="module")
def crit():
    return rt.load_criteria()


def rows_for(responses, crit, item=ITEM, **kw):
    recs = [record(n, r, **kw) for n, r in enumerate(responses)]
    return rt.build_rows(recs, {item["id"]: item}, crit)


# ── 기준 파일 ────────────────────────────────────────────────

def test_criteria_match_plan(crit):
    assert crit["signals"]["review_actions"] == ["긴급 사인 요청", "정보 요청 후 보류"]
    assert crit["signals"]["review_priorities"] == ["Critical", "판단보류"]
    assert crit["signals"]["agreement_fields"] == ["우선순위", "처리"] and crit["signals"]["expected_responses_per_item"] == 6
    assert crit["selection"] == {"accuracy_fields": ["우선순위", "모듈", "처리"], "accuracy_gain_min": 2.0, "auto_rate_floor": 30.0}


def test_criteria_typo_is_rejected(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text(rt.CRITERIA.read_text(encoding="utf-8").replace("accuracy_gain_min", "accuracy_gain_mim"), encoding="utf-8")
    with pytest.raises(GateError, match="accuracy_gain_mim"):
        rt.load_criteria(bad)


def test_baseline_files_exist_and_are_dev_m2(crit):
    for p in crit["meta"]["baseline"]:
        log = json.loads((ROOT / p).read_text(encoding="utf-8"))
        assert log["metadata"]["dataset_version"] == "v1.4" and "dev" in p and "m2" in p


# ── 신호 ─────────────────────────────────────────────────────

def test_s1_flags_urgent_and_hold_and_risky_priority(crit):
    same = [text()] * 5
    assert not rows_for(same + [text()], crit)[0]["flags"]["S1"]
    assert rows_for(same + [text(action="긴급 사인 요청")], crit)[5]["flags"]["S1"]
    assert rows_for(same + [text(action="정보 요청 후 보류")], crit)[5]["flags"]["S1"]
    assert rows_for(same + [text(priority="Critical", action="개발 배정")], crit)[5]["flags"]["S1"]
    assert rows_for(same + [text(priority="판단보류")], crit)[5]["flags"]["S1"]


def test_s3_flags_every_response_of_a_disagreeing_item(crit):
    agree = rows_for([text()] * 6, crit)
    assert not any(r["flags"]["S3"] for r in agree)
    split = rows_for([text()] * 5 + [text(priority="Minor")], crit)
    assert all(r["flags"]["S3"] for r in split)
    other_field = rows_for([text()] * 5 + [text(module="네트워크")], crit)   # 모듈만 다른 것은 S3 대상이 아니다
    assert not any(r["flags"]["S3"] for r in other_field)


def test_wrong_response_count_per_item_is_refused(crit):
    with pytest.raises(GateError, match="응답 수"):
        rows_for([text()] * 5, crit)


def test_s2_flags_failed_response_and_null_guardrail(crit):
    r = rows_for([text()] * 6, crit, success=False)[0]
    assert r["flags"]["S2"] and "실행 실패" in r["trace"]
    assert not rows_for([text()] * 6, crit, guardrail=None)[0]["flags"]["S2"]   # guardrail 값이 None 이어도 오류 없음


def test_failed_response_counts_as_disagreement_in_s3(crit):
    recs = [record(n, text()) for n in range(5)] + [record(5, text(), success=False)]
    assert all(r["flags"]["S3"] for r in rt.build_rows(recs, {ITEM["id"]: ITEM}, crit))


def test_s2_reads_pipeline_traces(crit):
    r = rows_for([text()] * 6, crit, retried=True, guardrail={"applied": [{"rule": "H-1"}]}, critical_upgraded=True,
                 discard_held=True, hit_num_predict=True)[0]
    assert r["flags"]["S2"] and set(r["trace"]) >= {"형식 재요청", "안전장치 적용", "Critical 상향", "폐기 보류", "출력 잘림"}
    assert not rows_for([text()] * 6, crit)[0]["flags"]["S2"]
    assert rows_for(["엉뚱한 응답"] * 6, crit)[0]["flags"]["S2"]   # 형식 위반


def test_signals_never_read_labels(crit):
    """설계 의도 2 — 정답 라벨을 바꿔도 신호(flags·trace)는 같다. 라벨은 평가용 결과(위험 누락·정확도)에만 영향을 준다."""
    other = copy.deepcopy(ITEM)
    other["labels"]["우선순위"] = ["Trivial"]
    other["labels"]["처리"] = ["폐기"]
    other["labels"]["risk"] = ["X-1", "X-3"]
    fabricated = text().replace("결제 후 재화 미반영", "갤럭시 S24에서 결제 후 재화 3회 미반영")
    responses = [text(), text(action="긴급 사인 요청"), fabricated, "엉뚱한 응답", text(priority="Minor"), text()]
    a, b = rows_for(responses, crit), rows_for(responses, crit, item=other)
    assert a[2]["flags"]["S4"] and a[3]["flags"]["S2"] and a[1]["flags"]["S1"] and a[0]["flags"]["S3"]   # 신호가 실제로 켜진 응답을 비교한다
    assert [r["flags"] for r in a] == [r["flags"] for r in b] and [r["trace"] for r in a] == [r["trace"] for r in b]


# ── 후보 평가·선택 ───────────────────────────────────────────

def row(flags=(), risk=(), halluc=False, ok=True):
    f = {s: s in flags for s in ("S1", "S2", "S3", "S4")}
    return {"eval_id": "E", "item_id": "A01", "flags": f, "trace": [], "risk_miss": list(risk), "hallucinated": halluc,
            "fields_ok": {"우선순위": ok, "모듈": True, "처리": True}}


def test_evaluate_counts_auto_and_review():
    rows = [row(), row(risk=["X-1"]), row(flags=["S1"], risk=["X-1"]), row(flags=["S1"]), row(halluc=True), row(ok=False)]
    m = rt.evaluate(rows, ["S1"])
    assert (m["n"], m["review"], m["auto"], m["auto_rate"]) == (6, 2, 4, 66.7)
    assert m["risk_auto"]["X-1"] == 1 and m["risk_total"]["X-1"] == 2
    assert m["hallucinated_auto"] == 1 and m["bad_auto"] == 2 and m["accuracy_auto"] == 75.0
    assert m["review_precision"] == 50.0
    assert rt.evaluate(rows, [])["auto_rate"] == 100.0


def test_a_response_with_risk_and_hallucination_counts_once():
    m = rt.evaluate([row(risk=["X-3"], halluc=True)], [])
    assert m["bad_auto"] == 1 and m["bad_total"] == 1


def fake_eval(monkeypatch, table):
    """evaluate 를 시험용 표로 바꿔 선택 규칙만 정확히 시험한다. table: {신호 튜플: (n, auto, auto_ok, bad_auto)}."""
    def fake(rows, signals):
        n, auto, ok, bad = table[tuple(signals)]
        return {"signals": signals, "n": n, "auto": auto, "auto_ok": ok, "bad_auto": bad,
                "auto_rate": round(auto / n * 100, 1), "accuracy_auto": round(ok / auto * 100, 1) if auto else None}
    monkeypatch.setattr(rt, "evaluate", fake)


def test_selection_adopts_when_bad_auto_drops_and_compares_with_current_selection(monkeypatch, crit):
    """+S2 가 위험·날조 통과를 줄이면 채택, +S4 는 줄지 않아 거절, +S3 는 S1+S2 에 더한 집합을 지금 선택과 비교한다."""
    fake_eval(monkeypatch, {("S1",): (100, 90, 70, 5), ("S1", "S2"): (100, 80, 66, 4), ("S1", "S2", "S4"): (100, 75, 62, 4),
                            ("S1", "S2", "S3"): (100, 40, 36, 0)})
    s = rt.select([], crit)
    assert s["selected"] == ["S1", "S2", "S3"] and [x["adopted"] for x in s["steps"]] == [True, False, True]
    assert [x["signal"] for x in s["steps"]] == ["S2", "S4", "S3"]
    assert s["steps"][2]["from"] == ["S1", "S2"] and s["steps"][2]["to"] == ["S1", "S2", "S3"]   # 거절한 S4 는 섞이지 않는다


def test_rejected_signal_does_not_leak_into_later_candidates(monkeypatch, crit):
    fake_eval(monkeypatch, {("S1",): (100, 90, 70, 5), ("S1", "S2"): (100, 80, 56, 5), ("S1", "S4"): (100, 85, 60, 3),
                            ("S1", "S4", "S3"): (100, 50, 40, 5)})
    # S2 는 줄지도 않고 정확도도 오르지 않아 거절된다. 다음 후보는 S1+S2+S4 가 아니라 S1+S4 다
    s = rt.select([], crit)
    assert [x["to"] for x in s["steps"]] == [["S1", "S2"], ["S1", "S4"], ["S1", "S4", "S3"]] and s["selected"] == ["S1", "S4"]


def test_selection_floor_is_checked_first(monkeypatch, crit):
    fake_eval(monkeypatch, {("S1",): (100, 90, 70, 5), ("S1", "S2"): (100, 29, 29, 0), ("S1", "S4"): (100, 90, 70, 5),
                            ("S1", "S3"): (100, 90, 70, 5)})
    s = rt.select([], crit)
    assert s["selected"] == ["S1"] and "하한" in s["steps"][0]["reason"] and not s["steps"][0]["adopted"]


def test_selection_boundary_floor_and_gain_pass_with_exact_ratios(monkeypatch, crit):
    """하한 30.0%와 정확도 향상 2.0%p는 같으면 충족이다. 62.1% → 64.1% 는 부동소수로는 1.9999… 라 거절되는 값이다."""
    assert 64.1 - 62.1 < 2.0   # 반올림한 값을 빼는 방식이면 이 경우를 거절한다
    fake_eval(monkeypatch, {("S1",): (1000, 1000, 621, 5), ("S1", "S2"): (1000, 1000, 641, 5), ("S1", "S2", "S4"): (1000, 300, 190, 5),
                            ("S1", "S2", "S3"): (1000, 300, 200, 5)})
    s = rt.select([], crit)
    assert s["steps"][0]["adopted"] and s["steps"][1]["adopted"] is False   # S4: 하한 30.0% 와 같지만 위험·정확도 조건에서 거절
    assert "하한" not in s["steps"][1]["reason"]


def test_selection_just_below_gain_or_floor_is_rejected(monkeypatch, crit):
    fake_eval(monkeypatch, {("S1",): (1000, 1000, 621, 5), ("S1", "S2"): (1000, 1000, 640, 5), ("S1", "S4"): (1000, 299, 200, 5),
                            ("S1", "S3"): (1000, 1000, 621, 5)})
    s = rt.select([], crit)
    assert s["selected"] == ["S1"] and "하한" in s["steps"][1]["reason"]


def test_selection_stops_when_s1_alone_is_below_floor(monkeypatch, crit):
    fake_eval(monkeypatch, {("S1",): (100, 20, 15, 0)})
    s = rt.select([], crit)
    assert s["selected"] == ["S1"] and not s["floor_ok"] and s["steps"] == []


def test_selection_handles_zero_auto_without_error(monkeypatch, crit):
    fake_eval(monkeypatch, {("S1",): (100, 90, 70, 5), ("S1", "S2"): (100, 0, 0, 0), ("S1", "S4"): (100, 90, 70, 5),
                            ("S1", "S3"): (100, 90, 70, 5)})
    assert rt.select([], crit)["selected"] == ["S1"]


def test_evaluate_counts_auto_and_review():
    rows = [row(), row(risk=["X-1"]), row(flags=["S1"], risk=["X-1"]), row(flags=["S1"]), row(halluc=True), row(ok=False)]
    m = rt.evaluate(rows, ["S1"])
    assert (m["n"], m["review"], m["auto"], m["auto_rate"]) == (6, 2, 4, 66.7)
    assert m["risk_auto"]["X-1"] == 1 and m["risk_total"]["X-1"] == 2
    assert m["hallucinated_auto"] == 1 and m["bad_auto"] == 2 and m["accuracy_auto"] == 75.0 and m["auto_ok"] == 3
    assert m["review_precision"] == 50.0
    assert rt.evaluate(rows, [])["auto_rate"] == 100.0


def test_a_response_with_risk_and_hallucination_counts_once():
    m = rt.evaluate([row(risk=["X-3"], halluc=True)], [])
    assert m["bad_auto"] == 1 and m["bad_total"] == 1


# ── 입력·보호 ────────────────────────────────────────────────

# 측정 전에는 실제 기록으로 계산하는 테스트를 건너뛴다(결과가 테스트 출력으로 새는 것을 막는다). 측정 결과 파일이 생기면 CI에서도 돈다.
after_measurement = pytest.mark.skipif(not rt.RESULT_JSON.exists(), reason="측정 전")


@after_measurement
def test_analysis_runs_on_saved_records(crit):
    """저장된 기준선 기록 378응답(63문항 × 6)으로 평가와 선택이 끝까지 돈다(값은 이 테스트에서 확인하지 않는다)."""
    res = rt.run_analysis(crit)
    assert res["n"] == 378 and set(res["baseline"]) == {"R0", "R1"} and set(res["selection"]["selected"]) <= {"S1", "S2", "S3", "S4"}
    assert res["baseline"]["R0"]["auto_rate"] == 100.0 and res["baseline"]["R0"]["review"] == 0


def test_coverage_check_refuses_duplicates_and_missing_items(crit):
    items = {i["id"]: i for i in DATASET["items"]}
    dev = [i for i in items.values() if i["split"] == "dev"]
    recs = [{"item_id": it["id"], "seed": 1, "run_index": 0} for it in dev]
    rt.check_coverage(recs, items, crit)
    with pytest.raises(GateError, match="겹칩니다"):
        rt.check_coverage(recs + recs[:1], items, crit)
    with pytest.raises(GateError, match="맞지 않습니다"):
        rt.check_coverage(recs[1:], items, crit)


def test_guard_refuses_uncommitted_files(monkeypatch):
    monkeypatch.setattr(rt, "git_lines", lambda *a: [])
    with pytest.raises(GateError, match="커밋되어 있지 않습니다"):
        rt.guard()
    monkeypatch.setattr(rt, "git_lines", lambda *a: ["x"] if a[0] == "ls-files" else [" M routing_v15.toml"])
    with pytest.raises(GateError, match="커밋하지 않은 변경"):
        rt.guard()


def test_main_stops_when_guard_fails_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(rt, "RESULT_JSON", tmp_path / "r.json")
    monkeypatch.setattr(rt, "REPORT", tmp_path / "r.md")
    monkeypatch.setattr(rt, "guard", lambda: (_ for _ in ()).throw(GateError("막힘")))
    assert rt.main([]) == 2 and not (tmp_path / "r.json").exists()


@after_measurement
def test_result_is_saved_before_printing(monkeypatch, tmp_path):
    """ISSUE-014 — 출력이 실패해도 결과는 이미 파일로 남는다."""
    monkeypatch.setattr(rt, "RESULT_JSON", tmp_path / "r.json")
    monkeypatch.setattr(rt, "REPORT", tmp_path / "r.md")
    monkeypatch.setattr(rt, "guard", lambda: None)

    def failing_print(*a, **k):
        raise UnicodeEncodeError("cp949", "x", 0, 1, "bad")

    monkeypatch.setattr("builtins.print", failing_print)
    with pytest.raises(UnicodeEncodeError):
        rt.main([])
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["n"] == 378 and (tmp_path / "r.md").exists()


def test_guarded_files_exist():
    assert all((ROOT / f).exists() for f in rt.GUARDED)
    assert {"tests/test_routing_v15.py", "src/triage_eval/pipeline/score.py", "src/triage_eval/common/detect_hallucination.py"} <= set(rt.GUARDED)


def synthetic_records(items):
    """개발용 문항마다 응답 6개(seed 3개 × 2회). 일부는 처리·우선순위를 다르게 하고 일부는 흔적을 남긴다 — 실제 결과가 아니다."""
    recs = []
    for k, it in enumerate(i for i in items.values() if i["split"] == "dev"):
        for seed_n, seed in enumerate((1, 11, 21)):
            for run in (0, 1):
                n = seed_n * 2 + run
                response = text(priority="Critical", action="긴급 사인 요청") if (k % 5 == 0 and n == 0) else text()
                extra = {"retried": True} if (k % 7 == 0 and n == 1) else {}
                recs.append(record(k * 6 + n, response, item_id=it["id"], seed=seed, run_index=run, eval_id=f"{it['id']}-{n}", **extra))
    return recs


def test_whole_pipeline_runs_end_to_end_on_synthetic_records(monkeypatch, tmp_path, crit):
    """실제 기록 대신 만든 응답으로 입력 검증 → 신호 → 평가 → 선택 → 보고서 → 저장까지 끝까지 돈다 (측정 전 점검용, 값은 의미 없음)."""
    monkeypatch.setattr(rt, "load_records", lambda paths, c, items: synthetic_records(items))
    monkeypatch.setattr(rt, "RESULT_JSON", tmp_path / "r.json")
    monkeypatch.setattr(rt, "REPORT", tmp_path / "r.md")
    monkeypatch.setattr(rt, "guard", lambda: None)
    assert rt.main([]) == 0
    res = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    report = (tmp_path / "r.md").read_text(encoding="utf-8")
    assert res["n"] == 378 and "## 선택:" in report and "## 선택 과정" in report and "신호 하나만" in report
    assert res["selection"]["selected"][0] == "S1" and res["selection"]["floor_ok"]
