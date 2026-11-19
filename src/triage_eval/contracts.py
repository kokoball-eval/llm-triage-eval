"""응답 계약 — 모델 응답의 8칸 형식, 허용 값, 읽기(파싱), 형식 규칙. 다른 모든 모듈이 이 정의만 보고 맞춘다.

칸 (순서 고정)
    [요약] [분류] [모듈] [우선순위] [재현 정보] [발생 빈도] [처리] [누락 정보 및 권장 조치]

형식 규칙
    R1 응답이 '[요약]'으로 시작        R2 8칸이 모두 있음          R3 칸 순서가 규격과 같음
    R4 빈 줄 없음                      R5 마지막 칸 앞에 칸이 아닌 줄 없음
    R6 선택지 칸 5개와 [모듈] 값이 허용 값 안      R7 응답이 출력 언어(config.OUTPUT_LANG)의 문자만 씀

[설계 의도]
1. 허용 값은 여기 한 곳에서만 정의한다. 프롬프트·재요청·안전장치·채점이 모두 이 값을 쓴다.
2. 값 비교 전에 앞뒤 공백, 끝의 마침표, 감싼 따옴표·백틱만 정리한다. 동의어 매핑("Major급" → Major)은 하지 않는다.
   고쳐 주면 형식 위반이 점수에서 사라져, 실제 운영에서 읽지 못할 응답을 정상으로 오판하게 된다.
3. [모듈]은 복합 이슈일 때 "/"로 병기할 수 있다. "·"는 모듈 이름 안에 있으므로(계정·로그인) 구분자로 쓰지 않는다.
4. (R7) 출력 언어는 입력 언어와 별개로 "BTS를 쓰는 팀이 읽는 언어"로 고정한다. 7B 모델이 한국어 응답 도중 중국어로
   넘어가는 실패가 있었다. 판정은 금지 목록이 아니라 허용 목록으로 한다: 한글, ASCII(Critical·SSR 같은 값과 용어),
   일반 구두점·화살표·원문자만 허용하고 나머지는 모두 위반이다. 한계: 악센트 없는 라틴 문자로 쓴 외국어 문장은 잡지 못한다.
"""

import re

from triage_eval.config import OUTPUT_LANG

FIELDS = ["요약", "분류", "모듈", "우선순위", "재현 정보", "발생 빈도", "처리", "누락 정보 및 권장 조치"]
ENUMS = {
    "분류": ["결함", "개선 제안", "버그 아님", "문의·건의", "중복 의심", "무관"],
    "우선순위": ["Critical", "Major", "Minor", "Trivial", "판단보류", "해당 없음"],
    "재현 정보": ["충분", "부족"],
    "발생 빈도": ["항상", "간헐적", "1회", "미기재", "해당 없음"],
    "처리": ["긴급 사인 요청", "등록(재현 대기)", "개발 배정", "정보 요청 후 보류", "CS 응대", "폐기"],
}
MODULES = ["클라이언트 안정성", "네트워크", "계정·로그인", "결제·재화", "콘텐츠 진행",
           "게임플레이·밸런스", "그래픽·사운드", "UI·텍스트", "플랫폼 호환성", "보안·어뷰징"]
NO_MODULE = "해당 없음"
FREE_TEXT_FIELDS = ["요약", "누락 정보 및 권장 조치"]   # 선택지가 없는 칸. 다른 문자 체계는 여기에서만 생길 수 있다
LANG_NAMES = {"ko": "한국어", "en": "영어"}

# 설계 의도 4 — 출력 언어에서 쓰는 문자만 나열하고, 그 밖의 문자는 모두 위반으로 본다.
_COMMON = (r"\x09\x0a\x0d\x20-\x7e"        # ASCII
           r"·×°℃"      # · × ° ℃
           r"‐-⁞"                  # 일반 구두점 (— … “ ” ‘ ’)
           r"←-⇿"                  # 화살표
           r"①-⓿■-◿☀-⛿")  # ① ■ ★ 등
_HANGUL = r"가-힣ᄀ-ᇿ㄰-㆏"
FORBIDDEN_SCRIPTS = {"ko": re.compile(f"[^{_COMMON}{_HANGUL}]"), "en": re.compile(f"[^{_COMMON}]")}

# 한 줄 자유 서술 값의 허용 문자 (재요청 JSON 스키마의 pattern). 위 허용 목록에서 줄바꿈·탭과
# JSON 문자열을 깨뜨리는 큰따옴표·역슬래시를 뺐다. 문법 변환기가 \u 이스케이프를 못 읽을 수 있어 실제 문자로 쓴다.
_LINE_ASCII = " !#-\\[\\]-~"
_LINE_EXTRA = ("·×°℃‐-⁞←-⇿"
               "①-⓿■-◿☀-⛿")
_LINE_HANGUL = "가-힣ᄀ-ᇿ㄰-㆏"
LINE_PATTERNS = {"ko": f"^[{_LINE_ASCII}{_LINE_EXTRA}{_LINE_HANGUL}]+$", "en": f"^[{_LINE_ASCII}{_LINE_EXTRA}]+$"}

LABEL_RE = re.compile(r"^\[(?P<label>[^\]]+)\]\s*:\s*(?P<value>.*)$")


