"""v1.5 요약 품질 LLM-as-judge — 채점관 실행·지표·판정(pipeline/quality.py) 테스트. 가짜 채점관만 쓰고 실제 모델을 부르지 않는다.

측정 세트에 대한 실제 채점관 판정은 이 테스트에서 만들지 않는다(계획 §5 — 측정은 프롬프트·도구 커밋 뒤 1회).
"""

import io
import json
import sys
from pathlib import Path

import pytest

from triage_eval.pipeline import quality as qa
from triage_eval.pipeline.gate import GateError

ROOT = Path(__file__).resolve().parent.parent


class FakeJudge:
    """요약에 '흐림'이 있으면 문제 있음(Q1, 문구 '흐림'), 아니면 없음. bad 번 만큼 먼저 깨진 출력을 낸다."""

    def __init__(self, bad: int = 0, load: float = 0.0, reload_at: str | None = None, raise_at: str | None = None):
        self.bad, self.load, self.reload_at, self.raise_at, self.prompts = bad, load, reload_at, raise_at, []

    def generate(self, model, prompt, options=None, think=None, format=None, keep_alive=None):
        if keep_alive == 0 or format is None:
            return {"response": "", "load_duration": 0}
        self.prompts.append(prompt)
        if self.raise_at and self.raise_at in prompt:
            raise ConnectionError("서버 연결 끊김")
        load = 16.4e9 if self.reload_at and self.reload_at in prompt else self.load
        if self.bad:
            self.bad -= 1
            return {"response": "문제 있음", "load_duration": load}
        summary = prompt.rsplit("[요약]\n", 1)[1]
        verdict = ({"판정": qa.YES, "유형": ["Q1"], "문제 문구": "흐림"} if "흐림" in summary
                   else {"판정": qa.NO, "유형": [], "문제 문구": ""})
        return {"response": json.dumps(verdict, ensure_ascii=False), "load_duration": load}


def row(n: str, summary: str) -> dict:
    return {"번호": n, **{f: "" for f in qa.FIELDS}, "제목": "보스방 튕김", "본문": "보스방 들어가면 튕김", "요약": summary}


@pytest.fixture
def crit():
    return qa.load_criteria()


# ── 프롬프트·출력 읽기 ───────────────────────────────────────

def test_prompt_has_definition_and_all_fields():
    """설계 의도 2 — 채점 화면과 같은 정의 문구와 검토자 화면의 모든 원문 칸이 들어간다."""
    ui = (ROOT / "tools/quality_v15/rater_ui.html").read_text(encoding="utf-8")
    p = qa.build_prompt(row("Q01", "보스방 튕김"))
    for line in ("무엇이·어디서가 빠져, 무엇에 대한 이슈인지 알 수 없다",
                 "원문과 다른 뜻으로 읽힌다. 이슈의 성격이 바뀐 경우(건의 → 오류, 의심 → 확정 등)도 포함",
                 "목적어·서술어가 빠지거나 문장이 어색해 뜻이 흐려진다",
                 "이슈의 성격을 바꾸는 서술(건의 → 오류, 의심 → 확정 등)은 날조가 아니라 Q2로 본다.",
                 "이 요약 한 줄만 읽은 담당자가 이슈를"):
        assert line in p and line in ui
    assert all(f"{f}:" in p for f in qa.FIELDS) and p.rstrip().endswith("보스방 튕김")


@pytest.mark.parametrize("text,ok", [
    ('{"판정": "품질 문제 있음", "유형": ["Q1"], "문제 문구": "x"}', True),
    ('{"판정": "품질 문제 없음", "유형": [], "문제 문구": ""}', True),
    ('{"판정": "품질 문제 있음", "유형": ["Q9"], "문제 문구": "x"}', False),
    ('{"판정": "품질 문제 있음", "문제 문구": "x"}', False),
    ('{"판정": "아마도", "유형": []}', False), ("문제 있음", False), ("", False)])
def test_parse_judgement(text, ok):
    assert (qa.parse_judgement(text) is not None) is ok


