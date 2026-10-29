"""v1.5 LLM-as-judge — 대화로 보여 준 문항과 측정 세트 원문 대조 도구 테스트 (tools/judge_v15/check_shown.py, 계획 §13)."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "judge_v15"))
import check_shown as cs  # noqa: E402


@pytest.fixture(scope="module")
def measure():
    return cs.fs.load_measure()


def question(row: dict, jid: str, tag: str = "", **swap) -> str:
    """대화에서 쓴 질문 모양. swap={칸: (원래 글자, 바꿀 글자)}로 옮겨 적기 오류를 흉내 낸다."""
    f = {k: str(row[k] or "") for k in row}
    for key, (a, b) in swap.items():
        f[key] = f[key].replace(a, b, 1)
    notice = " / ".join(f["참고: 업데이트 공지"].splitlines())
    issues = " / ".join(x.lstrip("- ") for x in f["참고: 기존 등록 이슈"].splitlines())
    return (f"[{jid}{tag}] 요약 한 줄이 리포트에 없는 말을 지어냈나요?\n\n■ 요약(판정 대상): {f['요약']}\n\n■ 리포트 원문(대조용)\n"
            f"입력 종류: {f['입력 종류']} / 게시판: {f['게시판 / 작성자']} / 게시 일시: {f['게시 일시'] or '(없음)'}\n"
            f"제목: {f['제목']}\n본문: {f['본문']}\n업데이트 공지: {notice}\n기존 등록 이슈(참고 자료): {issues}")


def event(*questions: str) -> str:
    return json.dumps({"message": {"content": [{"type": "tool_use", "name": "AskUserQuestion",
                                                "input": {"questions": [{"question": q} for q in questions]}}]}},
                      ensure_ascii=False)


def test_exact_copy_has_no_diff(measure):
    for jid in ("J01", "J03", "J55"):     # 여러 줄 공지(J03)·빈 칸 본문(J55) 포함
        assert cs.compare(question(measure[jid], jid), measure[jid]) == {}


def test_changed_letter_is_found(measure):
    row = measure["J09"]
    d = cs.compare(question(row, "J09", 요약=("뾰족", "뽰족"), 본문=("뾰족", "뽰족")), row)
    assert set(d) == {"요약", "본문"} and "뽰" in d["요약"][0]["보여 준 글자"]


def test_last_scored_and_recheck_are_separated(measure):
    """설계 의도 2 — 다시 판정한 질문은 채점 질문, 재확인 질문은 따로."""
    row = measure["J01"]
    lines = [event(question(row, "J01", 제목=("카일", "카이"))),
             event(question(row, "J01", " 다시 판정")),
             event(question(row, "J01", " 재확인", 제목=("카일", "카이")))]
    result = cs.check(lines, {"J01": row})
    assert result["items_with_diff"] == {} and set(result["recheck_diff"]["J01"]) == {"제목"}


def test_question_without_source_is_named():
    with pytest.raises(ValueError, match="J01"):
        cs.fields("[J01] 요약 판정은?\n■ 요약: 무엇")


def test_missing_item_is_reported(measure):
    result = cs.check(["not json", event(question(measure["J02"], "J02"))], {j: measure[j] for j in ("J01", "J02")})
    assert result["missing"] == ["J01"] and result["shown"] == 1


def test_committed_check_matches_record():
    """커밋된 대조 결과: 63건을 모두 보여 줬고, 차이 있는 문항은 계획 §13·ISSUE-013 목록과 같다."""
    if not cs.RESULT.exists():
        pytest.skip("대조 결과 커밋 전")
    r = json.loads(cs.RESULT.read_text(encoding="utf-8"))
    assert r["shown"] == 63 and r["missing"] == []
    assert sorted(r["items_with_diff"]) == ["J09", "J10", "J23", "J29", "J34", "J45", "J46", "J58", "J62"]
    assert all(d == {} for d in r["recheck_diff"].values())
    recheck = json.loads((cs.fs.bs.OUT_DIR / "recheck_v15.json").read_text(encoding="utf-8"))
    assert sorted(recheck["asked"]) == sorted(r["recheck_diff"])
