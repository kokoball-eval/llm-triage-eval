"""v1.5 9단계(triage-final, 날조 확인 화면) 시험 — 모델을 호출하지 않으므로 CI에서 자동 실행된다.

판정 순서·경계, 기준 파일과 계획 문서의 값, 날조 확인 묶음, 구성 동일성, STARTED 마커·재실행 규칙, 날조 확인 잠금,
개발용 기록으로 한 리허설(판정 도구 전체 경로), 확인 화면이 라벨·신호를 내려보내지 않음을 확인한다.
"""

import argparse
import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

import pytest

from triage_eval.pipeline import final_v15 as fv
from triage_eval.pipeline import run as run_mod
from triage_eval.pipeline.gate import GateError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "final_v15"))
import rater_server as rater  # noqa: E402

THR = {"min_bad_responses": 10, "min_bad_items": 5, "auto_rate_floor": 30.0, "adopt_pass_max": 0, "partial_pass_max_pct": 50.0}


def m(n=100, auto=50, bad_total=20, bad_auto=0, bad_items=8):
    return {"n": n, "auto": auto, "bad_total": bad_total, "bad_auto": bad_auto, "bad_items": bad_items, "bad_auto_items": 0}


# ── 판정 순서와 경계 (routing_v15_results.md §6) ─────────────

@pytest.mark.parametrize("metrics, label", [
    (m(bad_total=9), "표본 부족"),                      # 응답 10건 미만
    (m(bad_total=10, bad_items=4), "표본 부족"),        # 문항 5개 미만
    (m(bad_total=9, auto=0), "표본 부족"),              # 표본 부족이 처리율보다 먼저
    (m(auto=29, bad_auto=0), "미채택"),                 # 처리율 30% 미만은 통과 0건이어도 미채택
    (m(auto=30, bad_auto=0), "채택"),                   # 정확히 30% 는 통과
    (m(bad_auto=0), "채택"),
    (m(bad_total=20, bad_auto=10), "부분 채택"),        # 정확히 50%
    (m(bad_total=20, bad_auto=11), "미채택"),           # 55%
    (m(bad_total=10, bad_auto=5, bad_items=5), "부분 채택"),
    (m(bad_total=14, bad_auto=7), "부분 채택"),         # 반올림 없이 정확히 50%
    (m(bad_total=200, bad_auto=101), "미채택"),         # 50.5%
])
def test_verdict_order_and_boundaries(metrics, label):
    assert fv.verdict(metrics, THR)[0] == label


def test_criteria_file_matches_section_6_and_plan():
    crit = fv.load_criteria()
    v = crit["verdict"]
    assert (v["min_bad_responses"], v["min_bad_items"], v["auto_rate_floor"], v["adopt_pass_max"], v["partial_pass_max_pct"]) == (10, 5, 30.0, 0, 50.0)
    assert v["signals"] == ["S1", "S2"] and crit["meta"]["seeds"] == [1, 11, 21] and crit["meta"]["repeat"] == 2
    plan = (ROOT / "report" / "final_run_v15_plan.md").read_text(encoding="utf-8")
    for needle in ("10건 미만", "5개 미만", "30%", "50%", "STARTED", "--redo-incomplete", "날조 있음"):
        assert needle in plan
    section6 = (ROOT / "report" / "routing_v15_results.md").read_text(encoding="utf-8").split("## 6.", 1)[1].split("## 7.", 1)[0]
    assert "30%" in section6 and "50%" in section6 and "S1+S2 고정" in section6