def test_unparsable_output_is_retried_once():
    """설계 의도 3 — 한 번 깨지면 다시 요청해 읽고, 두 번 깨지면 읽지 못함으로 둔다."""
    j = qa.judge_one(FakeJudge(bad=1), "fake", row("Q01", "흐림 요약"))
    assert j["parsed"] and j["attempts"] == 2 and j["verdict"] == qa.YES and j["types"] == ["Q1"]
    j = qa.judge_one(FakeJudge(bad=2), "fake", row("Q01", "흐림 요약"))
    assert not j["parsed"] and j["verdict"] is None


def test_mid_run_reload_is_detected():
    """설계 의도 4 — 워밍업 뒤 로드 시간이 RELOAD_SEC 를 넘은 응답을 찾아낸다(OBS-005)."""
    rows = [row("Q01", "보스방 튕김"), row("Q02", "다른 요약")]
    _, reloads = qa.run_judge(FakeJudge(reload_at="다른 요약"), "fake", rows, echo=False)
    assert reloads and reloads[0].startswith("Q02")


def test_measure_mode_does_not_print_judgements(capsys):
    """측정 모드(echo=False)는 판정 내용을 화면에 내지 않는다."""
    qa.run_judge(FakeJudge(), "fake", [row("Q01", "흐림 요약")], echo=False)
    assert capsys.readouterr().out == ""


# ── 지표·판정 (계획 §6) ──────────────────────────────────────

def make(n_nat_agree: int, _unused: int, inj_hit: int, human_yes_nat: int = 0):
    """자연 31(일치 n_nat_agree, 나머지는 불일치) + 주입 15(채점관 검출 inj_hit). 불일치한 건은 자동으로 거짓 경보(또는 놓침)가 된다."""
    key, human, judged = {}, {}, {}
    n = 0
    for k in range(31):
        n += 1
        j = f"Q{n:02d}"
        key[j] = {"injected": False, "summary": "s", "item_id": f"N{k:02d}"}
        hy = k < human_yes_nat
        human[j] = {"verdict": qa.YES if hy else qa.NO, "types": ["Q1"] if hy else [], "확신도": "높음"}
        agree = k < n_nat_agree
        verdict = human[j]["verdict"] if agree else (qa.YES if not hy else qa.NO)
        judged[j] = {"verdict": verdict, "types": [], "phrase": "", "parsed": True}
    for k in range(15):
        n += 1
        j = f"Q{n:02d}"
        kind = ["Q1", "Q2", "Q3"][k % 3]
        key[j] = {"injected": True, "kind": kind, "summary": "s 흐림", "item_id": f"I{k:02d}"}
        human[j] = {"verdict": qa.YES, "types": [kind], "확신도": "높음"}
        judged[j] = ({"verdict": qa.YES, "types": [kind], "phrase": "흐림", "parsed": True} if k < inj_hit
                     else {"verdict": qa.NO, "types": [], "phrase": "", "parsed": True})
    return key, human, judged


def test_boundary_values_pass(crit):
    """기준값과 같으면 충족: 일치 27/31, 검출 12/15."""
    key, human, judged = make(27, 4, 12, human_yes_nat=5)
    mx = qa.metrics(key, human, judged, crit)
    assert mx["natural_agree"] == 27 and mx["injected_detected"] == 12 and mx["injected_recall"] == 80.0
    names = {c["name"]: c["passed"] for c in qa.decide(mx, crit)["checks"]}
    assert names["자연 응답 일치율(%)"] and names["심은 결함 검출률(%)"]


def test_below_boundary_fails(crit):
    key, human, judged = make(26, 5, 11)
    mx = qa.metrics(key, human, judged, crit)
    v = qa.decide(mx, crit)
    assert not v["adopted"] and not any(c["passed"] for c in v["checks"] if c["name"] in ("자연 응답 일치율(%)", "심은 결함 검출률(%)"))


def test_perfect_judge_is_adopted(crit):
    key, human, judged = make(31, 0, 15, human_yes_nat=6)
    mx = qa.metrics(key, human, judged, crit)
    v = qa.decide(mx, crit)
    assert v["adopted"] and mx["kappa_used"] and mx["natural_kappa"] == 1.0 and mx["injected_type_match"] == 15


def test_kappa_is_used_only_with_enough_positives(crit):
    key, human, judged = make(31, 0, 15, human_yes_nat=4)
    mx = qa.metrics(key, human, judged, crit)
    assert not mx["kappa_used"] and "자연 응답 κ" not in [c["name"] for c in qa.decide(mx, crit)["checks"]]
    key, human, judged = make(31, 0, 15, human_yes_nat=5)
    assert qa.metrics(key, human, judged, crit)["kappa_used"]


