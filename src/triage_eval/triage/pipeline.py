"""리포트 1건 처리 — 첫 응답 → 칸 재요청 → Critical 체크리스트 → 폐기 확인 → 후처리 안전장치.

    처리 결과 = triage(client, item)

평가(평가셋 실행)가 이 함수를 쓰고, 서비스(링크로 들어온 리포트, docs/service_design.md)도 이 함수를 쓰도록 설계했다.
평가에서 잰 성능이 곧 서비스의 성능이 되도록 두 경로가 같은 코드를 지나게 하기 위해서다.

[설계 의도]
1. 칸 재요청: 응답이 허용 값(R6)이나 출력 언어(R7)를 어기면 틀린 칸만 1회 다시 묻는다.
   - 응답 전체를 다시 생성하면 형식에 문제가 없던 칸의 판단까지 바뀐다(프롬프트 개정 실험에서 응답 64개 중 29~49개가 바뀌었다).
   - 분류·우선순위·재현 정보 중 하나가 틀렸으면 [처리]도 함께 다시 정하게 한다. 처리는 그 판단에서 정해지므로,
     분류만 고치면 "결함인데 폐기" 같은 앞뒤가 맞지 않는 응답이 남는다.
   - 재요청 응답은 JSON 스키마로 제한한다. 선택지 칸은 허용 값 enum, 자유 서술 칸은 허용 문자 패턴(한 줄)이다.
     텍스트로만 다시 물으면 모델이 '무관 중', '과熱' 같은 틀린 값을 그대로 되풀이했다.
   - 받은 값은 다시 검증해, 허용 값일 때만 첫 응답의 그 줄에 넣는다. 코드가 비슷한 값으로 고쳐 주지 않는다.
   - 실행 환경이 pattern 을 지원하지 않아 호출이 실패하면 pattern 없이 한 번 더 부르고 그 사실을 기록한다.
   - 첫 응답은 제한하지 않는다. 8칸 전체를 JSON 으로 받으면 출력 형식 자체가 바뀐다.
2. Critical 체크리스트(checklist.py)는 재요청 뒤, 폐기 확인 앞에 묻는다. 형식이 고쳐진 판단을 보고 묻기 위해서다.
3. 폐기 확인: 최종 [처리]가 폐기이고 [분류]가 중복 의심이 아니면, 폐기 직전에 "이상 현상 제보 / 무관"을 1회 묻는다.
   폐기는 되돌릴 수 없고 아무도 다시 보지 않는 처리라 이 처리에만 확인 단계를 둔다(기준서 H-9). "이상 현상 제보"면
   [처리]를 정보 요청 후 보류로 바꾸고 [분류]는 그대로 둔다. 중복 의심은 기존 이슈에 덧붙이고 폐기하는 것이 규칙이라 제외한다.
4. 모든 호출(첫 응답·재요청·확인 질문)을 attempts 에 남기고, 지연은 호출 시간의 합으로 기록한다.
   재요청·확인에 드는 시간도 실제 운영에서 드는 비용이다.
5. 후처리 안전장치(guardrail.py)는 마지막에 적용하고, 모델 원본은 raw_response_text 로 남긴다.
"""

import json

from triage_eval import llm
from triage_eval.config import MODEL, OPTIONS
from triage_eval.contracts import (ENUMS, FORBIDDEN_SCRIPTS, LINE_PATTERNS, MODULES, NO_MODULE, OUTPUT_LANG,
                                   check_format, enum_errors, language_errors, parse_response, replace_field)
from triage_eval.triage import checklist
from triage_eval.triage.guardrail import GUARDRAIL_VERSION, apply_record
from triage_eval.triage.prompt import (DISCARD_CHECK_CHOICES, PROMPT_VERSION, SYSTEM_PROMPT, build_discard_check_prompt,
                                       build_prompt, build_retry_prompt)

RECHECK_TRIGGER = ("분류", "우선순위", "재현 정보")   # 이 칸이 틀리면 [처리]도 다시 정한다
RETRY_POLICY = {"trigger": ["R6_enum_valid", "R7_output_language"], "max_retries": 1, "scope": "field",
                "output": "json_schema_enum+line_pattern", "num_predict": 256}
DISCARD_CHECK_POLICY = {"trigger": "처리=폐기, 분류≠중복 의심", "choices": DISCARD_CHECK_CHOICES,
                        "on_report": "정보 요청 후 보류", "num_predict": 32}
