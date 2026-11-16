"""v1.5 요약 품질 LLM-as-judge — 요약 품질 판정의 채점관(gemma4:12b)과 검토자 채점의 일치를 잰다.

계획(사전 등록): report/quality_v15_plan.md · 기준: quality_v15.toml · 세트: tools/quality_v15/build_set.py
채점 화면: tools/quality_v15/rater_server.py

사용법
------
    uv run python -m triage_eval.pipeline.quality --dev       # 개발용 자료로 채점관 실행 (프롬프트 개발, 계획 §5)
    uv run python -m triage_eval.pipeline.quality --measure   # 측정 1회 (검토자 판정은 rater_answers_v15.json)

종료 코드
---------
    --dev       0  실행 완료   2  측정 환경 오염(실행 도중 모델 재로드)
    --measure   0  채택   1  미채택   2  판정 불가 (판정 빈칸·번호 불일치, 측정 환경 오염, 기준 파일 오류, 측정 가드)

출력
----
    data/quality_v15/dev_result_v15.json     개발용 실행 결과 (마지막 실행만 남는다)
    data/quality_v15/quality_result_v15.json 측정 결과 (검토자 판정·채점관 판정·지표·판정)
    report/quality_v15.md                    사람이 읽는 판정표

[설계 의도]
1. 기준 파일은 모르는 섹션·키나 빠진 키가 있으면 읽지 않는다(오타가 기준을 조용히 바꾸지 못하게). judge.py 와 같은 규칙.
2. 채점관에게는 검토자 화면과 같은 칸(입력 종류·게시판/작성자·게시 일시·제목·본문·공지·기존 이슈·요약)과 계획 §2의 정의
   문구(유형 표·날조 제외·우선순위)를 글자까지 같게 준다. 사람과 채점관이 다른 정보로 판정하면 일치율이 정의가 아니라
   정보 차이를 잰다. 일치는 테스트로 확인한다.
3. 출력은 JSON 스키마(판정 enum + 유형 목록 + 문제 문구)로 제한하고, 읽지 못하면 1번 다시 요청한다. 또 실패하면 불일치·놓침·
   거짓 경보로 센다(가장 불리하게, 기준 파일 규칙 5). 통신 오류는 "읽지 못함"과 구분해 측정 무효로 본다.
4. temperature 0, seed 1, think=False. 실행 전에 채점관을 메모리에서 내리고 워밍업 1회 뒤, 응답의 로드 시간이 RELOAD_SEC 를
   넘으면 실행 도중 재로드로 보고 판정하지 않는다(OBS-005).
5. 주입 결함은 유형이 달라도 채점관이 "문제 있음"이면 검출로 센다(기준 파일 규칙 4). 유형별 일치는 기록만 한다.
6. 프롬프트는 개발용 자료(결함 주입 6건 포함 63건)로만 다듬는다. 측정 세트와 검토자 판정은 프롬프트 확정 전에 열지 않았다.
7. 측정은 한 번만 한다. 결과 파일이 이미 있거나, 프롬프트·기준·측정 세트·검토자 판정 파일에 커밋하지 않은 변경이 있거나,
   프롬프트 지문·모델 digest가 개발용 실행과 다르면 실행하지 않는다. 측정 결과는 화면에 출력하기 전에 파일로 저장하고
   표준 출력은 UTF-8로 맞춘다(ISSUE-014).
8. 측정을 시작하면 채점관을 실행하기 전에 시작 표시 파일(measure_started_v15.json)을 만들고, 판정 46건이 끝나면 지표 계산 전에
   그 파일에 판정 원자료를 먼저 저장한다. 가드는 이 파일이 있으면 실행하지 않는다. 지표 계산·통신·Ctrl+C로 중간에 끊겨도 결과 파일이
   없다는 이유로 두 번째 측정이 되지 않게 하기 위해서다. 재로드·통신 오류로 무효가 된 실행은 무효 기록을 남기고 표시를 지운다(재측정 허용).
9. 프롬프트 지문은 문구뿐 아니라 실제로 채점관에 가는 입력 전체(고정 가짜 문항으로 만든 build_prompt 결과)·OPTIONS·THINK·스키마를 덮는다.
10. 검토자 판정 파일은 채점 화면이 쓴 것을 고치지 않고 그대로 읽는다. 빈칸·선택지 밖 값·번호 불일치면 채점관을 실행하기 전에 멈춘다.
"""