def test_false_alarms_fail(crit):
    """검토자가 없음으로 본 26건 중 5건을 채점관이 있음으로 보면 19.2% — 기준 초과."""
    key, human, judged = make(26, 5, 15, human_yes_nat=5)
    mx = qa.metrics(key, human, judged, crit)
    names = {c["name"]: c["passed"] for c in qa.decide(mx, crit)["checks"]}
    assert mx["false_alarm"] == 5 and names["거짓 경보율(%)"] is False


def test_unparsed_counts_as_disagreement_miss_and_false_alarm(crit):
    """기준 파일 규칙 5 — 읽지 못한 건은 불일치·놓침·거짓 경보로 센다(가장 불리하게)."""
    key, human, judged = make(31, 0, 15)
    judged["Q01"] = {"verdict": None, "types": [], "phrase": "", "parsed": False}
    judged["Q32"] = {"verdict": None, "types": [], "phrase": "", "parsed": False}
    mx = qa.metrics(key, human, judged, crit)
    assert mx["natural_agree"] == 30 and mx["false_alarm"] == 1 and mx["injected_detected"] == 14 and mx["unparsed"] == 2


def test_zero_denominator_fails_false_alarm_check(crit):
    key, human, judged = make(31, 0, 15, human_yes_nat=31)
    mx = qa.metrics(key, human, judged, crit)
    names = {c["name"]: c["passed"] for c in qa.decide(mx, crit)["checks"]}
    assert mx["false_alarm_rate"] is None and names["거짓 경보율(%)"] is False


def test_near_boundary_flag(crit):
    key, human, judged = make(26, 5, 15)
    assert qa.metrics(key, human, judged, crit)["near_boundary"]
    key, human, judged = make(31, 0, 15)
    assert not qa.metrics(key, human, judged, crit)["near_boundary"]


# ── 검토자 판정 파일 ─────────────────────────────────────────

def _answers(tmp_path, mutate=None, n=46):
    ids = [f"Q{i:02d}" for i in range(1, n + 1)]
    answers = {j: {"판정": qa.NO, "유형": [], "확신도": "높음", "메모": ""} for j in ids}
    if mutate:
        mutate(answers)
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"answers": answers}, ensure_ascii=False), encoding="utf-8")
    return path, ids


def test_rater_answers_are_read(tmp_path):
    path, ids = _answers(tmp_path)
    assert sorted(qa.read_rater_answers(path, ids)) == ids


@pytest.mark.parametrize("mutate,expected", [
    (lambda a: a["Q01"].update({"판정": ""}), "빈칸"),
    (lambda a: a["Q01"].update({"판정": "품질 문제 있음", "유형": []}), "유형이 판정과 맞지 않습니다"),
    (lambda a: a["Q01"].update({"유형": ["Q1"]}), "유형이 판정과 맞지 않습니다"),
    (lambda a: a.pop("Q02"), "번호가 측정 세트와 다릅니다"),
])
def test_incomplete_rater_answers_stop_before_judge(tmp_path, mutate, expected):
    path, ids = _answers(tmp_path, mutate)
    with pytest.raises(GateError, match=expected):
        qa.read_rater_answers(path, ids)


def test_committed_rater_answers_cover_measure_set():
    """커밋 대상 검토자 판정이 측정 세트 46건을 빠짐없이 덮는다(측정 전 확인용)."""
    key = json.loads((ROOT / "data/quality_v15/answer_key_v15.json").read_text(encoding="utf-8"))["map"]
    qa.read_rater_answers(qa.RATER_ANSWERS, list(key))


# ── 개발용·측정 모드 ─────────────────────────────────────────