def test_criteria_rejects_unknown_keys(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text((ROOT / "final_run_v15.toml").read_text(encoding="utf-8") + "\n[extra]\nx = 1\n", encoding="utf-8")
    with pytest.raises(GateError):
        fv.load_criteria(bad)


def test_guarded_files_exist_in_repo():
    for f in fv.GUARDED:
        assert (ROOT / f).exists(), f


# ── 날조 확인 묶음 ────────────────────────────────────────────

def rec(item, seed, run, text, ok=True):
    body = f"[요약]: {text}\n[분류]: 결함\n[우선순위]: Major" if ok else ""
    return {"item_id": item, "seed": seed, "run_index": run, "eval_id": f"qwen_{item}_run{run}", "success": ok, "response_text": body}


def test_groups_merge_same_summary_and_normalize_whitespace():
    recs = [rec("N1", 1, 1, "로그인  실패"), rec("N1", 1, 2, "로그인 실패 "), rec("N1", 11, 1, "다른 문장"),
            rec("N2", 1, 1, "로그인 실패"), rec("N2", 11, 1, "", ok=False)]
    groups, empty = fv.fabrication_groups(recs)
    assert len(groups) == 3                                           # N1 두 묶음 + N2 한 묶음(문항이 다르면 묶지 않는다)
    assert empty == ["s11-qwen_N2_run1"]
    merged = [g for g in groups.values() if g["item_id"] == "N1" and g["summary"] == "로그인 실패"][0]
    assert sorted(merged["members"]) == ["s1-qwen_N1_run1", "s1-qwen_N1_run2"]


def test_group_ids_and_display_order_are_deterministic_permutation():
    recs = [rec(f"N{i}", 1, 1, f"문장 {i}") for i in range(20)]
    g1, _ = fv.fabrication_groups(recs)
    g2, _ = fv.fabrication_groups(list(reversed(recs)))
    assert set(g1) == set(g2)
    order = fv.display_order(list(g1))
    assert order == fv.display_order(list(g2)) and sorted(order) == sorted(g1)
    assert order != sorted(g1, key=lambda g: g1[g]["item_id"])       # 문항 순서가 아니다


# ── 구성 동일성 ──────────────────────────────────────────────

def base_cfg(crit):
    return json.loads((ROOT / crit["meta"]["baseline"][0]).read_text(encoding="utf-8"))["metadata"]["run_config"]


def test_config_problems_detects_differences():
    crit = fv.load_criteria()
    cfg = json.loads(json.dumps(base_cfg(crit)))
    assert fv.config_problems(crit, [{"run_config": cfg}]) == []
    bad = json.loads(json.dumps(cfg))
    bad["prompt_version"] = "v9"
    bad["options"]["temperature"] = 0.9
    problems = fv.config_problems(crit, [{"run_config": bad}])
    assert any("prompt_version" in p for p in problems) and any("options" in p for p in problems)
    warn = json.loads(json.dumps(cfg))
    warn["environment"] = {"warnings": ["다른 프로그램이 VRAM 사용"]}
    assert any("환경 경고" in p for p in fv.config_problems(crit, [{"run_config": warn}]))
    other_model = json.loads(json.dumps(cfg))
    other_model["model_digests"] = {"qwen2.5:7b": "ffffffffffff"}
    assert any("model_digests" in p for p in fv.config_problems(crit, [{"run_config": other_model}]))


# ── run.py: 기존 평가셋 선택은 그대로, v15final 만 더해졌다 ─────

def test_run_dataset_options(capsys):
    assert run_mod.DATASET_V14.name == "aether_raid_v14.json" and run_mod.OUT_DIR_V14.parts[-2:] == ("v14", "history")
    assert run_mod.DATASET_V15_FINAL.name == "aether_raid_v15_final.json" and run_mod.OUT_DIR_V15_FINAL.parts[-2:] == ("v15_final", "history")
    # 모델을 부르기 전에 문항이 없다고 멈춘다(세트에 dev 문항이 없음) — v15final 이 m2 를 허용함도 확인
    assert run_mod.main(["--dataset", "v15final", "--split", "dev", "--method", "m2"]) == 2
    out = capsys.readouterr().out
    assert "문항이 없어" in out and "v14 평가셋에서만" not in out
    assert run_mod.main(["--dataset", "v13", "--method", "m2"]) == 2      # 기존 제한 유지
    assert "v14 평가셋에서만" in capsys.readouterr().out


def test_seed_argv_uses_final_flags():
    crit = fv.load_criteria()
    a = fv.seed_argv(crit, 11)
    assert a[:4] == ["--dataset", "v15final", "--split", "final"] and "--final" in a and "--strict-env" in a
    assert a[a.index("--seed") + 1] == "11" and a[a.index("--method") + 1] == "m2" and a[a.index("--repeat") + 1] == "2"


# ── STARTED 마커와 재실행 규칙 ────────────────────────────────

@pytest.fixture
def run_env(tmp_path, monkeypatch):
    crit = json.loads(json.dumps(fv.load_criteria()))
    crit["meta"]["history_dir"] = str(tmp_path / "history")
    crit["meta"]["started_marker"] = str(tmp_path / "marker.json")
    monkeypatch.setattr(fv, "load_criteria", lambda *a, **k: crit)
    monkeypatch.setattr(fv, "guard", lambda: None)
    monkeypatch.setattr(fv, "check_seal", lambda c: None)
    monkeypatch.setattr(fv, "current_digests", lambda model: {model: "845dbda0ea48"})   # CI 에는 Ollama 가 없다 — 모델 조회를 막아 둔다
    calls = []
    behavior = {"fail_seed": None}

    def fake_main(argv):
        seed = int(argv[argv.index("--seed") + 1])
        calls.append(seed)
        if behavior["fail_seed"] == seed:
            return 2
        hist = tmp_path / "history"
        hist.mkdir(exist_ok=True)
        (hist / f"v15final_final_m2_{seed}_{len(calls)}.json").write_text(json.dumps({"results": [], "seed": seed}), encoding="utf-8")
        return 0

    monkeypatch.setattr(fv.run_mod, "main", fake_main)
    return crit, calls, behavior, tmp_path


def args(redo=None):
    return argparse.Namespace(redo_incomplete=redo)


def test_run_records_marker_and_never_reruns_completed_seeds(run_env):
    crit, calls, _, tmp = run_env
    assert fv.cmd_run(args()) == 0 and calls == [1, 11, 21]
    marker = json.loads((tmp / "marker.json").read_text(encoding="utf-8"))
    assert all(marker["seeds"][str(s)]["status"] == "done" and len(marker["seeds"][str(s)]["sha256"]) == 64 for s in (1, 11, 21))
    assert fv.cmd_run(args()) == 0 and calls == [1, 11, 21]            # 두 번째 실행은 아무것도 돌지 않는다


def test_failed_seed_blocks_until_redo_is_requested_and_is_logged(run_env):
    crit, calls, behavior, tmp = run_env
    behavior["fail_seed"] = 11
    assert fv.cmd_run(args()) == 2 and calls == [1, 11]
    assert json.loads((tmp / "marker.json").read_text(encoding="utf-8"))["seeds"]["11"]["status"] == "failed"
    behavior["fail_seed"] = None
    with pytest.raises(GateError):                                    # 그냥 다시 실행하면 거부
        fv.cmd_run(args())
    assert calls == [1, 11]
    assert fv.cmd_run(args(redo=[11])) == 0 and calls == [1, 11, 11, 21]
    marker = json.loads((tmp / "marker.json").read_text(encoding="utf-8"))
    assert marker["redo"][0]["seed"] == 11 and marker["seeds"]["11"]["status"] == "done"


def test_run_refuses_when_guard_fails(run_env, monkeypatch):
    def boom():
        raise GateError("커밋하지 않은 변경")
    monkeypatch.setattr(fv, "guard", boom)
    with pytest.raises(GateError):
        fv.cmd_run(args())
    assert run_env[1] == []


def test_load_run_records_detects_modified_record(run_env):
    crit, _, _, tmp = run_env
    fv.cmd_run(args())
    marker = json.loads((tmp / "marker.json").read_text(encoding="utf-8"))
    path = Path(marker["seeds"]["1"]["file"]) if Path(marker["seeds"]["1"]["file"]).is_absolute() else ROOT / marker["seeds"]["1"]["file"]
    path.write_text(json.dumps({"results": [{"model": "x"}]}), encoding="utf-8")
    with pytest.raises(GateError):
        fv.load_run_records(crit, marker)


def test_check_seal_matches_sealed_dataset():
    fv.check_seal(fv.load_criteria())
    tampered = json.loads(json.dumps(fv.load_criteria()))
    tampered["meta"]["dataset"] = "data/eval_v15/aether_raid_v15.json"
    with pytest.raises(GateError):
        fv.check_seal(tampered)


# ── 날조 확인 잠금 ───────────────────────────────────────────

def fab_file(tmp, groups, decision="날조 없음", mutate=None):
    answers = {gid: {"결정": decision, "members": sorted(g["members"]), "summary_sha": rater.summary_sha(g["summary"])} for gid, g in groups.items()}
    if mutate:
        mutate(answers)
    p = tmp / "fab.json"
    p.write_text(json.dumps({"answers": answers}), encoding="utf-8")
    return p


def fab_crit(p):
    crit = json.loads(json.dumps(fv.load_criteria()))
    crit["meta"]["fabrication_check"] = str(p)
    return crit


def test_load_fabrication_requires_complete_locked_matching_answers(tmp_path, monkeypatch):
    groups, empty = fv.fabrication_groups([rec("N1", 1, 1, "가"), rec("N1", 1, 2, "가"), rec("N2", 1, 1, "나"), rec("N2", 11, 1, "", ok=False)])
    monkeypatch.setattr(fv, "committed_and_clean", lambda paths: [])
    gid_a = next(g for g, v in groups.items() if v["summary"] == "가")
    p = fab_file(tmp_path, groups, mutate=lambda a: a[gid_a].update({"결정": "날조 있음"}))
    out = fv.load_fabrication(fab_crit(p), groups, empty)
    assert out["s1-qwen_N1_run1"] and out["s1-qwen_N1_run2"] and not out["s1-qwen_N2_run1"] and not out["s11-qwen_N2_run1"]
    with pytest.raises(GateError):                                    # 파일 없음
        fv.load_fabrication(fab_crit(tmp_path / "none.json"), groups, empty)
    p = fab_file(tmp_path, groups, mutate=lambda a: a.pop(gid_a))     # 묶음 하나가 판정 안 됨
    with pytest.raises(GateError):
        fv.load_fabrication(fab_crit(p), groups, empty)
    p = fab_file(tmp_path, groups, mutate=lambda a: a[gid_a].update({"summary_sha": "0" * 64}))   # 확인 당시와 요약이 다름
    with pytest.raises(GateError):
        fv.load_fabrication(fab_crit(p), groups, empty)
    p = fab_file(tmp_path, groups, mutate=lambda a: a[gid_a].update({"결정": "모르겠음"}))
    with pytest.raises(GateError):
        fv.load_fabrication(fab_crit(p), groups, empty)
    monkeypatch.setattr(fv, "committed_and_clean", lambda paths: ["커밋하지 않은 변경이 있습니다"])   # 잠기지 않음
    p = fab_file(tmp_path, groups)
    with pytest.raises(GateError):
        fv.load_fabrication(fab_crit(p), groups, empty)


# ── 리허설: 개발용 기록으로 판정 도구 전체 경로 ───────────────

def test_rehearsal_with_dev_baseline_reproduces_stage7_routing(tmp_path, monkeypatch):
    """7단계 개발용 기준선 기록(63문항 × 6응답)을 새 도구에 넣어, 신호·자동 처리 수가 7단계 결과와 같음을 확인한다.
    날조 확인은 전부 없음으로 채웠으므로 위험 누락만 센다."""
    labels = json.loads((ROOT / "data" / "eval_v15" / "aether_raid_v15.json").read_text(encoding="utf-8"))
    dev_items = [i for i in labels["items"] if i["split"] == "dev"]
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps({**labels, "items": dev_items}, ensure_ascii=False), encoding="utf-8")
    crit = json.loads(json.dumps(fv.load_criteria()))
    crit["meta"]["dataset"] = str(ds)
    marker = {"seeds": {}}
    for seed, rel_path in zip(crit["meta"]["seeds"], crit["meta"]["baseline"]):
        marker["seeds"][str(seed)] = {"status": "done", "file": rel_path, "sha256": fv.sha256_lf(ROOT / rel_path)}
    records, _ = fv.load_run_records(crit, marker)
    groups, empty = fv.fabrication_groups(records)
    p = fab_file(tmp_path, groups)
    crit["meta"]["fabrication_check"] = str(p)
    monkeypatch.setattr(fv, "committed_and_clean", lambda paths: [])
    monkeypatch.setattr(fv, "read_marker", lambda c: marker)
    res = fv.run_verdict(crit)
    dev = json.loads((ROOT / "data" / "results" / "v15" / "routing" / "routing_dev_v15.json").read_text(encoding="utf-8"))["selection"]["selected_metrics"]
    assert dev["signals"] == ["S1", "S2"]
    assert res["selected"]["auto"] == dev["auto"] and res["selected"]["review"] == dev["review"] and res["metrics"]["n"] == dev["n"] == 378
    assert res["selected"]["risk_auto"] == dev["risk_auto"] and res["selected"]["risk_total"] == dev["risk_total"]
    # 개발용 기록의 평가셋 해시는 임시 평가셋과 다르므로 해시 문제만 나와야 한다(구성 키·seed·문항은 일치)
    assert res["config_problems"] and all("평가셋 해시" in p for p in res["config_problems"])
    assert res["fabrication"]["human_fabricated_responses"] == 0
    assert res["verdict"] == "판정하지 않음"
    assert "판정" in fv.render(res) and res["fabrication"]["groups"] == len(groups)