import argparse
import json
import subprocess
import sys
import time
import tomllib
from collections import Counter
from pathlib import Path

from triage_eval.common.ollama_runtime import get_model_digests, sha256_text
from triage_eval.common.paths import ROOT
from triage_eval.pipeline.agreement import cohen_kappa
from triage_eval.pipeline.gate import GateError
from triage_eval.pipeline.score import rel

CRITERIA = ROOT / "quality_v15.toml"
KNOWN = {
    "meta": {"version", "judge_model", "rater", "measure_set", "answer_key", "dev_set", "practice"},
    "natural": {"agreement_min", "kappa_min", "min_positive_for_kappa", "false_alarm_max"},
    "injected": {"recall_min"},
}


def load_criteria(path: Path = CRITERIA) -> dict:
    """설계 의도 1."""
    if not path.exists():
        raise GateError(f"기준 파일이 없습니다: {path}")
    c = tomllib.loads(path.read_text(encoding="utf-8"))
    problems = [f"모르는 섹션 [{s}]" for s in c if s not in KNOWN]
    problems += [f"[{s}] 모르는 키 {k!r}" for s, body in c.items() if s in KNOWN for k in body if k not in KNOWN[s]]
    problems += [f"[{s}] {k} 가 없습니다" for s, keys in KNOWN.items() for k in keys if k not in c.get(s, {})]
    if problems:
        raise GateError("기준 파일 오류 → " + "; ".join(problems))
    return c


DEV_RESULT = ROOT / "data" / "quality_v15" / "dev_result_v15.json"
RESULT_JSON = ROOT / "data" / "quality_v15" / "quality_result_v15.json"
INVALID_RUNS = ROOT / "data" / "quality_v15" / "invalid_runs_v15.json"
RATER_ANSWERS = ROOT / "data" / "quality_v15" / "rater_answers_v15.json"
REPORT = ROOT / "report" / "quality_v15.md"
STARTED = ROOT / "data" / "quality_v15" / "measure_started_v15.json"   # 설계 의도 8
CONFIDENCE = ("높음", "보통", "낮음")
GUARDED = ["src/triage_eval/pipeline/quality.py", "quality_v15.toml", "data/quality_v15/measure_set_v15.json",
           "data/quality_v15/answer_key_v15.json", "data/quality_v15/dev_result_v15.json",
           "data/quality_v15/rater_answers_v15.json"]   # 설계 의도 7·8

YES, NO = "품질 문제 있음", "품질 문제 없음"
TYPES = ["Q1", "Q2", "Q3"]
OPTIONS = {"temperature": 0, "seed": 1, "num_predict": 300, "num_ctx": 4096}   # 설계 의도 4
THINK = False
RELOAD_SEC = 1.0
SCHEMA = {"type": "object",
          "properties": {"판정": {"type": "string", "enum": [YES, NO]},
                         "유형": {"type": "array", "items": {"type": "string", "enum": TYPES}},
                         "문제 문구": {"type": "string"}},
          "required": ["판정", "유형", "문제 문구"]}   # 설계 의도 3
FIELDS = ["입력 종류", "게시판 / 작성자", "게시 일시", "제목", "본문", "참고: 업데이트 공지", "참고: 기존 등록 이슈"]
NONE_TEXT = "(없음)"