def test_dev_mode_never_reads_measure_set(monkeypatch, tmp_path, crit):
    """개발용 모드는 개발용 자료만 채점관에 넣는다 (계획 §5)."""
    monkeypatch.setattr(qa, "DEV_RESULT", tmp_path / "dev.json")
    monkeypatch.setattr(qa, "get_model_digests", lambda c, ms: {m: "fake" for m in ms})
    fake = FakeJudge()
    assert qa.run_dev(fake, crit) == 0
    measure = json.loads((ROOT / "data/quality_v15/measure_set_v15.json").read_text(encoding="utf-8"))
    dev = json.loads((ROOT / "data/quality_v15/dev_set_v15.json").read_text(encoding="utf-8"))
    sent = "\n".join(fake.prompts)
    assert len(fake.prompts) == len(dev["items"]) == 63
    only_measure = {r["요약"] for r in measure["items"]} - {d["summary"] for d in dev["items"]}
    assert not any(f"[요약]\n{s}\n" in sent for s in only_measure)


def test_committed_dev_result_matches_prompt():
    """커밋된 개발용 결과가 지금 프롬프트로 만든 것이다."""
    dev = json.loads((ROOT / "data/quality_v15/dev_result_v15.json").read_text(encoding="utf-8"))
    assert dev["prompt_version"] == qa.PROMPT_VERSION and dev["prompt_sha256"] == qa.prompt_fingerprint()
    assert dev["options"] == qa.OPTIONS and dev["reloads"] == [] and dev["summary"]["unparsed"] == 0


def _patch_outputs(monkeypatch, tmp_path):
    monkeypatch.setattr(qa, "RESULT_JSON", tmp_path / "result.json")
    monkeypatch.setattr(qa, "REPORT", tmp_path / "report.md")
    monkeypatch.setattr(qa, "INVALID_RUNS", tmp_path / "invalid.json")
    monkeypatch.setattr(qa, "STARTED", tmp_path / "started.json")
    monkeypatch.setattr(qa, "get_model_digests", lambda c, ms: {m: "fake" for m in ms})


def _fake_rater_answers(monkeypatch, tmp_path):
    key = json.loads((ROOT / "data/quality_v15/answer_key_v15.json").read_text(encoding="utf-8"))["map"]
    path, _ = _answers(tmp_path, n=len(key))
    monkeypatch.setattr(qa, "RATER_ANSWERS", path)


def test_measure_refuses_no_write(crit):
    with pytest.raises(GateError, match="no-write"):
        qa.run_measure(FakeJudge(), crit, write=False)


def test_measure_full_flow_with_fake_judge(monkeypatch, tmp_path, crit):
    """가짜 판정으로 지표 → 판정 → 보고서까지 끝난다 (측정은 한 번뿐이라 마지막 단계가 죽으면 안 된다)."""
    _patch_outputs(monkeypatch, tmp_path)
    _fake_rater_answers(monkeypatch, tmp_path)
    code = qa.run_measure(FakeJudge(), crit, guard=False)
    assert code in (0, 1)
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["metrics"]["natural_n"] == 31 and result["metrics"]["injected_n"] == 15
    assert "## 판정:" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_measure_saves_result_before_printing(monkeypatch, tmp_path, crit):
    """설계 의도 7 (ISSUE-014) — 화면 출력이 실패해도 측정 결과는 이미 파일로 남아 있다."""
    _patch_outputs(monkeypatch, tmp_path)
    _fake_rater_answers(monkeypatch, tmp_path)

    def failing_print(*args, **kwargs):
        if "✅" in str(args) or "❌" in str(args):
            raise UnicodeEncodeError("cp949", "✅", 0, 1, "illegal multibyte sequence")

    monkeypatch.setattr("builtins.print", failing_print)
    with pytest.raises(UnicodeEncodeError):
        qa.run_measure(FakeJudge(), crit, guard=False)
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["metrics"]["natural_n"] == 31
    assert (tmp_path / "report.md").exists()


def test_cli_prints_on_cp949_console(monkeypatch):
    """설계 의도 7 — cp949 콘솔에서도 ✅·한글 출력이 멈추지 않는다. 실제 측정은 하지 않게 run_measure 를 막는다."""
    out = io.TextIOWrapper(io.BytesIO(), encoding="cp949")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp949"))

    def stop(*a, **k):
        raise GateError("시험")

    monkeypatch.setattr(qa, "run_measure", stop)
    assert qa.main(["--measure"]) == 2
    print("✅")
    out.flush()
    assert out.encoding == "utf-8" and "✅" in out.buffer.getvalue().decode("utf-8")