# ── 확인 화면 ────────────────────────────────────────────────

@pytest.fixture
def rater_env(tmp_path, monkeypatch):
    groups, _ = fv.fabrication_groups([rec("NA01", 1, 1, "요약 하나"), rec("NA01", 11, 1, "요약 둘"), rec("NB02", 1, 1, "요약 셋")])
    inputs = {"NA01": {"board": "자유게시판", "posted_at": "2026-11-20 10:00", "title": "제목", "body": "본문",
                      "context": {"update_note": "공지", "known_issues": ["기존 이슈"]}},
              "NB02": {"reporter": "내부 QA", "title": "제목2", "body": "본문2", "context": {}}}
    monkeypatch.setattr(rater, "load_groups", lambda: (groups, inputs))
    monkeypatch.setattr(rater, "ANSWERS", tmp_path / "answers.json")
    return groups


def test_screen_state_exposes_only_input_and_summary(rater_env):
    s = rater.state()
    assert len(s["groups"]) == 3 and all(set(g) == {"id", "input", "summary"} for g in s["groups"])
    blob = json.dumps(s, ensure_ascii=False)
    for forbidden in ("labels", "risk", "NA01", "NB02", "seed", "S1", "S2", "우선순위", "긴급 사인", "hallucinated"):
        assert forbidden not in blob


