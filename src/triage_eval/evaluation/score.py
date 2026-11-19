"""채점 — 처리 결과 1건을 정답 라벨과 대조하고, 여러 건을 성능 지표로 묶는다.

지표
    칸 정답      예측값이 허용 답 목록 안에 있음. 경계 사례는 허용 답이 여러 개다
    최선 답      예측값이 허용 답의 첫 값(가장 바람직한 답)과 같음
    6칸 정답     분류·우선순위·모듈·재현 정보·발생 빈도·처리가 모두 허용 답 안
    X-1 누락     Critical 문항(risk X-1)인데 [처리]가 긴급 사인 요청이 아님
    X-2 누락     정답 분류가 결함인 문항을 폐기함
    X-3 누락     사람 확인이 필요한 문항(risk X-3)을 사람 확인 없이 끝냄(개발 배정·CS 응대·폐기)
    과잉 상신    허용 답에 없는데 긴급 사인 요청으로 보냄(리드 QA 사인 큐를 불필요하게 채움)
    날조         [요약]·[누락 정보]에 입력에 없는 구체 사실(hallucination.py 규칙)
    요청·권장 반영  꼭 요청할 정보·권장 조치의 핵심어가 [누락 정보 및 권장 조치]에 있는 비율(참고 지표)

[설계 의도]
1. 정답은 허용 답 목록으로 채점하고 최선 답 일치를 따로 센다. 경계 사례는 실무에서도 답이 갈리므로
   하나만 정답으로 두면 맞는 답을 오답 처리한다.
2. 위험 건(X-1~X-3)은 정답률과 따로 건수로 센다. 평균 정답률이 높아도 Critical 한 건을 놓치면 실무에서는 사고다.
3. 과잉 상신을 함께 센다. X-1만 보면 모든 글을 긴급 사인 요청으로 보내는 모델이 만점을 받는다.
4. 형식이 깨져 칸을 읽지 못한 응답은 그 칸을 오답으로 센다. 운영에서도 읽지 못한 응답은 처리되지 않는다.
5. 날조의 근거 원문은 모델에 실제로 들어간 입력 전체(build_input)다. 공지·기존 이슈를 인용한 것은 날조가 아니다.
6. 요청·권장 반영률은 핵심어 포함 여부라 표현이 다르면 놓친다. 합격 기준에 넣지 않는 참고 지표다.
"""

import re
from statistics import mean

from triage_eval.contracts import NO_MODULE, check_format, parse_response, split_modules
from triage_eval.evaluation.hallucination import detect
from triage_eval.triage.prompt import build_input

JUDGED = ["분류", "우선순위", "모듈", "재현 정보", "발생 빈도", "처리"]
CLOSING = {"개발 배정", "CS 응대", "폐기"}          # 사람 확인 없이 끝나는 처리 (X-3)
STOPWORDS = {"확인", "정보", "필요", "여부", "추가", "요청", "기존", "이슈", "관련", "내용"}


def field_correct(field: str, predicted: str | None, allowed: list[str]) -> tuple[bool, bool]:
    """(허용 답 안인가, 최선 답과 같은가)."""
    if field == "모듈":
        mods = set(split_modules(predicted or ""))
        if not allowed:  # 무관한 글 등 모듈이 없는 문항
            ok = mods <= {NO_MODULE}
            return ok, ok
        ok = bool(mods) and mods <= set(allowed)
        return ok, ok and allowed[0] in mods
    return predicted in allowed, bool(allowed) and predicted == allowed[0]


def keywords(phrase: str) -> list[str]:
    words = re.findall(r"[가-힣A-Za-z0-9]{2,}", phrase)
    return [w for w in words if w not in STOPWORDS] or words


def phrase_covered(phrase: str, text: str) -> bool:
    return any(k in text for k in keywords(phrase))


def score_one(record: dict, item: dict) -> dict:
    """처리 결과 1건(실행 기록의 응답 한 줄)을 문항의 정답 라벨로 채점한다."""
    text = record.get("response_text", "") if record.get("success") else ""
    fmt = check_format(text)
    pred = parse_response(text)
    lb = item["labels"]
    fields = {}
    for f in JUDGED:
        ok, best = field_correct(f, pred.get(f), lb[f])
        fields[f] = {"predicted": pred.get(f), "allowed": lb[f], "ok": ok, "best": best}
    action = pred.get("처리")
    risks = {
        "X-1": "X-1" in lb["risk"] and action != "긴급 사인 요청",
        "X-2": lb["분류"][0] == "결함" and "폐기" not in lb["처리"] and action == "폐기",
        "X-3": "X-3" in lb["risk"] and action in CLOSING,
    }
    note = pred.get("누락 정보 및 권장 조치", "") or ""
    required = lb.get("must_request", []) + lb.get("must_recommend", [])
    halluc = detect(text, build_input(item)) if text else {"hallucinated": False, "ungrounded": []}
    return {
        "eval_id": record["eval_id"], "item_id": item["id"], "run_index": record["run_index"], "seed": record.get("seed"),
        "success": record.get("success", False), "format": fmt, "fields": fields,
        "all_fields_ok": all(v["ok"] for v in fields.values()),
        "risk_miss": [k for k, v in risks.items() if v],
        "over_escalation": action == "긴급 사인 요청" and "긴급 사인 요청" not in lb["처리"],
        "required_total": len(required), "required_covered": sum(phrase_covered(p, note) for p in required),
        "hallucinated": halluc["hallucinated"], "ungrounded": halluc["ungrounded"],
        "elapsed_sec": record.get("elapsed_sec"),
    }


def pct(a: int, b: int):
    return round(a / b * 100, 1) if b else None


def summarize(rows: list[dict], items: dict) -> dict:
    """채점 행들을 성능 지표로 묶는다. 위험 건은 '놓친 건수 / 그 위험이 걸린 응답 수'로 센다."""
    n = len(rows)
    lat = [r["elapsed_sec"] for r in rows if r["elapsed_sec"] is not None]
    label = lambda r: items[r["item_id"]]["labels"]   # noqa: E731
    return {
        "n": n,
        "success": sum(r["success"] for r in rows),
        "strict": sum(r["format"]["strict_pass"] for r in rows),
        "field_ok": {f: sum(r["fields"][f]["ok"] for r in rows) for f in JUDGED},
        "field_best": {f: sum(r["fields"][f]["best"] for r in rows) for f in JUDGED},
        "all_fields_ok": sum(r["all_fields_ok"] for r in rows),
        "risk_miss": {k: sum(k in r["risk_miss"] for r in rows) for k in ("X-1", "X-2", "X-3")},
        "risk_total": {
            "X-1": sum("X-1" in label(r)["risk"] for r in rows),
            "X-2": sum(label(r)["분류"][0] == "결함" and "폐기" not in label(r)["처리"] for r in rows),
            "X-3": sum("X-3" in label(r)["risk"] for r in rows),
        },
        "over_escalation": sum(r["over_escalation"] for r in rows),
        "hallucinated": sum(r["hallucinated"] for r in rows),
        "required_coverage_pct": pct(sum(r["required_covered"] for r in rows), sum(r["required_total"] for r in rows)),
        "latency_sec_mean": round(mean(lat), 3) if lat else None,
    }