def test_measure_with_connection_error_is_invalid(monkeypatch, tmp_path, crit):
    """통신 오류가 나면 판정하지 않고 무효 실행을 기록한다."""
    _patch_outputs(monkeypatch, tmp_path)
    _fake_rater_answers(monkeypatch, tmp_path)
    measure = json.loads((ROOT / "data/quality_v15/measure_set_v15.json").read_text(encoding="utf-8"))
    with pytest.raises(GateError, match="측정 환경 오염"):
        qa.run_measure(FakeJudge(raise_at=measure["items"][5]["요약"]), crit, guard=False)
    assert not (tmp_path / "result.json").exists()
    assert "통신 오류" in json.loads((tmp_path / "invalid.json").read_text(encoding="utf-8"))[0]["reasons"][0]


def test_measure_guard_refuses_existing_result_and_digest_mismatch(monkeypatch, tmp_path, crit):
    """설계 의도 7 — 결과 파일이 이미 있거나 모델 digest가 개발용 실행과 다르면 측정하지 않는다."""
    monkeypatch.setattr(qa, "get_model_digests", lambda c, ms: {m: "fake" for m in ms})
    existing = tmp_path / "result.json"
    existing.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(qa, "RESULT_JSON", existing)
    with pytest.raises(GateError) as e:
        qa.measure_guard(FakeJudge(), crit)
    assert "이미 있습니다" in str(e.value) and "digest" in str(e.value)


def test_measure_guard_refuses_uncommitted_guarded_file(monkeypatch, tmp_path, crit):
    """검토자 판정 파일 등 보호 파일이 커밋되어 있지 않거나 바뀌었으면 측정하지 않는다."""
    monkeypatch.setattr(qa, "get_model_digests", lambda c, ms: {m: "fake" for m in ms})
    monkeypatch.setattr(qa, "RESULT_JSON", tmp_path / "none.json")
    monkeypatch.setattr(qa, "git_lines", lambda *a: [])    # ls-files 가 비면 커밋되지 않은 것
    with pytest.raises(GateError, match="커밋되어 있지 않습니다"):
        qa.measure_guard(FakeJudge(), crit)
    monkeypatch.setattr(qa, "git_lines", lambda *a: ["x"] if a[0] == "ls-files" else [" M quality_v15.toml"])
    with pytest.raises(GateError, match="커밋하지 않은 변경"):
        qa.measure_guard(FakeJudge(), crit)


def test_guarded_files_include_rater_answers_and_exist():
    assert "data/quality_v15/rater_answers_v15.json" in qa.GUARDED and "src/triage_eval/pipeline/quality.py" in qa.GUARDED
    assert all((ROOT / f).exists() for f in qa.GUARDED)


# ── 독립 검토 반영 ───────────────────────────────────────────

def _mx(**kw):
    base = {"natural_agreement": 90.0, "injected_recall": 90.0, "false_alarm_rate": 10.0, "kappa_used": True, "natural_kappa": 0.8}
    return {**base, **kw}


def test_equal_to_limit_passes_for_every_metric(crit):
    """기준값과 같으면 충족 — 일치율 85.0·검출률 80.0·거짓 경보율 15.0·κ 0.61 모두 같으면 채택, 조금만 나빠도 미채택."""
    ok = dict(natural_agreement=85.0, injected_recall=80.0, false_alarm_rate=15.0, natural_kappa=0.61)
    assert qa.decide(_mx(**ok), crit)["adopted"]
    for bad in ({"natural_agreement": 84.99}, {"injected_recall": 79.99}, {"false_alarm_rate": 15.01}, {"natural_kappa": 0.6099}):
        assert not qa.decide(_mx(**{**ok, **bad}), crit)["adopted"]


def test_measure_full_flow_prints_no_judgements(monkeypatch, tmp_path, crit, capsys):
    _patch_outputs(monkeypatch, tmp_path)
    _fake_rater_answers(monkeypatch, tmp_path)
    qa.run_measure(FakeJudge(), crit, guard=False)
    out = capsys.readouterr().out
    assert "Q01:" not in out and "문제 문구" not in out and "판정:" in out