def test_screen_save_history_and_validation(rater_env):
    gid = rater.state()["groups"][0]["id"]
    rater.save_answer({"id": gid, "결정": "날조 없음"})
    rater.save_answer({"id": gid, "결정": "날조 없음"})                        # 같은 내용은 이력을 남기지 않는다
    assert json.loads(rater.ANSWERS.read_text(encoding="utf-8"))["changes"] == []
    rater.save_answer({"id": gid, "결정": "날조 있음", "메모": "수치가 원문에 없음"})
    data = json.loads(rater.ANSWERS.read_text(encoding="utf-8"))
    assert len(data["changes"]) == 1 and data["answers"][gid]["결정"] == "날조 있음" and data["answers"][gid]["members"]
    assert rater.status().startswith("확인 1/3묶음, 날조 있음 1묶음")
    for bad in ({"id": "없음", "결정": "날조 있음"}, {"id": gid, "결정": "보류"}, ["x"], "x"):
        with pytest.raises(ValueError):
            rater.save_answer(bad)


def test_screen_refuses_before_runs_finish(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(fv, "read_marker", lambda c: None)
    with pytest.raises(rater.NotReady):
        rater.load_groups()


def test_screen_code_reads_no_scores_or_labels():
    src = (ROOT / "tools" / "final_v15" / "rater_server.py").read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]
    for word in ("score", "routing", "labels", "risk_miss", "routed", "baseline", "sealed"):
        assert word not in code, word


