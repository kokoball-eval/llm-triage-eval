"""v1.5 LLM-as-judge — 검토자 판정을 대화로 받아 기록하고, 기록으로 검토자 채점 시트를 만든다.

계획: report/judge_v15_plan.md §12 (추가 기록, 입력 방식 변경)

사용법
------
    uv run python tools/judge_v15/fill_sheet.py show J01 J02                     # 측정 세트 원문·요약 그대로 출력
    uv run python tools/judge_v15/fill_sheet.py record J01 없음 높음 [--phrase 문구] [--memo 메모]
    uv run python tools/judge_v15/fill_sheet.py status                           # 기록한 건수·다음 번호
    uv run python tools/judge_v15/fill_sheet.py sheet                            # 63건 모두 기록했을 때 시트 생성

출력
----
    data/judge_v15/rater_answers_v15.json             검토자 판정 기록 (받은 순서·고친 이력 포함)
    data/judge_v15/요약_날조_채점시트_검토자.xlsx      검토자 채점 시트 (측정 도구 --sheet 입력)

[설계 의도]
1. 데이터 파일은 측정 세트(measure_set_v15.json)와 판정 기록만 읽는다. 정답 대응표·개발용 자료 파일은 읽지 않고, 출력은
   측정 세트의 원문 칸·요약과 건수뿐이다. 문항을 보여 주는 쪽이 주입 여부를 알고 있으므로(계획 §12), 보여 주는 내용이
   측정 세트 원문 그대로임을 코드로 보장한다. 시트 모양·경로 상수를 쓰려고 build_set 을 불러오며, 그 모듈의 주입 목록은
   이 도구에서 쓰거나 출력하지 않는다.
2. 판정은 받은 즉시 받은 순서대로 파일에 쓴다. 채점이 중간에 끊겨도 이어서 할 수 있고, 같은 번호를 다른 값으로 다시
   기록하면 이전 값을 changes에 남긴다(같은 값이면 남기지 않는다). 판정을 고친 이력이 결과 해석에 필요할 수 있기 때문이다.
   문구·메모는 앞뒤 공백을 지우고, '='로 시작하면 거부한다. 엑셀이 수식으로 저장해 측정 도구가 다시 읽지 못하기 때문이다.
3. 시트는 기록 파일에서 기계적으로 만든다. 사람이 엑셀에 옮겨 적는 단계가 없어 옮겨 적기 오류가 생기지 않는다.
   시트 모양은 build_set.write_sheet 와 같다. 63건이 모두 기록되지 않았으면 만들지 않는다.
4. 시트가 이미 있으면 기록 파일과 내용이 같을 때만 통과하고, 다르면 쓰지 않는다. 커밋한 시트가 측정 입력이므로
   측정 뒤에 덮어쓰면 측정과 시트가 어긋난다.
"""

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_set as bs  # noqa: E402

from triage_eval.pipeline.judge import NO, YES, read_rater_sheet  # noqa: E402
from triage_eval.pipeline.score import rel  # noqa: E402

MEASURE = bs.OUT_DIR / bs.FILES[0]
ANSWERS = bs.OUT_DIR / "rater_answers_v15.json"
SHEET = bs.OUT_DIR / "요약_날조_채점시트_검토자.xlsx"
SHOWN = ["입력 종류", "게시판 / 작성자", "게시 일시", "제목", "본문", "참고: 업데이트 공지", "참고: 기존 등록 이슈", "요약"]
VERDICT_ALIAS = {"있음": YES, YES: YES, "없음": NO, NO: NO}


def load_measure(path: Path = MEASURE) -> dict[str, dict]:
    return {r["번호"]: r for r in json.loads(path.read_text(encoding="utf-8"))["items"]}


def load_answers(path: Path = ANSWERS) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"version": "v15", "measure_set": bs.FILES[0], "method": "대화로 판정을 받아 기록 (계획 §12)",
            "answers": {}, "changes": []}


def show(jids: list[str], measure: dict[str, dict]) -> str:
    """설계 의도 1 — 측정 세트의 칸을 그대로 이어 붙인다. 빈 칸은 '(없음)'."""
    out = []
    for jid in jids:
        if jid not in measure:
            raise SystemExit(f"측정 세트에 없는 번호: {jid}")
        row = measure[jid]
        out.append(f"■ {jid}")
        out += [f"[{name}]\n{str(row.get(name) or '').strip() or '(없음)'}" for name in SHOWN]
        out.append("")
    return "\n".join(out)


