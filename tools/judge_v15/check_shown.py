"""v1.5 LLM-as-judge — 대화로 보여 준 문항 텍스트를 측정 세트 원문과 글자 단위로 대조한다 (계획 §13, ISSUE-013).

검토자 판정을 대화로 받을 때(계획 §12) Claude가 측정 세트 원문을 질문 창에 옮겨 적었고, 그 과정에서 글자가 바뀐
문항이 있었다. 이 도구는 대화 기록(Claude Code 세션 기록 .jsonl)에서 질문 원문을 꺼내 측정 세트와 대조한다.

사용법
------
    uv run python tools/judge_v15/check_shown.py --transcript <세션 기록 .jsonl>     # 결과 출력 + 결과 파일 저장

출력
----
    data/judge_v15/shown_check_v15.json   대조 결과 (문항별 칸·글자 차이, 대화 기록의 sha256)
    대화 기록 자체는 저장소에 넣지 않는다(개인 대화). sha256으로 어떤 기록을 대조했는지만 남긴다.

[설계 의도]
1. 측정 세트 파일만 읽는다. 정답 대응표는 읽지 않는다(fill_sheet.py 설계 의도 1과 같은 이유).
2. 문항마다 검토자가 답한 질문(재확인 질문 제외)을 대조 대상으로 삼는다. 같은 번호를 다시 물은 경우에는 마지막 질문이
   답을 받은 질문이다. 재확인 질문(`[Jnn 재확인]`)은 따로 대조한다.
3. 질문 창에서는 칸 이름과 줄바꿈을 줄여 보여 주었으므로, 공백·칸 이름·구분 기호('/', '-', '·')를 양쪽에서 똑같이 지운 뒤
   비교한다. 글자 자체가 바뀐 경우만 차이로 남는다. 구분 기호를 지우므로 그 기호만 빠지거나 더해진 차이는 잡지 못한다.
"""

import argparse
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fill_sheet as fs  # noqa: E402

from triage_eval.pipeline.score import rel  # noqa: E402

RESULT = fs.bs.OUT_DIR / "shown_check_v15.json"
LABELS = ("테스트 환경", "재현율", "이슈 설명", "기대 결과", "실제 결과", "참고 문서")
QUESTION = re.compile(r"\[(J\d\d)( 재확인| 다시 판정)?\]")


def extract(lines) -> dict[str, dict[str, str]]:
    """설계 의도 2 — {번호: {"scored": 마지막 채점 질문, "recheck": 마지막 재확인 질문}}."""
    out: dict[str, dict[str, str]] = {}
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (ev.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for c in content:
            if not (isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion"):
                continue
            for q in c.get("input", {}).get("questions", []):
                m = QUESTION.match(q.get("question", ""))
                if m and "■ 요약" in q["question"]:
                    kind = "recheck" if m.group(2) == " 재확인" else "scored"
                    out.setdefault(m.group(1), {})[kind] = q["question"]
    return out


def norm(s: str) -> str:
    """설계 의도 3."""
    for label in LABELS:
        s = s.replace(f"[{label}]", "")
    s = s.replace("(빈칸)", "").replace("(없음)", "")
    return re.sub(r"[\s/\-·]", "", s)


def fields(question: str) -> dict[str, str]:
    """질문 텍스트를 칸별로 나눈다. 원문 칸이 없는 질문이면 ValueError(어느 질문인지 보이게)."""
    s = re.search(r"■ 요약[^:]*: (.*)", question)
    m = re.search(r"\n입력 종류: (.*?)\n제목: (.*?)\n(.*?)\n업데이트 공지: (.*?)\n기존 등록 이슈[^:]*: (.*)$", question, re.S)
    if not (s and m):
        raise ValueError(f"원문 칸을 찾을 수 없는 질문: {question[:40]!r}")
    summary = s.group(1)
    head = re.sub(r"(게시판|작성자|게시 일시): ", "", m.group(1))
    return {"요약": summary, "머리 정보": head, "제목": m.group(2), "본문": re.sub(r"^본문: ", "", m.group(3)),
            "공지": m.group(4), "기존 이슈": m.group(5)}


def expected(row: dict) -> dict[str, str]:
    return {"요약": row["요약"], "머리 정보": row["입력 종류"] + row["게시판 / 작성자"] + row["게시 일시"],
            "제목": row["제목"], "본문": row["본문"], "공지": row["참고: 업데이트 공지"], "기존 이슈": row["참고: 기존 등록 이슈"]}


def diffs(want: str, got: str) -> list[dict]:
    a, b = norm(want), norm(got)
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    return [{"원문": a[max(i1 - 4, 0):i2 + 4], "보여 준 글자": b[max(j1 - 4, 0):j2 + 4]}
            for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal"]


def compare(question: str, row: dict) -> dict[str, list[dict]]:
    got, want = fields(question), expected(row)
    return {k: d for k in want if (d := diffs(want[k], got[k]))}


def check(lines, measure: dict[str, dict]) -> dict:
    shown = extract(lines)
    items, recheck, missing = {}, {}, []
    for jid, row in measure.items():
        q = shown.get(jid, {})
        if "scored" not in q:
            missing.append(jid)
            continue
        if d := compare(q["scored"], row):
            items[jid] = d
        if "recheck" in q:
            recheck[jid] = compare(q["recheck"], row)
    return {"shown": len(measure) - len(missing), "missing": missing, "items_with_diff": items, "recheck_diff": recheck}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="대화로 보여 준 문항과 측정 세트 원문 대조 (계획 §13)")
    p.add_argument("--transcript", type=Path, required=True)
    args = p.parse_args(argv)
    raw = args.transcript.read_bytes()
    result = {"version": "v15", "measure_set": fs.bs.FILES[0], "transcript_sha256": hashlib.sha256(raw).hexdigest(),
              **check(raw.decode("utf-8").splitlines(), fs.load_measure())}
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"보여 준 문항 {result['shown']}/{result['shown'] + len(result['missing'])}, 글자 차이 {len(result['items_with_diff'])}건: "
          + ", ".join(f"{j}({'·'.join(d)})" for j, d in result["items_with_diff"].items()))
    print("재확인 질문 차이: " + (", ".join(f"{j}({'·'.join(d) or '없음'})" for j, d in result["recheck_diff"].items()) or "없음"))
    print(f"저장: {rel(RESULT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