def test_screen_http_checks(rater_env):
    server = ThreadingHTTPServer(("127.0.0.1", 0), rater.Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{port}"
        state = json.loads(request.urlopen(base + "/api/state").read())
        gid = state["groups"][0]["id"]
        assert b"<html" in request.urlopen(base + "/").read()
        body = json.dumps({"id": gid, "결정": "날조 없음"}).encode("utf-8")
        assert json.loads(request.urlopen(request.Request(base + "/api/answer", body, {"Content-Type": "application/json"})).read())["saved"]["결정"] == "날조 없음"
        with pytest.raises(HTTPError) as e:
            request.urlopen(request.Request(base + "/api/answer", body, {"Content-Type": "text/plain"}))
        assert e.value.code == 403
        with pytest.raises(HTTPError) as e:
            request.urlopen(request.Request(base + "/api/state", headers={"Host": "evil.example"}))
        assert e.value.code == 403
        with pytest.raises(HTTPError) as e:
            request.urlopen(request.Request(base + "/api/answer", b"[1]", {"Content-Type": "application/json"}))
        assert e.value.code == 400
    finally:
        server.shutdown()
        server.server_close()


# ── 독립 검토 반영: guard 완전성, 재실행·마커 구멍, 여러 줄 요약, 판정 경로 ──

def test_guard_covers_every_imported_repo_module():
    """triage-final 과 확인 화면이 import 하는 triage_eval 코드는 모두 guard 목록에 있다."""
    import subprocess
    code = ("import sys, json; sys.path.insert(0, %r); import triage_eval.pipeline.final_v15, rater_server; "
            "print(json.dumps(sorted(m.__file__ for n, m in sys.modules.items() if n.startswith('triage_eval') and getattr(m, '__file__', None))))"
            % str(ROOT / "tools" / "final_v15"))
    out = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    files = json.loads(out.stdout.strip().splitlines()[-1])
    guarded = {(ROOT / f).resolve() for f in fv.GUARDED}
    missing = [Path(f).relative_to(ROOT).as_posix() for f in files if Path(f).name != "__init__.py" and Path(f).resolve() not in guarded]
    assert missing == []


def test_summary_joins_following_lines_until_next_label():
    body = "[요약]: 첫 줄 내용\n둘째 줄 내용\n\n셋째 줄\n[분류]: 결함\n[우선순위]: Major"
    r = {"item_id": "N1", "seed": 1, "run_index": 1, "eval_id": "e", "success": True, "response_text": body}
    assert fv.summary_of(r) == "첫 줄 내용 둘째 줄 내용 셋째 줄"
    assert fv.summary_of({**r, "response_text": "[분류]: 결함"}) == ""
    assert fv.summary_of({**r, "success": False}) == ""


def test_redo_refused_when_the_seed_already_wrote_a_record(run_env):
    crit, calls, _, tmp = run_env

    def writes_then_crashes(argv):
        seed = int(argv[argv.index("--seed") + 1])
        calls.append(seed)
        hist = tmp / "history"
        hist.mkdir(exist_ok=True)
        (hist / f"v15final_final_m2_{seed}.json").write_text("{}", encoding="utf-8")
        raise RuntimeError("기록을 쓴 뒤 오류")

    fv.run_mod.main = writes_then_crashes
    with pytest.raises(RuntimeError):
        fv.cmd_run(args())
    assert json.loads((tmp / "marker.json").read_text(encoding="utf-8"))["seeds"]["1"]["status"] == "started"
    with pytest.raises(GateError) as e:
        fv.cmd_run(args(redo=[1]))
    assert "기록 파일이 이미 있습니다" in str(e.value) and calls == [1]


def test_deleted_marker_with_existing_history_is_refused(run_env):
    crit, calls, _, tmp = run_env
    fv.cmd_run(args())
    (tmp / "marker.json").unlink()
    with pytest.raises(GateError) as e:
        fv.cmd_run(args())
    assert "마커" in str(e.value) and calls == [1, 11, 21]


def test_broken_marker_is_reported_not_overwritten(run_env):
    crit, calls, _, tmp = run_env
    (tmp / "marker.json").write_text("{깨진", encoding="utf-8")
    with pytest.raises(GateError):
        fv.cmd_run(args())
    assert (tmp / "marker.json").read_text(encoding="utf-8") == "{깨진" and calls == []


def test_model_digest_mismatch_blocks_before_any_run(run_env, monkeypatch):
    crit, calls, _, _ = run_env
    monkeypatch.setattr(fv, "current_digests", lambda model: {model: "ffffffffffff"})
    with pytest.raises(GateError) as e:
        fv.cmd_run(args())
    assert "digest" in str(e.value) and calls == []
    monkeypatch.setattr(fv, "current_digests", lambda model: {model: "845dbda0ea48"})
    assert fv.cmd_run(args()) == 0


def test_check_seal_requires_sealed_status(tmp_path, monkeypatch):
    crit = json.loads(json.dumps(fv.load_criteria()))
    doc = json.loads((ROOT / crit["meta"]["dataset"]).read_text(encoding="utf-8"))
    doc["status"] = "draft"
    copy = tmp_path / "ds.json"
    copy.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    crit["meta"]["dataset"] = str(copy)
    with pytest.raises(GateError):
        fv.check_seal(crit)


def dev_world(tmp_path, decide=None):
    """개발용 기록을 새 도구에 넣는 공통 준비. decide(groups) → 묶음별 결정 dict (기본 전부 날조 없음)."""
    labels = json.loads((ROOT / "data" / "eval_v15" / "aether_raid_v15.json").read_text(encoding="utf-8"))
    ds = tmp_path / "ds.json"
    ds.write_text(json.dumps({**labels, "items": [i for i in labels["items"] if i["split"] == "dev"]}, ensure_ascii=False), encoding="utf-8")
    crit = json.loads(json.dumps(fv.load_criteria()))
    crit["meta"]["dataset"] = str(ds)
    crit["meta"]["result_json"] = str(tmp_path / "out" / "r.json")
    crit["meta"]["report"] = str(tmp_path / "out_report.md")
    marker = {"seeds": {str(s): {"status": "done", "file": f, "sha256": fv.sha256_lf(ROOT / f)}
                        for s, f in zip(crit["meta"]["seeds"], crit["meta"]["baseline"])}}
    records, _ = fv.load_run_records(crit, marker)
    groups, _ = fv.fabrication_groups(records)
    answers = {gid: {"결정": (decide or {}).get(gid, "날조 없음"), "members": sorted(g["members"]),
                     "summary_sha": rater.summary_sha(g["summary"])} for gid, g in groups.items()}
    p = tmp_path / "fab.json"
    p.write_text(json.dumps({"answers": answers}), encoding="utf-8")
    crit["meta"]["fabrication_check"] = str(p)
    return crit, marker, groups, p


def test_run_verdict_applies_human_fabrication_and_flags_wrong_dataset_hash(tmp_path, monkeypatch):
    crit, marker, groups, _ = dev_world(tmp_path)
    monkeypatch.setattr(fv, "committed_and_clean", lambda paths: [])
    monkeypatch.setattr(fv, "read_marker", lambda c: marker)
    plain = fv.run_verdict(crit)
    # 개발용 기록의 평가셋 해시는 임시 평가셋과 다르므로 '판정하지 않음' + 해시 문제만 나온다(검사가 작동함)
    assert plain["verdict"] == "판정하지 않음" and plain["config_problems"] and all("평가셋 해시" in p for p in plain["config_problems"])
    # 사람이 날조로 본 묶음은 위험·날조 응답 수에 더해지고, 자동 처리된 날조는 통과로 센다
    some = {gid: "날조 있음" for gid in list(groups)[:40]}
    crit2, marker2, _, _ = dev_world(tmp_path / "w2" if (tmp_path / "w2").mkdir() is None else tmp_path, decide=some)
    monkeypatch.setattr(fv, "read_marker", lambda c: marker2)
    marked = fv.run_verdict(crit2)
    assert marked["fabrication"]["human_fabricated_responses"] > 0
    assert marked["metrics"]["bad_total"] > plain["metrics"]["bad_total"]
    assert marked["selected"]["hallucinated_total"] == marked["fabrication"]["human_fabricated_responses"]


def test_run_verdict_refuses_partial_fabrication_file(tmp_path, monkeypatch):
    crit, marker, groups, p = dev_world(tmp_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["answers"].pop(next(iter(data["answers"])))
    p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(fv, "committed_and_clean", lambda paths: [])
    monkeypatch.setattr(fv, "read_marker", lambda c: marker)
    with pytest.raises(GateError):
        fv.run_verdict(crit)
    p.write_text("{깨진", encoding="utf-8")
    with pytest.raises(GateError):
        fv.run_verdict(crit)


def test_cmd_verdict_writes_result_before_printing_and_no_write_skips(tmp_path, monkeypatch, capsys):
    crit, marker, _, _ = dev_world(tmp_path)
    monkeypatch.setattr(fv, "load_criteria", lambda *a, **k: crit)
    monkeypatch.setattr(fv, "guard", lambda: None)
    monkeypatch.setattr(fv, "check_seal", lambda c: None)
    monkeypatch.setattr(fv, "committed_and_clean", lambda paths: [])
    monkeypatch.setattr(fv, "read_marker", lambda c: marker)
    assert fv.cmd_verdict(argparse.Namespace(no_write=True)) == 0
    assert not (tmp_path / "out").exists() and "판정:" in capsys.readouterr().out
    assert fv.cmd_verdict(argparse.Namespace(no_write=False)) == 0
    assert json.loads((tmp_path / "out" / "r.json").read_text(encoding="utf-8"))["verdict"] and (tmp_path / "out_report.md").exists()
