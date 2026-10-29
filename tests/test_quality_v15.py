"""v1.5 요약 품질 LLM-as-judge — 기준 파일, 측정 세트·결함 주입·개발용 자료·연습 문항 생성, 채점 화면 서버 테스트."""

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import pytest

from triage_eval.pipeline import quality
from triage_eval.pipeline.gate import GateError

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, file: str):
    """tools/judge_v15/build_set.py 와 파일 이름이 같아, 경로로 다른 이름을 붙여 불러온다."""
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / "quality_v15" / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qb = _load("quality_build_set", "build_set.py")
rs = _load("quality_rater_server", "rater_server.py")

DATA = ROOT / "data" / "quality_v15"


@pytest.fixture(scope="module")
def built():
    return qb.build()


# ── 기준 파일 ────────────────────────────────────────────────

def test_criteria_match_plan():
    c = quality.load_criteria()
    assert c["meta"]["judge_model"] == "gemma4:12b"
    assert c["natural"] == {"agreement_min": 85.0, "kappa_min": 0.61, "min_positive_for_kappa": 5, "false_alarm_max": 15.0}
    assert c["injected"] == {"recall_min": 80.0}


def test_criteria_typo_is_rejected(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text(quality.CRITERIA.read_text(encoding="utf-8").replace("recall_min", "recal_min"), encoding="utf-8")
    with pytest.raises(GateError, match="recal_min"):
        quality.load_criteria(bad)


# ── 측정 세트 (계획 §3) ──────────────────────────────────────

def test_committed_files_are_reproduced(built):
    for name, payload in zip(qb.FILES, built):
        assert json.loads(qb.jb.serialize(payload)) == json.loads((DATA / name).read_text(encoding="utf-8"))


def test_composition(built):
    """설계 의도 3·4 — 남은 리포트 46개 한 번씩, 결함 주입 15건 유형별 5건, 대화에서 언급된 리포트·A06 없음."""
    _, key, _, _ = built
    entries = list(key["map"].values())
    assert sorted(key["map"]) == [f"Q{n:02d}" for n in range(1, 47)]
    ids = {e["item_id"] for e in entries}
    assert sorted(ids) == sorted(set(qb.jb.load_items()) - set(qb.EXCLUDED_REPORTS)) and len(ids) == 46
    assert not ids & set(qb.CHAT_EXCLUDED) and set(qb.CHAT_EXCLUDED) <= set(qb.EXCLUDED_REPORTS)
    inj = [e for e in entries if e["injected"]]
    assert Counter(e["kind"] for e in inj) == {"Q1": 5, "Q2": 5, "Q3": 5}
    assert all(e["model"] == "qwen2.5:7b" for e in entries)


def test_no_summary_the_rater_has_seen(built):
    """설계 의도 2 — 측정 세트의 요약과 주입 원래 요약은 같은 리포트에서 검토자가 본 요약과 유사도 0.85 미만."""
    _, key, _, _ = built
    seen = qb.seen_by_report()
    for e in key["map"].values():
        for text in {e["summary"], e.get("original_summary", e["summary"])}:
            assert all(qb.similarity(text, x) < qb.SIMILAR for x in seen.get(e["item_id"], [])), e["item_id"]


def test_measure_set_hides_source_and_injection(built):
    measure, key, _, _ = built
    allowed = {"번호", "입력 종류", "게시판 / 작성자", "게시 일시", "제목", "본문", "참고: 업데이트 공지", "참고: 기존 등록 이슈", "요약"}
    assert all(set(r) == allowed for r in measure["items"])
    text = json.dumps(measure, ensure_ascii=False)
    assert "injected" not in text and "item_id" not in text and "qwen" not in text and "kind" not in text


def test_surface_is_not_a_cue(built):
    """설계 의도 9 — 평균 길이 차이 3자 이내(날조 측정은 39자 대 25자), "~다" 종결 비율 차이 0.1 이내."""
    key = built[1]["map"]
    gap = qb.length_gap(key)
    assert abs(gap["injected_mean"] - gap["natural_mean"]) <= 3
    inj = [e["summary"] for e in key.values() if e["injected"]]
    nat = [e["summary"] for e in key.values() if not e["injected"]]
    assert abs(sum(x.endswith("다") for x in inj) / len(inj) - sum(x.endswith("다") for x in nat) / len(nat)) <= 0.1
    nominal = lambda xs: sum(x[-1] in "임음함됨" for x in xs) / len(xs)   # noqa: E731
    assert abs(nominal(inj) - nominal(nat)) <= 0.1


def test_definition_is_the_same_everywhere():
    """계획 §2 — 유형 Q2 문구와 우선순위 문구가 채점 화면·계획·기준 파일에 글자까지 같다."""
    q2 = "원문과 다른 뜻으로 읽힌다. 이슈의 성격이 바뀐 경우(건의 → 오류, 의심 → 확정 등)도 포함"
    pri = "이슈의 성격을 바꾸는 서술(건의 → 오류, 의심 → 확정 등)은 날조가 아니라 Q2로 본다."
    ui = (ROOT / "tools/quality_v15/rater_ui.html").read_text(encoding="utf-8")
    plan = (ROOT / "report/quality_v15_plan.md").read_text(encoding="utf-8")
    toml = (ROOT / "quality_v15.toml").read_text(encoding="utf-8")
    assert q2 in ui and q2 in plan and pri in ui and pri in plan and pri in toml


@pytest.mark.parametrize("mutate,expected", [
    (lambda k, o, i, a: (k, o + "!", i, a), "고른 요약과 다릅니다"),
    (lambda k, o, i, a: (k, o, i + " 서버점검중", a), "없는 낱말"),
    (lambda k, o, i, a: (k, o, i + " 갤럭시 S24", a + ["갤럭시", "S24"]), "탐지기"),
    (lambda k, o, i, a: (k, o, o, a), "같습니다"),
    (lambda k, o, i, a: (k, o, i, a + ["없는낱말"]), "맞지 않는 항목"),
])
def test_bad_injection_is_refused(mutate, expected):
    """설계 의도 5 — 원래 요약 불일치, 신고하지 않은 새 낱말, 새 구체 사실(탐지기), 바뀌지 않은 요약은 거부."""
    items = qb.jb.load_items()
    i, inj = "A02", qb.DEV_INJECTIONS["A02"]          # 개발용 주입으로 시험한다(측정 세트 문항을 테스트에 적지 않는다)
    pick = {i: inj[1]}
    with pytest.raises(ValueError, match=expected):
        qb.check_injections({i: mutate(*inj)}, items, pick)


def test_dev_set_is_separate(built):
    """설계 의도 6."""
    _, key, dev, _ = built
    in_measure = {e["item_id"]: e["summary"] for e in key["map"].values()}
    assert all(d["summary"] != in_measure.get(d["item_id"]) for d in dev["items"])
    dev_inj = {d["item_id"] for d in dev["items"] if d["injected"]}
    assert len(dev["items"]) == 63 and len(dev_inj) == 6 and not dev_inj & set(qb.INJECTIONS)
    assert Counter(qb.DEV_INJECTIONS[i][0] for i in dev_inj) == {"Q1": 2, "Q2": 2, "Q3": 2}


def test_practice_is_outside_measure_set(built):
    """설계 의도 7 — 연습 문항은 측정 세트의 리포트·요약을 쓰지 않는다."""
    measure, _, _, practice = built
    titles = {r["제목"] for r in measure["items"]} | {r["요약"] for r in measure["items"]}
    assert len(practice["items"]) == 5
    assert not ({p["제목"] for p in practice["items"]} | {p["요약"] for p in practice["items"]}) & titles


def test_sealed_files_are_not_overwritten(monkeypatch, tmp_path, built):
    monkeypatch.setattr(qb, "OUT_DIR", tmp_path)
    payloads = dict(zip(qb.FILES, built))
    assert qb.write_sealed(payloads) == [] and qb.write_sealed(payloads) == []
    changed = {**payloads, qb.FILES[0]: {**payloads[qb.FILES[0]], "seed": 0}}
    assert qb.write_sealed(changed) == [qb.FILES[0]]


# ── 채점 화면 서버 (계획 §4) ─────────────────────────────────

@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(rs, "ANSWERS", tmp_path / "answers.json")
    monkeypatch.setattr(rs, "PRACTICE_ANSWERS", tmp_path / "practice.json")
    srv = rs.ThreadingHTTPServer(("127.0.0.1", 0), rs.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", tmp_path
    srv.shutdown()


def post(url, payload):
    req = urllib.request.Request(url + "/api/answer", data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def answer(**kw):
    return {"세트": "measure", "번호": "Q01", "판정": "품질 문제 있음", "유형": ["Q2"], "확신도": "보통", "메모": "", **kw}


def test_server_reads_only_measure_and_practice():
    """설계 의도 1 — 서버 코드에 정답 대응표·개발용 자료 경로가 없고, 화면 상태에 주입 정보가 없다."""
    src = (ROOT / "tools" / "quality_v15" / "rater_server.py").read_text(encoding="utf-8")
    assert "answer_key" not in src and "dev_set" not in src
    text = json.dumps(rs.state(), ensure_ascii=False)
    assert "injected" not in text and "original_summary" not in text and "kind_name" not in text


def test_server_saves_and_keeps_history(server):
    """설계 의도 2 — 저장 즉시 파일에 쓰고, 다른 값으로 다시 저장하면 이력, 같은 값이면 이력 없음."""
    url, tmp = server
    assert post(url, answer())[0] == 200
    assert post(url, answer())[0] == 200
    assert post(url, answer(판정="품질 문제 없음", 유형=[]))[0] == 200
    data = json.loads((tmp / "answers.json").read_text(encoding="utf-8"))
    assert data["answers"]["Q01"]["판정"] == "품질 문제 없음" and len(data["changes"]) == 1
    with urllib.request.urlopen(url + "/api/state") as r:
        assert json.loads(r.read())["answers"]["Q01"]["판정"] == "품질 문제 없음"


@pytest.mark.parametrize("bad,expected", [
    ({"유형": []}, "유형을 하나 이상"),
    ({"판정": "품질 문제 없음"}, "유형을 비워"),
    ({"확신도": "매우"}, "확신도"),
    ({"판정": None}, "판정"),
    ({"번호": "Q99"}, "없는 번호"),
    ({"유형": ["Q4"]}, "Q1·Q2·Q3"),
    ({"세트": "dev"}, "세트"),
])
def test_server_rejects_bad_answers(server, bad, expected):
    """설계 의도 3."""
    url, tmp = server
    code, body = post(url, answer(**bad))
    assert code == 400 and expected in body["error"] and not (tmp / "answers.json").exists()


def test_practice_answers_go_to_separate_file(server):
    """설계 의도 4."""
    url, tmp = server
    assert post(url, answer(세트="practice", 번호="P2", 유형=["Q1"]))[0] == 200
    assert (tmp / "practice.json").exists() and not (tmp / "answers.json").exists()


def test_practice_reference_only_after_saving(server):
    """설계 의도 4 — 저장 전 상태에는 참고 답이 없고, 저장하면 응답과 상태에 나온다."""
    url, _ = server
    with urllib.request.urlopen(url + "/api/state") as r:
        assert all("참고 답" not in p for p in json.loads(r.read())["practice"])
    code, body = post(url, answer(세트="practice", 번호="P1", 판정="품질 문제 없음", 유형=[]))
    assert code == 200 and body["saved"]["reference"]["참고 답"]
    with urllib.request.urlopen(url + "/api/state") as r:
        practice = {p["번호"]: p for p in json.loads(r.read())["practice"]}
    assert "참고 답" in practice["P1"] and "참고 답" not in practice["P2"]


def test_foreign_requests_are_refused(server):
    """설계 의도 5 — 다른 Host나 JSON이 아닌 저장 요청은 거부."""
    url, tmp = server
    req = urllib.request.Request(url + "/api/answer", data=json.dumps(answer()).encode("utf-8"),
                                 headers={"Content-Type": "text/plain"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    req = urllib.request.Request(url + "/api/state", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403 and not (tmp / "answers.json").exists()


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', json.dumps({"세트": "measure", "번호": ["Q01"]}).encode("utf-8"), b"not json"])
def test_malformed_requests_get_400(server, body):
    """설계 의도 6 — 객체가 아닌 JSON·잘못된 값 형태·깨진 JSON은 400이고 서버는 계속 돈다."""
    url, _ = server
    req = urllib.request.Request(url + "/api/answer", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400
    assert post(url, answer())[0] == 200


def test_broken_answer_file_is_not_overwritten(server):
    """설계 의도 6 — 답 파일이 깨지면 500으로 알리고 덮어쓰지 않는다."""
    url, tmp = server
    (tmp / "answers.json").write_text("{깨짐", encoding="utf-8")
    assert post(url, answer())[0] == 500
    assert (tmp / "answers.json").read_text(encoding="utf-8") == "{깨짐"


def test_page_is_served(server):
    url, _ = server
    with urllib.request.urlopen(url + "/") as r:
        page = r.read().decode("utf-8")
    assert "요약 품질 채점" in page and "날조" in page and "Q1 대상 누락" in page
