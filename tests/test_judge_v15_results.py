"""v1.5 LLM-as-judge — 결과 보고서와 사후 의견 기록이 측정 결과 파일과 맞는지 확인한다 (report/judge_v15_results.md)."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "judge_v15"
RESULT = DATA / "judge_result_v15.json"
REPORT = ROOT / "report" / "judge_v15_results.md"

pytestmark = pytest.mark.skipif(not RESULT.exists(), reason="측정 전")


@pytest.fixture(scope="module")
def result():
    return json.loads(RESULT.read_text(encoding="utf-8"))


def test_report_numbers_match_result(result):
    """보고서에 적은 판정·지표 숫자가 결과 파일과 같다."""
    text = REPORT.read_text(encoding="utf-8")
    m, v = result["metrics"], result["verdict"]
    assert ("미채택" if not v["adopted"] else "채택") in text.split("\n## 1.")[0]
    assert f"{m['natural_agreement']}% ({m['natural_n']}건 중 {m['natural_agree']}건)" in text
    assert f"{m['injected_recall']:g}% ({m['injected_n']}건 중 {m['injected_detected']}건)" in text
    assert f"{m['false_alarm_rate']}% ({m['false_alarm']}건)" in text
    assert f"| 자연 응답 κ | {round(m['natural_kappa'], 2)} |" in text
    assert f"| 계 | {m['injected_n']} | **{m['injected_detected']}** | {m['rater_injected_detected']} | {result['detector']['injected_detected']} |" in text


def test_post_hoc_items_are_the_disagreements(result):
    """사후 의견을 받은 문항은 실제로 판정이 갈린 문항·검토자가 놓친 심은 날조와 같다 (판정은 바꾸지 않음)."""
    key = json.loads((DATA / "answer_key_v15.json").read_text(encoding="utf-8"))["map"]
    notes = json.loads((DATA / "post_hoc_rater_notes_v15.json").read_text(encoding="utf-8"))
    h, j = result["human"], result["judge"]
    yes = "날조 있음"
    natural = [x for x in key if not key[x]["injected"] and key[x]["item_id"] not in {e["item_id"] for e in result["metrics"]["excluded"]}]
    assert sorted(notes["rater_yes_judge_no_natural"]) == sorted(x for x in natural if h[x]["verdict"] == yes and j[x]["verdict"] != yes)
    assert sorted(notes["judge_yes_rater_no_natural"]) == sorted(x for x in natural if j[x]["verdict"] == yes and h[x]["verdict"] != yes)
    assert sorted(notes["injected_missed_by_rater"]) == sorted(x for x in key if key[x]["injected"] and h[x]["verdict"] != yes)
    assert json.loads((DATA / "rater_answers_v15.json").read_text(encoding="utf-8"))["answers"]["J13"]["판정"] == h["J13"]["verdict"]