def record(data: dict, jid: str, verdict: str, conf: str, phrase: str, memo: str, measure: dict[str, dict]) -> dict:
    """설계 의도 2."""
    if jid not in measure:
        raise SystemExit(f"측정 세트에 없는 번호: {jid}")
    if verdict not in VERDICT_ALIAS:
        raise SystemExit(f"판정은 있음·없음 중 하나: {verdict!r}")
    if conf not in bs.CONFIDENCE:
        raise SystemExit(f"확신도는 {'·'.join(bs.CONFIDENCE)} 중 하나: {conf!r}")
    phrase, memo = phrase.strip(), memo.strip()
    if phrase.startswith("=") or memo.startswith("="):
        raise SystemExit("문구·메모는 '='로 시작할 수 없습니다(엑셀 수식으로 저장됨)")
    new = {"판정": VERDICT_ALIAS[verdict], "확신도": conf, "날조 문구": phrase, "메모": memo}
    old = data["answers"].get(jid)
    if old and {k: old[k] for k in new} == new:
        return data
    now = datetime.now().isoformat(timespec="seconds")
    if old:
        data["changes"].append({"번호": jid, "이전": old, "recorded_at": now})
    data["answers"][jid] = {**new, "recorded_at": now}
    return data


def save_answers(data: dict, path: Path = ANSWERS) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def status(data: dict, measure: dict[str, dict]) -> str:
    left = [j for j in measure if j not in data["answers"]]
    return f"기록 {len(data['answers'])}/{len(measure)}건, 고친 이력 {len(data['changes'])}건" + (
        f", 다음 {left[0]}" if left else ", 모두 기록함")


def same(read: dict, want: dict) -> bool:
    """빈 칸은 엑셀에서 None 으로 읽히므로 빈 문자열과 같게 본다."""
    norm = lambda d: {j: {k: (v or "") for k, v in x.items()} for j, x in d.items()}   # noqa: E731
    return norm(read) == norm(want)


def make_sheet(data: dict, measure: dict[str, dict], target: Path = SHEET) -> str:
    """설계 의도 3·4. 결과 문구를 돌려준다."""
    left = [j for j in measure if j not in data["answers"]]
    if left:
        raise SystemExit(f"아직 기록하지 않은 번호 {len(left)}건: {', '.join(left[:5])}{' …' if len(left) > 5 else ''}")
    want = {j: {"verdict": a["판정"], "phrase": a["날조 문구"], "확신도": a["확신도"], "메모": a["메모"]}
            for j, a in data["answers"].items()}
    summaries = {j: r["요약"] for j, r in measure.items()}
    if target.exists():
        if same(read_rater_sheet(target, list(measure), summaries), want):
            return f"{target.name} 가 이미 있고 기록과 같습니다"
        raise SystemExit(f"{target.name} 가 이미 있고 기록과 다릅니다 — 덮어쓰지 않습니다(설계 의도 4)")
    rows = [{**measure[j], "판정": a["판정"], "날조 문구": a["날조 문구"], "확신도": a["확신도"], "메모": a["메모"]}
            for j, a in sorted(data["answers"].items())]
    with tempfile.TemporaryDirectory() as tmp:
        shutil.move(str(bs.write_sheet({"items": rows}, Path(tmp))), target)
    if not same(read_rater_sheet(target, list(measure), summaries), want):   # 측정 도구와 같은 함수로 다시 읽어 확인
        target.unlink()
        raise SystemExit("만든 시트를 다시 읽은 값이 기록과 다릅니다 — 시트를 지웠습니다")
    return f"저장: {rel(target)} ({len(rows)}건)"


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="검토자 판정 기록·채점 시트 생성 (계획 §12)")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("jids", nargs="+")
    r = sub.add_parser("record")
    r.add_argument("jid")
    r.add_argument("verdict")
    r.add_argument("conf")
    r.add_argument("--phrase", default="")
    r.add_argument("--memo", default="")
    sub.add_parser("status")
    sub.add_parser("sheet")
    args = p.parse_args(argv)
    measure = load_measure()
    data = load_answers()
    if args.cmd == "show":
        print(show(args.jids, measure))
    elif args.cmd == "record":
        save_answers(record(data, args.jid, args.verdict, args.conf, args.phrase, args.memo, measure))
        print(status(data, measure))
    elif args.cmd == "status":
        print(status(data, measure))
    else:
        print(make_sheet(data, measure))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
