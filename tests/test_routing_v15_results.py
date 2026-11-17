"""v1.5 확신도 기반 라우팅 — 결과 보고서가 측정 결과 파일과 맞는지 확인한다 (report/routing_v15_results.md)."""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
RESULT = ROOT / "data" / "results" / "v15" / "routing" / "routing_dev_v15.json"
REPORT = ROOT / "report" / "routing_v15_results.md"

pytestmark = pytest.mark.skipif(not RESULT.exists(), reason="측정 전")


@pytest.fixture(scope="module")
def result():
    return json.loads(RESULT.read_text(encoding="utf-8"))


def test_selection_and_summary_numbers_match_result(result):
    text = REPORT.read_text(encoding="utf-8")
    summary = text.split("\n## 1.")[0]
    sel = result["selection"]
    m = sel["selected_metrics"]
    assert "+".join(sel["selected"]) in summary
    assert f"{m['bad_total']}건 중 {m['bad_auto']}건" in summary
    assert f"{m['auto_rate']}%" in summary
    assert "채택 판정은 하지 않았다" in summary


def _line(text, label):
    lines = [ln for ln in text.splitlines() if ln.startswith(label)]
    assert len(lines) == 1, label
    return lines[0]


def test_result_table_rows_match_result(result):
    """결과 표(§3)의 각 행에서 구성별 값이 같은 줄에 있다."""
    text = REPORT.read_text(encoding="utf-8").split("## 3.")[1].split("## 4.")[0]
    steps = {s["signal"]: s for s in result["selection"]["steps"]}
    rows = {"| R0": result["baseline"]["R0"], "| R1": result["baseline"]["R1"], "| **+S2": steps["S2"]["metrics_to"],
            "| +S4": steps["S4"]["metrics_to"], "| +S3": steps["S3"]["metrics_to"]}
    for label, m in rows.items():
        ln = _line(text, label)
        x1, x2, x3 = (f"{m['risk_auto'][k]}/{m['risk_total'][k]}" for k in ("X-1", "X-2", "X-3"))
        assert f"{m['auto_rate']}%" in ln and f"{x1} · {x2} · {x3}" in ln
        assert f"{m['bad_auto']}/{m['bad_total']} ({m['bad_auto_items']})" in ln and f"{m['accuracy_auto']}%" in ln
        if m["review"]:
            assert f"{m['review_precision']}%" in ln


def test_single_signal_sentence_matches_result(result):
    """§3의 '신호 하나씩만 썼을 때' 문장이 신호별 검토 건수와 위험·날조 검토 수를 정확히 적었다."""
    text = REPORT.read_text(encoding="utf-8")
    para = next(ln for ln in text.splitlines() if "신호 하나씩만 썼을 때" in ln)
    for sig, v in result["single_signal"].items():
        hit = f"{sig} {v['review']}건"
        assert hit in para
        rest = para.split(hit)[1][:20]
        assert f"{v['bad_caught']}/{v['bad_total']}" in rest


def test_body_counts_match_rows(result):
    """§4 본문의 응답·문항 수가 원자료와 같다."""
    text = REPORT.read_text(encoding="utf-8")
    assert result["n"] == 378 and "378응답" in text
    sel = result["selection"]["selected_metrics"]
    assert f"{sel['bad_auto_items']}개 문항" in text and f"{sel['bad_total']}건" in text
    assert round(sel["bad_auto"] / sel["bad_total"] * 100, 1) == 68.2 and "68.2%" in text


def test_step9_criteria_stay_as_registered():
    """9단계 판정 기준(§6)은 새 세트를 만들기 전에 확정한 값이다. 각 값이 해당 행·순서에 붙어 있어야 한다."""
    sec = REPORT.read_text(encoding="utf-8").split("## 6.")[1].split("## 7.")[0]
    lines = sec.splitlines()

    def row(label):
        return next(ln for ln in lines if ln.startswith(label))

    assert "S1+S2 고정" in row("| 대상 신호 집합")
    assert "30% 이상" in row("| 자동 처리율")
    assert "**0건**" in row("| **목표선")
    assert "**50% 이하**" in row("| **하한선")
    assert "10건 미만" in row("| 표본 부족") and "5개 미만" in row("| 표본 부족")
    assert "사람이 확인" in row("| 날조 판정")
    order = [ln for ln in lines if ln[:2] in ("1.", "2.", "3.", "4.", "5.")]
    assert "표본 부족" in order[0] and "30% 미만" in order[1] and "채택" in order[2] and "부분 채택" in order[3]