DISCARD_CHECK_SCHEMA = {"type": "object", "properties": {"판정": {"type": "string", "enum": DISCARD_CHECK_CHOICES}},
                        "required": ["판정"]}


def retry_schema(fields: list[str], use_pattern: bool = True) -> dict:
    """재요청할 칸만 담는 JSON 스키마 (설계 의도 1)."""
    def prop(f):
        if f in ENUMS or f == "모듈":
            return {"type": "string", "enum": ENUMS.get(f) or (MODULES + [NO_MODULE])}
        return {"type": "string", "pattern": LINE_PATTERNS[OUTPUT_LANG]} if use_pattern else {"type": "string"}
    return {"type": "object", "properties": {f: prop(f) for f in fields}, "required": list(fields)}


def value_ok(field: str, value: str, merged_text: str) -> bool:
    """재요청 값을 넣어도 되는가. 선택지 칸은 허용 값(R6), 자유 서술 칸은 한 줄 + 출력 언어(R7)."""
    if field in ENUMS or field == "모듈":
        return all(f != field for f, _ in enum_errors(parse_response(merged_text)))
    return "\n" not in value and not FORBIDDEN_SCRIPTS[OUTPUT_LANG].search(value)


def parse_json(text: str) -> dict:
    """JSON 답을 읽는다. 읽을 수 없으면 빈 dict — 첫 응답이 그대로 남는다."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}
    return {k: v.strip() for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def merge_retry(first_text: str, new_values: dict, fields: list[str]) -> tuple[str, dict]:
    """재요청에서 받은 값 중 다시 물은 칸만, 허용 값일 때만 첫 응답의 그 줄에 넣는다."""
    out, fixed = first_text, {}
    for field in fields:
        value = new_values.get(field)
        if not value:
            continue
        candidate = replace_field(out, field, value)
        if value_ok(field, value, candidate):
            out, fixed[field] = candidate, value
    return out, fixed


def first_answer(client, model: str, item: dict, options: dict) -> dict:
    """설계 의도 1 — 1회 생성하고, R6·R7 위반이면 틀린 칸만 1회 다시 요청한다."""
    first = llm.generate(client, model, build_prompt(item), options)
    if not first["success"]:
        return {**first, "attempts": [], "retried": False}
    fmt1 = check_format(first["response_text"])
    attempts = [{"kind": "first", "response_text": first["response_text"], "failed_rules": fmt1["failed_rules"],
                 "elapsed_sec": first["elapsed_sec"]}]
    if not any(r in fmt1["failed_rules"] for r in RETRY_POLICY["trigger"]):
        return {**first, "attempts": attempts, "retried": False}

    values = parse_response(first["response_text"])
    errors, lang_errs = enum_errors(values), language_errors(values)
    recheck = ("처리",) if any(f in RECHECK_TRIGGER for f, _ in errors) and all(f != "처리" for f, _ in errors) else ()
    fields = [f for f, _ in errors] + list(recheck) + [f for f, _ in lang_errs]
    if not fields:   # 위반이 칸 밖(칸이 아닌 줄 등)에 있으면 고칠 칸이 없다
        return {**first, "attempts": attempts, "retried": False}
    retry_opts = {**options, "num_predict": RETRY_POLICY["num_predict"]}
    retry_prompt = build_retry_prompt(item, first["response_text"], errors, recheck, lang_errs)
    second = llm.generate(client, model, retry_prompt, retry_opts, schema=retry_schema(fields))
    pattern_fallback = False
    if not second["success"] and lang_errs:
        pattern_error, failed_sec = second["error_message"], second["elapsed_sec"]
        second = llm.generate(client, model, retry_prompt, retry_opts, schema=retry_schema(fields, use_pattern=False))
        second = {**second, "elapsed_sec": round(second["elapsed_sec"] + failed_sec, 3), "pattern_error": pattern_error}
        pattern_fallback = True
    merged, fixed = merge_retry(first["response_text"], parse_json(second["response_text"]), fields) \
        if second["success"] else (first["response_text"], {})
    attempts.append({"kind": "retry", "response_text": second["response_text"], "elapsed_sec": second["elapsed_sec"],
                     "eval_count": second.get("eval_count", 0), "error_message": second["error_message"],
                     "errors": [{"field": f, "value": v} for f, v in errors], "recheck": list(recheck),
                     "lang_errors": [{"field": f, "chars": c} for f, c in lang_errs], "fixed": fixed,
                     "pattern_fallback": pattern_fallback, "pattern_error": second.get("pattern_error")})
    return {**first, "response_text": merged,
            "elapsed_sec": round(first["elapsed_sec"] + second["elapsed_sec"], 3),
            "attempts": attempts, "retried": True,
            # 채택 = 원래 틀렸던 칸이 고쳐졌는가. 함께 다시 받은 [처리]만 받아진 경우는 채택이 아니다
            "retry_adopted": any(f in fixed for f, _ in errors + lang_errs)}


def critical_check(client, model: str, item: dict, res: dict, options: dict) -> dict:
    """설계 의도 2 — Critical 체크리스트. 분류가 결함일 때만 묻고, 올리기만 한다."""
    pred = parse_response(res.get("response_text", ""))
    if not res.get("success") or not checklist.should_ask(pred):
        return {**res, "critical_checked": False}
    opts = {**options, "num_predict": checklist.POLICY["num_predict"]}
    ans = llm.generate(client, model, checklist.build_prompt(build_prompt(item), res["response_text"]), opts,
                       schema=checklist.SCHEMA)
    answers = parse_json(ans["response_text"]) if ans["success"] else {}
    upgraded, reason = checklist.decide_upgrade(pred, answers)
    text = checklist.apply_upgrade(res["response_text"]) if upgraded else res["response_text"]
    attempts = res.get("attempts", []) + [{"kind": "critical_check", "response_text": ans["response_text"],
                                          "answers": answers, "upgraded": upgraded, "reason": reason,
                                          "elapsed_sec": ans["elapsed_sec"], "error_message": ans["error_message"]}]
    return {**res, "response_text": text, "elapsed_sec": round(res["elapsed_sec"] + ans["elapsed_sec"], 3),
            "attempts": attempts, "critical_checked": True, "critical_upgraded": upgraded}


def discard_check(client, model: str, item: dict, res: dict, options: dict) -> dict:
    """설계 의도 3 — 폐기 직전 확인. 대상이 아니면 res 를 그대로 돌려준다."""
    pred = parse_response(res.get("response_text", ""))
    if not res.get("success") or pred.get("처리") != "폐기" or pred.get("분류") == "중복 의심":
        return {**res, "discard_checked": False}
    opts = {**options, "num_predict": DISCARD_CHECK_POLICY["num_predict"]}
    ans = llm.generate(client, model, build_discard_check_prompt(item), opts, schema=DISCARD_CHECK_SCHEMA)
    verdict = parse_json(ans["response_text"]).get("판정") if ans["success"] else None
    held = verdict == "이상 현상 제보"
    text = replace_field(res["response_text"], "처리", DISCARD_CHECK_POLICY["on_report"]) if held else res["response_text"]
    attempts = res.get("attempts", []) + [{"kind": "discard_check", "response_text": ans["response_text"],
                                          "verdict": verdict, "held": held, "elapsed_sec": ans["elapsed_sec"],
                                          "error_message": ans["error_message"]}]
    return {**res, "response_text": text, "elapsed_sec": round(res["elapsed_sec"] + ans["elapsed_sec"], 3),
            "attempts": attempts, "discard_checked": True, "discard_held": held}


def triage(client, item: dict, options: dict | None = None, model: str = MODEL) -> dict:
    """리포트 1건을 처리한다. item 은 {"track": "A"|"B", "input": {...}} 꼴(evaluation 평가셋 문항과 같다).
    돌려주는 response_text 가 BTS에 들어갈 최종 8칸이다."""
    options = OPTIONS if options is None else options
    res = first_answer(client, model, item, options)
    res = critical_check(client, model, item, res, options)
    res = discard_check(client, model, item, res, options)
    return apply_record(res, item)


def run_config() -> dict:
    """처리 구성의 지문 — 실행 기록에 남기고 회귀 게이트가 기준선과 비교한다."""
    return {
        "prompt_version": PROMPT_VERSION,
        "system_prompt_sha256": llm.sha256_text(SYSTEM_PROMPT),
        "method": "m2",
        "method_assets_sha256": checklist.assets_sha256(),
        "guardrail_version": GUARDRAIL_VERSION,
        "retry_policy": RETRY_POLICY,
        "discard_check_policy": DISCARD_CHECK_POLICY,
        "critical_check_policy": checklist.POLICY,
    }
