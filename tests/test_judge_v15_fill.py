"""v1.5 LLM-as-judge — 검토자 판정 대화 입력 기록·채점 시트 생성 도구 테스트 (tools/judge_v15/fill_sheet.py, 계획 §12)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "judge_v15"))
import fill_sheet as fs  # noqa: E402

from triage_eval.pipeline.judge import NO, YES, read_rater_sheet  # noqa: E402


@pytest.fixture(scope="module")
def measure():
    return fs.load_measure()


def fill_all(measure, data=None):
    data = data or fs.load_answers(Path("없는 파일.json"))
    for n, jid in enumerate(measure):
        fs.record(data, jid, "있음" if n % 3 == 0 else "없음", "높음", "문구" if n % 3 == 0 else "", "", measure)
    return data


def test_reads_only_measure_set():
    """설계 의도 1 — 정답 대응표·개발용 자료 파일 이름이 코드에 없다."""
    src = (ROOT / "tools" / "judge_v15" / "fill_sheet.py").read_text(encoding="utf-8")
    assert "answer_key" not in src and "dev_set" not in src and "FILES[1]" not in src and "FILES[2]" not in src
    assert fs.MEASURE.name == "measure_set_v15.json"


def test_show_is_verbatim(measure):
    out = fs.show(["J01", "J02"], measure)
    for jid in ("J01", "J02"):
        row = measure[jid]
        assert f"■ {jid}" in out and row["요약"] in out and row["본문"].strip() in out
    assert "injected" not in out and "item_id" not in out


def test_record_rejects_bad_values(measure):
    data = fs.load_answers(Path("없는 파일.json"))
    for args in (("J99", "있음", "높음"), ("J01", "모름", "높음"), ("J01", "있음", "매우")):
        with pytest.raises(SystemExit):
            fs.record(data, *args, "", "", measure)
    assert data["answers"] == {}


def test_rerecord_keeps_history(measure):
    """설계 의도 2."""
    data = fs.load_answers(Path("없는 파일.json"))
    fs.record(data, "J01", "없음", "보통", "", "", measure)
    fs.record(data, "J01", "날조 있음", "낮음", "어떤 문구", "다시 봄", measure)
    assert data["answers"]["J01"]["판정"] == YES and data["answers"]["J01"]["날조 문구"] == "어떤 문구"
    assert len(data["changes"]) == 1 and data["changes"][0]["이전"]["판정"] == NO
    assert fs.status(data, measure).startswith("기록 1/63건, 고친 이력 1건, 다음 J02")


def test_same_value_adds_no_history_and_text_is_cleaned(measure):
    """설계 의도 2 — 같은 값은 이력 없음, 문구 앞뒤 공백 제거, '='로 시작하는 문구 거부."""
    data = fs.load_answers(Path("없는 파일.json"))
    fs.record(data, "J01", "있음", "보통", "  문구 ", "", measure)
    fs.record(data, "J01", "있음", "보통", "문구", "", measure)
    assert data["answers"]["J01"]["날조 문구"] == "문구" and data["changes"] == []
    with pytest.raises(SystemExit, match="="):
        fs.record(data, "J02", "있음", "보통", "=SUM(1)", "", measure)
    assert list(data["answers"]) == ["J01"]


def test_no_sheet_until_all_recorded(measure, tmp_path):
    data = fs.load_answers(Path("없는 파일.json"))
    fs.record(data, "J01", "없음", "높음", "", "", measure)
    with pytest.raises(SystemExit, match="62건"):
        fs.make_sheet(data, measure, tmp_path / "s.xlsx")
    assert not (tmp_path / "s.xlsx").exists()


def test_sheet_reads_back_as_recorded(measure, tmp_path):
    """설계 의도 3 — 측정 도구의 읽기 함수로 다시 읽으면 기록과 같다."""
    data = fill_all(measure)
    target = tmp_path / "s.xlsx"
    assert "63건" in fs.make_sheet(data, measure, target)
    read = read_rater_sheet(target, list(measure), {j: r["요약"] for j, r in measure.items()})
    assert {j: r["verdict"] for j, r in read.items()} == {j: a["판정"] for j, a in data["answers"].items()}
    assert "이미 있고 기록과 같습니다" in fs.make_sheet(data, measure, target)


def test_existing_different_sheet_is_not_overwritten(measure, tmp_path):
    """설계 의도 4."""
    data = fill_all(measure)
    target = tmp_path / "s.xlsx"
    fs.make_sheet(data, measure, target)
    before = target.read_bytes()
    fs.record(data, "J01", "없음" if data["answers"]["J01"]["판정"] == YES else "있음", "높음", "", "", measure)
    with pytest.raises(SystemExit, match="덮어쓰지 않습니다"):
        fs.make_sheet(data, measure, target)
    assert target.read_bytes() == before


def test_committed_answers_are_valid(measure):
    """기록 파일이 커밋되어 있으면 번호·판정·확신도가 측정 세트·선택지 안이다."""
    if not fs.ANSWERS.exists():
        pytest.skip("검토자 판정 기록 전")
    data = json.loads(fs.ANSWERS.read_text(encoding="utf-8"))
    assert set(data["answers"]) <= set(measure)
    assert all(a["판정"] in (YES, NO) and a["확신도"] in ("높음", "보통", "낮음") for a in data["answers"].values())