PROMPT_VERSION = "q3"   # 설계 의도 6
PROMPT = """너는 게임 버그 리포트 트리아지 결과를 검수하는 QA 검토자다. 아래 [리포트 원문]과 모델이 쓴 [요약]을 비교해, 요약에 품질 문제가 있는지 판정한다.

[질문]
이 요약 한 줄만 읽은 담당자가 이슈를 정확히 이해할 수 있는가? 문제가 있으면 "품질 문제 있음"과 유형을 고른다(여러 개 가능).

[유형]
- Q1 대상 누락: 무엇이·어디서가 빠져, 무엇에 대한 이슈인지 알 수 없다
- Q2 오해 소지: 원문과 다른 뜻으로 읽힌다. 이슈의 성격이 바뀐 경우(건의 → 오류, 의심 → 확정 등)도 포함
- Q3 문장 결함: 목적어·서술어가 빠지거나 문장이 어색해 뜻이 흐려진다

[주의]
- 원문에 없는 사실(날조)은 이번에 판정하지 않는다. 단, 이슈의 성격을 바꾸는 서술(건의 → 오류, 의심 → 확정 등)은 날조가 아니라 Q2로 본다.
- 짧은 요약도 위 세 가지 문제가 없으면 "품질 문제 없음"이다.
- 원문을 다른 말로 바꿔 쓴 것, 원문 내용을 줄인 것은 문제가 아니다.

[판정 방법]
요약만 읽고 다음 세 가지에 답한다. (1) 무엇에 대한 어떤 현상인지 말할 수 있는가? (2) 원문과 같은 뜻인가? (3) 문장이 자연스럽고 완결되어 있는가? 셋 모두 그렇다고 답할 수 있으면 "품질 문제 없음"이다. 원문에 있는 세부 사항이 요약에 없다는 것만으로는 문제가 아니다(요약은 줄인 글이다). 문제인지 아닌지 경계에 있어 확신이 서지 않으면 "품질 문제 없음"으로 답한다. 담당자가 이슈를 잘못 이해하거나 이해하지 못할 분명한 이유가 있을 때만 "품질 문제 있음"이다.

[출력]
JSON으로만 답한다. "판정"은 "품질 문제 있음" 또는 "품질 문제 없음". "유형"은 문제 있음이면 해당하는 Q1·Q2·Q3의 목록, 없음이면 빈 목록. "문제 문구"는 문제 있음이면 요약에서 문제가 되는 부분을 그대로 옮겨 적고, 없음이면 빈 문자열로 둔다.
"""


def source_of(row: dict) -> str:
    return "\n".join(f"{f}: {row.get(f) or NONE_TEXT}" for f in FIELDS)


def build_prompt(row: dict) -> str:
    """설계 의도 2."""
    return f"{PROMPT}\n[리포트 원문]\n{source_of(row)}\n\n[요약]\n{row['요약']}\n"


