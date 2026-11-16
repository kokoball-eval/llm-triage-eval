"""v1.5 요약 품질 LLM-as-judge — 결과 보고서가 측정 결과 파일과 맞는지 확인한다 (report/quality_v15_results.md)."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "quality_v15"
RESULT = DATA / "quality_result_v15.json"
STARTED = DATA / "measure_started_v15.json"
REPORT = ROOT / "report" / "quality_v15_results.md"
TABLE = ROOT / "report" / "quality_v15.md"

pytestmark = pytest.mark.skipif(not RESULT.exists(), reason="측정 전")


@pytest.fixture(scope="module")
def result():
    return json.loads(RESULT.read_text(encoding="utf-8"))


def test_report_numbers_match_result(result):
    """보고서에 적은 판정·지표 숫자가 결과 파일과 같다."""
    text = REPORT.read_text(encoding="utf-8")
    m, v = result["metrics"], result["verdict"]
    assert ("미채택" if not v["adopted"] else "채택") in text.split("\n## 1.")[0]
    summary = text.split("\n## 1.")[0]
    for value in (f"{m['natural_agreement']}%", f"{m['natural_kappa']}", f"{m['injected_recall']}%"):
        assert value in summary
    assert f"{m['natural_n']}건 중 {m['natural_agree']}건" in text
    assert f"{m['injected_n']}건 중 {m['injected_detected']}건" in text
    assert m["false_alarm"] == 0 and "거짓 경보 0%" in summary


def test_confidence_table_matches_result(result):
    text = REPORT.read_text(encoding="utf-8")
    for c, v in result["metrics"]["by_confidence"].items():
        assert f"| {c} | {v['n']} | {v['agree']} |" in text


def test_disagreements_are_listed_and_all_misses(result):
    """갈린 자연 응답 목록이 결과와 같고, 보고서 문장대로 모두 검토자 있음·채점관 없음이다."""
    key = json.loads((DATA / "answer_key_v15.json").read_text(encoding="utf-8"))["map"]
    text = REPORT.read_text(encoding="utf-8")
    diff = [j for j, e in key.items() if not e["injected"] and result["human"][j]["verdict"] != result["judge"][j]["verdict"]]
    assert len(diff) == 7 and all(f"| {j} |" in text for j in diff)
    assert all(result["human"][j]["verdict"].endswith("있음") and result["judge"][j]["verdict"].endswith("없음") for j in diff)


def test_injected_table_matches_result(result):
    """보고서의 심은 결함 표(검토자·채점관 판정)와 유형별 검출 건수가 결과와 같다."""
    key = json.loads((DATA / "answer_key_v15.json").read_text(encoding="utf-8"))["map"]
    rows = {}
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and cells[0] in key and key[cells[0]]["injected"]:
            rows[cells[0]] = cells
    inj = [j for j, e in key.items() if e["injected"]]
    assert sorted(rows) == sorted(inj)
    for j in inj:
        human = "있음" if result["human"][j]["verdict"].endswith("있음") else "없음"
        judge = "있음" if result["judge"][j]["verdict"].endswith("있음") else "없음 (놓침)"
        assert rows[j][1] == key[j]["kind"] and rows[j][3] == human and rows[j][4].replace("*", "") == judge, j
    for kind in ("Q1", "Q2", "Q3"):
        js = [j for j in inj if key[j]["kind"] == kind]
        by_judge = sum(result["judge"][j]["verdict"].endswith("있음") for j in js)
        by_human = sum(result["human"][j]["verdict"].endswith("있음") for j in js)
        assert any(c[0].startswith(kind) and c[2] == str(by_judge) and c[3] == str(by_human) for c in
                   ([x.strip() for x in line.strip("|").split("|")] for line in REPORT.read_text(encoding="utf-8").splitlines()
                    if line.startswith(f"| {kind} ") and "|" in line[3:])
                   if len(c) == 4), kind


def test_started_file_holds_the_judgements_in_result(result):
    """측정 시작 표시 파일의 원자료가 결과 파일의 채점관 판정과 같다(재측정 방지 표시이자 원자료)."""
    raw = json.loads(STARTED.read_text(encoding="utf-8"))["results"]
    assert {r["번호"]: r["verdict"] for r in raw} == {j: v["verdict"] for j, v in result["judge"].items()}


def test_result_is_a_clean_single_run(result):
    assert result["model_digest"] == result["model_digest_after"] and result["metrics"]["unparsed"] == 0
    assert result["prompt_version"] == "q3"
    dev = json.loads((DATA / "dev_result_v15.json").read_text(encoding="utf-8"))
    assert dev["prompt_sha256"] == result["prompt_sha256"] and dev["model_digest"] == result["model_digest"]
    assert TABLE.exists()