def parse_lines(text: str) -> list[tuple[str, str, int]]:
    """응답을 줄 단위로 (칸 이름, 값, 줄 번호)로 나눈다. 빈 줄은 __BLANK__, 칸이 아닌 줄은 __TEXT__."""
    parsed = []
    for idx, line in enumerate(text.split("\n")):
        stripped = line.strip()
        if not stripped:
            parsed.append(("__BLANK__", "", idx))
            continue
        m = LABEL_RE.match(stripped)
        parsed.append((m.group("label").strip(), m.group("value").strip(), idx) if m else ("__TEXT__", stripped, idx))
    return parsed


def clean(value: str) -> str:
    """설계 의도 2 — 표기 잡음만 정리한다."""
    v = (value or "").strip().strip("`'\"").strip()
    return v[:-1].strip() if v.endswith(".") else v


def split_modules(value: str) -> list[str]:
    """설계 의도 3 — '/' 와 ',' 로만 나눈다."""
    parts = [clean(p) for p in clean(value).replace(",", "/").split("/")]
    return [p for p in parts if p]


def parse_response(text: str) -> dict:
    """응답에서 8칸 값을 꺼낸다. 같은 칸이 여러 번 나오면 첫 값을 쓴다."""
    values = {}
    for label, value, _ in parse_lines((text or "").strip()):
        if label in FIELDS and label not in values:
            values[label] = clean(value) if label != "모듈" else value.strip()
    return values


def enum_errors(values: dict) -> list[tuple[str, str | None]]:
    """R6 — 허용 값 밖인 (칸, 값) 목록. 형식 판정과 재요청 안내문이 함께 쓴다."""
    bad = [(f, values.get(f)) for f, allowed in ENUMS.items() if values.get(f) not in allowed]
    mods = split_modules(values.get("모듈", ""))
    if not mods or any(m not in MODULES + [NO_MODULE] for m in mods) or (NO_MODULE in mods and len(mods) > 1):
        bad.append(("모듈", values.get("모듈")))
    return bad


def language_errors(values: dict, lang: str = OUTPUT_LANG) -> list[tuple[str, str]]:
    """R7 위반 위치 — 출력 언어 밖 문자가 섞인 자유 서술 칸 (칸, 섞인 문자). 선택지 칸은 R6에서 잡힌다."""
    out = []
    for f in FREE_TEXT_FIELDS:
        found = FORBIDDEN_SCRIPTS[lang].findall(values.get(f) or "")
        if found:
            out.append((f, "".join(dict.fromkeys(found))))
    return out


def replace_field(text: str, field: str, value: str) -> str:
    """칸 한 줄의 값만 바꾼다. 다른 줄과 빈 줄은 그대로 둔다. 칸 줄이 없으면 원문을 돌려준다."""
    pattern = re.compile(rf"^(\s*\[{re.escape(field)}\]\s*[:：]).*$", re.M)
    return pattern.sub(lambda m: f"{m.group(1)} {value}", text, count=1)


def check_format(text: str, lang: str = OUTPUT_LANG) -> dict:
    """R1~R7 판정. strict = 전부 통과, parsable = R4(빈 줄)만 허용."""
    text = (text or "").strip()
    parsed = parse_lines(text)
    labels = [lbl for lbl, _, _ in parsed if lbl not in ("__BLANK__", "__TEXT__")]
    values = parse_response(text)
    results, reasons = {}, {}

    results["R1_no_preamble"] = text.startswith("[요약]")
    if not results["R1_no_preamble"]:
        reasons["R1_no_preamble"] = f"첫 40자: {text[:40]!r}"
    missing = [f for f in FIELDS if f not in labels]
    results["R2_all_fields"] = not missing
    if missing:
        reasons["R2_all_fields"] = f"누락 칸: {missing}"
    spec = [lbl for lbl in labels if lbl in FIELDS]
    results["R3_field_order"] = spec == FIELDS
    if not results["R3_field_order"]:
        reasons["R3_field_order"] = f"실제 순서: {spec}"
    blanks = [idx for lbl, _, idx in parsed if lbl == "__BLANK__"]
    results["R4_no_blank_line"] = not blanks
    if blanks:
        reasons["R4_no_blank_line"] = f"빈 줄 {len(blanks)}개"
    last = next((pos for pos, (lbl, _, _) in enumerate(parsed) if lbl == FIELDS[-1]), None)
    stray = [val for pos, (lbl, val, _) in enumerate(parsed) if lbl == "__TEXT__" and (last is None or pos < last)]
    results["R5_no_stray_text"] = not stray
    if stray:
        reasons["R5_no_stray_text"] = f"칸이 아닌 줄 {len(stray)}개: {[s[:30] for s in stray]}"
    bad = [f"{f}={v!r}" for f, v in enum_errors(values)]
    results["R6_enum_valid"] = not bad
    if bad:
        reasons["R6_enum_valid"] = ", ".join(bad)
    foreign = FORBIDDEN_SCRIPTS[lang].findall(text)
    results["R7_output_language"] = not foreign
    if foreign:
        reasons["R7_output_language"] = f"{LANG_NAMES[lang]} 외 문자 {len(foreign)}자: {''.join(foreign[:10])!r}"
    return {
        "rules": results,
        "failed_rules": [k for k, v in results.items() if not v],
        "reasons": reasons,
        "strict_pass": all(results.values()),
        "parsable_pass": all(v for k, v in results.items() if k != "R4_no_blank_line"),
    }