def test_cli_measure_keeps_guard_on(monkeypatch):
    """main 은 항상 가드를 켠 채로 측정한다 — 가드가 막으면 종료 코드 2, 채점관은 실행되지 않는다."""
    called = []

    def blocked(*a, **k):
        raise GateError("막힘")

    monkeypatch.setattr(qa, "measure_guard", blocked)
    monkeypatch.setattr(qa, "run_judge", lambda *a, **k: called.append(1))
    assert qa.main(["--measure"]) == 2 and not called


def test_started_marker_blocks_second_measurement(monkeypatch, tmp_path, crit):
    """설계 의도 8 — 측정이 중간에 끊겨 결과 파일이 없어도, 시작 표시가 있으면 다시 측정하지 않는다."""
    _patch_outputs(monkeypatch, tmp_path)
    (tmp_path / "started.json").write_text("{}", encoding="utf-8")
    with pytest.raises(GateError, match="시작한 기록"):
        qa.measure_guard(FakeJudge(), crit)


def test_crash_after_judging_keeps_raw_results(monkeypatch, tmp_path, crit):
    """판정 46건 뒤 지표 계산에서 예외가 나도 판정 원자료가 남고, 시작 표시도 남아 재측정이 막힌다."""
    _patch_outputs(monkeypatch, tmp_path)
    _fake_rater_answers(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("중간 실패")

    monkeypatch.setattr(qa, "metrics", boom)
    with pytest.raises(RuntimeError):
        qa.run_measure(FakeJudge(), crit, guard=False)
    raw = json.loads((tmp_path / "started.json").read_text(encoding="utf-8"))
    assert len(raw["results"]) == 46 and not (tmp_path / "result.json").exists()


def test_invalid_run_clears_started_marker(monkeypatch, tmp_path, crit):
    """재로드·통신 오류로 무효가 되면 무효 기록을 남기고 시작 표시를 지워 재측정을 허용한다(OBS-005)."""
    _patch_outputs(monkeypatch, tmp_path)
    _fake_rater_answers(monkeypatch, tmp_path)
    measure = json.loads((ROOT / "data/quality_v15/measure_set_v15.json").read_text(encoding="utf-8"))
    with pytest.raises(GateError):
        qa.run_measure(FakeJudge(raise_at=measure["items"][3]["요약"]), crit, guard=False)
    assert not (tmp_path / "started.json").exists() and (tmp_path / "invalid.json").exists()


def test_fingerprint_covers_whole_input(monkeypatch):
    """설계 의도 9 — 프롬프트 틀·OPTIONS·THINK 가 바뀌면 지문이 바뀐다."""
    base = qa.prompt_fingerprint()
    with monkeypatch.context() as m:
        m.setattr(qa, "OPTIONS", {**qa.OPTIONS, "num_predict": 200})
        assert qa.prompt_fingerprint() != base
    with monkeypatch.context() as m:
        m.setattr(qa, "THINK", True)
        assert qa.prompt_fingerprint() != base
    with monkeypatch.context() as m:
        orig = qa.build_prompt
        m.setattr(qa, "build_prompt", lambda r: orig(r).replace("[요약]", "[요약문]"))
        assert qa.prompt_fingerprint() != base


def test_guard_refuses_prompt_fingerprint_mismatch(monkeypatch, tmp_path, crit):
    _patch_outputs(monkeypatch, tmp_path)
    monkeypatch.setattr(qa, "git_lines", lambda *a: ["x"] if a[0] == "ls-files" else [])
    monkeypatch.setattr(qa, "prompt_fingerprint", lambda: "다른 지문")
    with pytest.raises(GateError, match="프롬프트 지문"):
        qa.measure_guard(FakeJudge(), crit)


def test_bad_confidence_in_rater_answers_is_refused(tmp_path):
    path, ids = _answers(tmp_path, lambda a: a["Q01"].update({"확신도": "아주 높음"}))
    with pytest.raises(GateError, match="확신도"):
        qa.read_rater_answers(path, ids)


def test_unparsed_enters_kappa_as_third_category(crit):
    key, human, judged = make(31, 0, 15, human_yes_nat=6)
    judged["Q01"] = {"verdict": None, "types": [], "phrase": "", "parsed": False}   # 검토자 있음을 읽지 못함
    mx = qa.metrics(key, human, judged, crit)
    assert mx["natural_agree"] == 30 and mx["natural_kappa"] < 1.0 and mx["natural_judge_detected"] == 5