def parse_judgement(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("판정") not in (YES, NO):
        return None
    types = data.get("유형")
    if not isinstance(types, list) or any(t not in TYPES for t in types):
        return None
    return {"verdict": data["판정"], "types": sorted(set(types)), "phrase": str(data.get("문제 문구") or "").strip()}


def judge_one(client, model: str, row: dict) -> dict:
    """설계 의도 3 — 1회 + 읽지 못하면 1회 재요청."""
    loads, raws, errors = [], [], 0
    for attempt in (1, 2):
        start = time.perf_counter()
        try:
            r = client.generate(model=model, prompt=build_prompt(row), options=OPTIONS, think=THINK, format=SCHEMA)
            raw, load = r.get("response", ""), round(r.get("load_duration", 0) / 1e9, 3)
        except Exception as e:   # 통신 오류는 따로 센다
            raw, load, errors = f"ERROR: {e}", 0.0, errors + 1
        raws.append(raw)
        loads.append(load)
        parsed = parse_judgement(raw)
        if parsed:
            return {**parsed, "parsed": True, "attempts": attempt, "raw": raws, "load_duration_sec": loads,
                    "errors": errors, "elapsed_sec": round(time.perf_counter() - start, 3)}
    return {"verdict": None, "types": [], "phrase": "", "parsed": False, "attempts": 2, "raw": raws,
            "load_duration_sec": loads, "errors": errors}


def run_judge(client, model: str, rows: list[dict], echo: bool = True) -> tuple[list[dict], list[str]]:
    """설계 의도 4 — (판정 목록, 재로드 감지 목록). echo=False 이면 판정 내용을 화면에 내지 않는다(측정 모드)."""
    try:
        client.generate(model=model, prompt="", keep_alive=0)
    except Exception:
        pass
    client.generate(model=model, prompt="워밍업", options=OPTIONS, think=THINK)
    out, reloads = [], []
    for row in rows:
        j = judge_one(client, model, row)
        if any(x > RELOAD_SEC for x in j["load_duration_sec"]):
            reloads.append(f"{row['번호']} (로드 {max(j['load_duration_sec'])}초)")
        if j["errors"]:
            reloads.append(f"{row['번호']} (통신 오류 {j['errors']}회)")
        out.append({"번호": row["번호"], **j})
        if echo:
            print(f"  {row['번호']}: {j['verdict'] or '읽지 못함'} {','.join(j['types'])} {j['phrase']}")
    return out, reloads


def prompt_fingerprint() -> str:
    """프롬프트 문구·입력 칸·빈칸 표시·출력 스키마를 함께 덮는 지문."""
    probe = {"번호": "P", **{f: f"<{f}>" for f in FIELDS[:-1]}, "참고: 기존 등록 이슈": "", "요약": "<요약>"}   # 설계 의도 9
    return sha256_text(json.dumps([PROMPT, FIELDS, NONE_TEXT, SCHEMA, build_prompt(probe), OPTIONS, THINK],
                                  ensure_ascii=False, sort_keys=True))


def run_meta(client, model: str, digest_before: str | None) -> dict:
    return {"judge_model": model, "model_digest": digest_before,
            "model_digest_after": get_model_digests(client, [model]).get(model),
            "prompt_version": PROMPT_VERSION, "prompt_sha256": prompt_fingerprint(), "options": OPTIONS, "think": THINK,
            "executed_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


# ── 개발용 모드 (계획 §5) ────────────────────────────────────

def dev_rows(dev: dict) -> list[dict]:
    return [{"번호": f"D{n:02d}", **{f: d.get(f, "") for f in FIELDS}, "요약": d["summary"], "_item": d}
            for n, d in enumerate(dev["items"], start=1)]


def dev_summary(rows: list[dict], results: list[dict]) -> dict:
    """개발용 자료에는 사람 판정이 없으므로 주입 검출과 자연 응답의 있음 판정 건수·목록만 정리한다."""
    inj = [(r, j) for r, j in zip(rows, results) if r["_item"]["injected"]]
    nat = [(r, j) for r, j in zip(rows, results) if not r["_item"]["injected"]]
    return {
        "n": len(rows), "unparsed": sum(not j["parsed"] for j in results),
        "injected_detected": sum(j["verdict"] == YES for _, j in inj), "injected_n": len(inj),
        "injected_missed": [{"item_id": r["_item"]["item_id"], "summary": r["요약"], "verdict": j["verdict"]}
                            for r, j in inj if j["verdict"] != YES],
        "natural_yes": [{"item_id": r["_item"]["item_id"], "summary": r["요약"], "types": j["types"], "phrase": j["phrase"]}
                        for r, j in nat if j["verdict"] == YES],
        "natural_n": len(nat),
    }


def run_dev(client, crit: dict, write: bool = True) -> int:
    dev = json.loads((ROOT / crit["meta"]["dev_set"]).read_text(encoding="utf-8"))
    rows = dev_rows(dev)
    model = crit["meta"]["judge_model"]
    digest = get_model_digests(client, [model]).get(model)
    results, reloads = run_judge(client, model, rows)
    summary = dev_summary(rows, results)
    print(f"\n개발용 {summary['n']}건 · 읽지 못함 {summary['unparsed']} · 주입 검출 "
          f"{summary['injected_detected']}/{summary['injected_n']} · 자연 응답 있음 판정 {len(summary['natural_yes'])}/{summary['natural_n']}")
    if reloads:
        print("측정 환경 오염 — 실행 도중 모델 재로드: " + ", ".join(reloads))
    if write:
        DEV_RESULT.write_text(json.dumps({**run_meta(client, model, digest), "reloads": reloads, "summary": summary,
                                          "results": [{**j, "item_id": r["_item"]["item_id"]} for r, j in zip(rows, results)]},
                                         ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"저장: {rel(DEV_RESULT)}")
    return 2 if reloads else 0


# ── 측정 모드 (계획 §5, §6) ──────────────────────────────────

def read_rater_answers(path: Path, expected: list[str]) -> dict[str, dict]:
    """설계 의도 8 — {번호: {verdict, types, 확신도}}. 빈칸·선택지 밖 값·번호 불일치면 GateError."""
    if not path.exists():
        raise GateError(f"검토자 판정 파일이 없습니다: {path}")
    answers = json.loads(path.read_text(encoding="utf-8")).get("answers", {})
    out, problems = {}, []
    for jid, a in answers.items():
        verdict = str(a.get("판정") or "").strip()
        types = a.get("유형") or []
        if verdict not in (YES, NO):
            problems.append(f"{jid} 판정 {'빈칸' if not verdict else repr(verdict)}")
        if any(t not in TYPES for t in types) or (verdict == YES) != bool(types):
            problems.append(f"{jid} 유형이 판정과 맞지 않습니다")
        if a.get("확신도") not in CONFIDENCE:
            problems.append(f"{jid} 확신도 {a.get('확신도')!r}")
        out[jid] = {"verdict": verdict, "types": sorted(types), "확신도": a.get("확신도")}
    if sorted(out) != sorted(expected):
        problems.append("검토자 판정의 번호가 측정 세트와 다릅니다")
    if problems:
        raise GateError(f"검토자 판정에 고칠 곳이 {len(problems)}곳 있습니다 → " + "; ".join(problems))
    return out


def pct(a: int, b: int) -> float | None:
    return round(a / b * 100, 2) if b else None


def metrics(key: dict, human: dict, judged: dict, crit: dict) -> dict:
    """계획 §6 — 읽지 못한 건은 가장 불리하게 센다(기준 파일 규칙 5)."""
    nat = [j for j, e in key.items() if not e["injected"]]
    inj = [j for j, e in key.items() if e["injected"]]
    h = {j: human[j]["verdict"] for j in key}
    m = {j: judged[j]["verdict"] or "읽지 못함" for j in key}
    agree = sum(h[j] == m[j] for j in nat)
    human_yes = [j for j in nat if h[j] == YES]
    human_no = [j for j in nat if h[j] == NO]
    out = {
        "natural_n": len(nat), "natural_agree": agree, "natural_agreement": pct(agree, len(nat)),
        "natural_kappa": cohen_kappa([h[j] for j in nat], [m[j] for j in nat]),
        "natural_human_yes": len(human_yes),
        "false_alarm": sum(m[j] != NO for j in human_no), "false_alarm_rate": pct(sum(m[j] != NO for j in human_no), len(human_no)),
        "natural_judge_detected": sum(m[j] == YES for j in human_yes),
        "injected_n": len(inj), "injected_detected": sum(m[j] == YES for j in inj),
        "rater_injected_detected": sum(h[j] == YES for j in inj),
        "injected_type_match": sum(m[j] == YES and key[j]["kind"] in judged[j]["types"] for j in inj),   # 기록 — 유형별 일치
        "unparsed": sum(not judged[j]["parsed"] for j in key),
    }
    out["kappa_used"] = out["natural_human_yes"] >= crit["natural"]["min_positive_for_kappa"]
    out["injected_recall"] = pct(out["injected_detected"], len(inj))
    out["by_confidence"] = {c: {"n": len(js), "agree": sum(h[j] == m[j] for j in js)}
                            for c in ("높음", "보통", "낮음") if (js := [j for j in nat if human[j]["확신도"] == c])}
    out["near_boundary"] = (out["natural_agreement"] is not None
                            and abs(out["natural_agreement"] - crit["natural"]["agreement_min"]) <= 3.0)   # 계획 §7
    return out


def decide(mx: dict, crit: dict) -> dict:
    """계획 §6 — 기준값과 같으면 충족."""
    n, i = crit["natural"], crit["injected"]
    checks = [("자연 응답 일치율(%)", mx["natural_agreement"], f"≥ {n['agreement_min']}", mx["natural_agreement"] >= n["agreement_min"]),
              ("심은 결함 검출률(%)", mx["injected_recall"], f"≥ {i['recall_min']}", mx["injected_recall"] >= i["recall_min"]),
              ("거짓 경보율(%)", mx["false_alarm_rate"], f"≤ {n['false_alarm_max']}",
               mx["false_alarm_rate"] is not None and mx["false_alarm_rate"] <= n["false_alarm_max"])]
    if mx["kappa_used"]:
        checks.append(("자연 응답 κ", mx["natural_kappa"], f"≥ {n['kappa_min']}",
                       mx["natural_kappa"] is not None and mx["natural_kappa"] >= n["kappa_min"]))
    checks = [{"name": a, "value": b, "limit": c, "passed": d} for a, b, c, d in checks]
    return {"adopted": all(c["passed"] for c in checks), "checks": checks}


def render(meta: dict, mx: dict, verdict: dict, key: dict, human: dict, judged: dict) -> str:
    L = ["# v1.5 LLM-as-judge 판정 — 요약 품질", "",
         f"> 계획 [`quality_v15_plan.md`](quality_v15_plan.md) · 기준 `quality_v15.toml` · `src/triage_eval/pipeline/quality.py` 생성 · "
         f"채점관 `{meta['judge_model']}` (digest {meta['model_digest']}, 프롬프트 {meta['prompt_version']})", "",
         f"## 판정: **{'채택' if verdict['adopted'] else '미채택'}**", "",
         "| 기준 | 값 | 기준 | 결과 |", "| :--- | ---: | :--- | :---: |"]
    L += [f"| {c['name']} | {c['value']} | {c['limit']} | {'✅' if c['passed'] else '❌'} |" for c in verdict["checks"]]
    if not mx["kappa_used"]:
        L += ["", f"> 검토자의 자연 응답 문제 있음이 {mx['natural_human_yes']}건이라 κ({mx['natural_kappa']})는 기록만 했다(계획 §6)."]
    conf = ", ".join(f"{c} {v['agree']}/{v['n']}" for c, v in mx["by_confidence"].items())
    L += ["", "## 기록 (판정에 쓰지 않음)", "",
          f"- 자연 응답 {mx['natural_n']}건: 일치 {mx['natural_agree']}, 검토자 있음 {mx['natural_human_yes']}건 중 채점관 있음 "
          f"{mx['natural_judge_detected']}건, 거짓 경보 {mx['false_alarm']}건",
          f"- 주입 {mx['injected_n']}건: 채점관 검출 {mx['injected_detected']}, 유형까지 일치 {mx['injected_type_match']}, "
          f"검토자 검출 {mx['rater_injected_detected']}",
          f"- 검토자 확신도별 자연 응답 일치: {conf}",
          f"- 채점관 출력을 읽지 못한 건: {mx['unparsed']}",
          *(["- 자연 응답 일치율이 기준 ±3%p 안 — 외부 QA 채점으로 사람끼리 일치도를 확인할 후보(계획 §7)"] if mx["near_boundary"] else []),
          "", "## 주입 결함별 결과", "", "| 번호 | 종류 | 채점관 | 채점관 유형 | 지목 문구 | 검토자 |", "| :--- | :--- | :--- | :--- | :--- | :--- |"]
    for j, e in sorted(key.items()):
        if e["injected"]:
            L.append(f"| {j} | {e['kind']} | {judged[j]['verdict'] or '읽지 못함'} | {','.join(judged[j]['types']) or '—'} | "
                     f"{judged[j]['phrase'] or '—'} | {human[j]['verdict']} {','.join(human[j]['types'])} |")
    L += ["", "## 자연 응답에서 판정이 갈린 건", "", "| 번호 | 요약 | 검토자 | 채점관 | 채점관 유형·지목 문구 |", "| :--- | :--- | :--- | :--- | :--- |"]
    for j, e in sorted(key.items()):
        if not e["injected"] and human[j]["verdict"] != judged[j]["verdict"]:
            L.append(f"| {j} | {e['summary']} | {human[j]['verdict']} {','.join(human[j]['types'])} | {judged[j]['verdict'] or '읽지 못함'} | "
                     f"{','.join(judged[j]['types'])} {judged[j]['phrase'] or '—'} |")
    return "\n".join(L) + "\n"


def git_lines(*args: str) -> list[str]:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True)
    return [x for x in out.stdout.decode("utf-8").splitlines() if x.strip()]


def measure_guard(client, crit: dict) -> str | None:
    """설계 의도 7 — 측정해도 되면 실행 전 모델 digest, 아니면 GateError."""
    problems = []
    if RESULT_JSON.exists():
        problems.append(f"측정 결과가 이미 있습니다({rel(RESULT_JSON)}). 측정은 한 번만 한다")
    if STARTED.exists():
        problems.append(f"측정을 시작한 기록이 있습니다({rel(STARTED)}). 중간에 끊긴 측정이면 원자료를 확인하고 기록을 남긴 뒤 처리한다")
    for f in GUARDED:
        if not git_lines("ls-files", "--", f):
            problems.append(f"{f} 가 저장소에 커밋되어 있지 않습니다")
    dirty = git_lines("status", "--porcelain", "--", *GUARDED)
    if dirty:
        problems.append("커밋하지 않은 변경이 있습니다: " + ", ".join(dirty))
    dev = json.loads(DEV_RESULT.read_text(encoding="utf-8")) if DEV_RESULT.exists() else {}
    model = crit["meta"]["judge_model"]
    digest = get_model_digests(client, [model]).get(model)
    if dev.get("prompt_sha256") != prompt_fingerprint():
        problems.append("프롬프트 지문이 개발용 실행과 다릅니다 — 확정한 프롬프트가 아닙니다")
    if dev.get("model_digest") != digest:
        problems.append(f"모델 digest가 개발용 실행과 다릅니다 ({dev.get('model_digest')} → {digest})")
    if problems:
        raise GateError("; ".join(problems))
    return digest


def record_invalid(meta: dict, reasons: list[str]) -> None:
    runs = json.loads(INVALID_RUNS.read_text(encoding="utf-8")) if INVALID_RUNS.exists() else []
    runs.append({**meta, "reasons": reasons})
    INVALID_RUNS.write_text(json.dumps(runs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_measure(client, crit: dict, write: bool = True, guard: bool = True) -> int:
    if not write:
        raise GateError("측정 모드는 결과를 반드시 기록한다(--no-write 불가, 설계 의도 7)")
    measure = json.loads((ROOT / crit["meta"]["measure_set"]).read_text(encoding="utf-8"))
    key = json.loads((ROOT / crit["meta"]["answer_key"]).read_text(encoding="utf-8"))["map"]
    human = read_rater_answers(RATER_ANSWERS, list(key))   # 설계 의도 8 — 채점관 실행 전에 확인
    model = crit["meta"]["judge_model"]
    digest = measure_guard(client, crit) if guard else get_model_digests(client, [model]).get(model)
    STARTED.write_text(json.dumps({"started_at": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n", encoding="utf-8")   # 설계 의도 8
    results, reloads = run_judge(client, model, measure["items"], echo=False)
    STARTED.write_text(json.dumps({"results": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    meta = {**run_meta(client, model, digest)}
    if reloads:
        record_invalid(meta, reloads)
        STARTED.unlink()
        raise GateError("측정 환경 오염 — 실행 도중 모델 재로드·통신 오류: " + ", ".join(reloads)
                        + f". {rel(INVALID_RUNS)} 에 기록했다. 환경을 정리하고 처음부터 다시 측정한다")
    judged = {r["번호"]: r for r in results}
    mx = metrics(key, human, judged, crit)
    verdict = decide(mx, crit)
    RESULT_JSON.write_text(json.dumps({**meta, "metrics": mx, "verdict": verdict, "human": human, "judge": judged},   # 설계 의도 7
                                      ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    REPORT.write_text(render(meta, mx, verdict, key, human, judged), encoding="utf-8")
    for c in verdict["checks"]:
        print(f"  {'✅' if c['passed'] else '❌'} {c['name']}: {c['value']} ({c['limit']})")
    print(f"판정: {'채택' if verdict['adopted'] else '미채택'}")
    print(f"저장: {rel(RESULT_JSON)}, {rel(REPORT)}")
    return 0 if verdict["adopted"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="v1.5 LLM-as-judge 요약 품질 판정")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dev", action="store_true", help="개발용 자료로 채점관 실행 (계획 §5)")
    mode.add_argument("--measure", action="store_true", help="측정 세트로 1회 측정")
    p.add_argument("--no-write", action="store_true")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):   # 설계 의도 7 — Windows 콘솔(cp949)에서도 ✅·한글 출력
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    import ollama

    try:
        crit = load_criteria()
        if args.dev:
            return run_dev(ollama.Client(), crit, write=not args.no_write)
        return run_measure(ollama.Client(), crit, write=not args.no_write)
    except GateError as e:
        print(f"판정 불가: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
