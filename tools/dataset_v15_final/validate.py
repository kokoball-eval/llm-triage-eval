"""v1.5 새 최종 평가용 세트 검증 (계획: report/final_v15_plan.md §1·§2).

[설계 의도]
1. 계획서의 할당표(60문항, 위험 문항 30개)와 작성 규칙(형식·새로운 문항)을 코드로 검사한다. 사람이 세어 틀리는 것을 막는다.
2. 위험 라벨은 문항당 하나만 허용한다. 위험 문항 수(X-1 16·X-3 10·X-2 4)와 일반 문항 30이 서로 겹치지 않게 세기 위해서다.
3. X-1 문항은 정답이 Critical 하나뿐이어야 한다(기준서 §7: Major·등록을 함께 허용한 문항은 X-1 측정 대상이 아니다).
4. 기존 평가셋(개발용 63 + 기존 최종 25)과 새 문항끼리 글자 유사도 0.85 이상이면 거부한다. 같은 상황을 말만 바꾼 문항을 막는 것은 계획 §2-2다.
5. 부분 검사(`--part`)는 문항 형식·라벨 규칙·유사도만 보고 할당표는 보지 않는다. 문항을 나눠 쓰는 동안 쓰는 쪽이 바로 확인하게 한다.
6. 이 도구는 모델을 호출하지 않고 결과 파일을 읽지 않는다.
"""

import argparse
import difflib
import importlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXISTING = ROOT / "data" / "eval_v15" / "aether_raid_v15.json"
TOOLS = Path(__file__).resolve().parent

CATEGORIES = ["분류", "우선순위", "모듈", "재현 정보", "처리", "발생 빈도"]
VOCAB = {
    "분류": {"결함", "개선 제안", "버그 아님", "문의·건의", "중복 의심", "무관"},
    "우선순위": {"Critical", "Major", "Minor", "Trivial", "판단보류", "해당 없음"},
    "모듈": {"클라이언트 안정성", "네트워크", "계정·로그인", "결제·재화", "콘텐츠 진행", "게임플레이·밸런스",
             "그래픽·사운드", "UI·텍스트", "플랫폼 호환성", "보안·어뷰징", "해당 없음"},
    "재현 정보": {"충분", "부족"},
    "처리": {"긴급 사인 요청", "등록(재현 대기)", "개발 배정", "정보 요청 후 보류", "CS 응대", "폐기"},
    "발생 빈도": {"항상", "간헐적", "1회", "미기재", "해당 없음"},
}
MODULES10 = sorted(VOCAB["모듈"] - {"해당 없음"})
RISKS = ("X-1", "X-2", "X-3")
SIMILARITY_LIMIT = 0.85

# 계획 §1 할당표
QUOTA_RISK = {"X-1": 16, "X-3": 10, "X-2": 4, None: 30}
QUOTA_TRACK_RISK = {("A", "X-1"): 8, ("A", "X-3"): 7, ("A", "X-2"): 4, ("A", None): 21,
                    ("B", "X-1"): 8, ("B", "X-3"): 3, ("B", "X-2"): 0, ("B", None): 9}
QUOTA_A_GENERAL = {"명확": 3, "단문": 4, "복합": 2, "중복": 3, "문의": 3, "무관": 6}
QUOTA_B_GENERAL_PRIORITY = {"Major": 3, "Minor": 3, "Trivial": 1}   # 트랙 B 일반 명확 7건의 첫 정답 우선순위
X1_ACCOUNT_PAYMENT = {"결제·재화", "계정·로그인"}
ID_PATTERN = re.compile(r"^N([AB])(\d{2})$")
CLOSING = {"개발 배정", "CS 응대", "폐기"}   # 사람 확인 없이 끝나는 처리 (score.py 의 X-3 판정과 같다)
POSTED = re.compile(r"^2026-11-(19|2[0-3]) ([01]\d|2[0-3]):[0-5]\d$")   # 업데이트 2026-11-19, 게시는 23일까지
BOARDS = {"공식 홈페이지 > 버그 제보", "자유게시판"}
TRACK_COUNT = {"A": 40, "B": 20}


def text_of(item):
    i = item["input"]
    return (i.get("title", "") + " " + i.get("body", "")).strip()


def risk_of(item):
    r = item["labels"]["risk"]
    return r[0] if r else None


def check_item(item, review="pending"):
    """문항 하나의 형식과 라벨 규칙. 문제 목록을 돌려준다. review 는 기대하는 검토 상태(초안 pending, 봉인본 approved)."""
    k = item.get("id", "?")
    p = []
    m = ID_PATTERN.match(k)
    if not m:
        return [f"{k}: 문항 번호는 NA01~NA40, NB01~NB20 형식이어야 합니다"]
    if item.get("track") != m.group(1):
        p.append(f"{k}: 트랙 {item.get('track')} 이(가) 번호와 맞지 않습니다")
    if (item.get("split"), item.get("set"), item.get("review")) != ("final", "representative", review):
        p.append(f"{k}: split·set·review 값이 규칙(final·representative·{review})과 다릅니다")
    inp = item.get("input", {})
    if not inp.get("title") or not inp.get("body"):
        p.append(f"{k}: 제목 또는 본문이 비어 있습니다")
    if not item.get("quality") or not item.get("rationale"):
        p.append(f"{k}: quality 또는 rationale 이 비어 있습니다")
    ctx = inp.get("context", {})
    if not ctx.get("update_note") or not ctx.get("known_issues"):
        p.append(f"{k}: context(업데이트 공지·알려진 이슈)가 없습니다")
    lb = item.get("labels", {})
    for c in CATEGORIES:
        vals = lb.get(c)
        if not isinstance(vals, list) or not vals:
            p.append(f"{k}: 라벨 {c} 이(가) 비어 있습니다")
        elif not set(vals) <= VOCAB[c]:
            p.append(f"{k}: 라벨 {c} 에 허용되지 않은 값 {sorted(set(vals) - VOCAB[c])}")
        elif len(set(vals)) != len(vals):
            p.append(f"{k}: 라벨 {c} 에 같은 값이 겹칩니다")
    if not isinstance(lb.get("human_review"), bool):
        p.append(f"{k}: human_review 는 true/false 여야 합니다")
    for f in ("must_request", "must_recommend"):
        if not isinstance(lb.get(f), list):
            p.append(f"{k}: {f} 는 목록이어야 합니다")
    risk = lb.get("risk")
    if not isinstance(risk, list) or len(risk) > 1 or not set(risk) <= set(RISKS):
        p.append(f"{k}: risk 는 비어 있거나 X-1·X-2·X-3 중 하나여야 합니다(한 문항에 하나)")
        return p
    if p:
        return p
    if risk == ["X-1"]:
        if lb["우선순위"] != ["Critical"] or lb["분류"] != ["결함"] or lb["처리"] != ["긴급 사인 요청"]:
            p.append(f"{k}: X-1 문항은 분류 결함·우선순위 Critical 하나·처리 긴급 사인 요청 하나여야 합니다(다른 처리를 허용하면 측정 대상이 아닙니다)")
    if risk == ["X-2"]:
        if lb["분류"] != ["결함"] or "Critical" in lb["우선순위"] or "폐기" in lb["처리"]:
            p.append(f"{k}: X-2 문항은 분류 결함·Critical 없음·처리에 폐기 없음이어야 합니다")
    if risk == ["X-3"]:
        if not lb["human_review"] or not re.search(r"P-B[1-6]", item["rationale"]):
            p.append(f"{k}: X-3 문항은 human_review 가 true 이고 rationale 에 경계 규칙(P-B1~B6)이 있어야 합니다")
        if set(lb["처리"]) & CLOSING:
            p.append(f"{k}: X-3 문항의 정답 처리에 사람 확인 없이 끝나는 처리({sorted(set(lb['처리']) & CLOSING)})가 있습니다")
    if not risk and not lb["human_review"] and lb["처리"][0] == "긴급 사인 요청":
        p.append(f"{k}: 위험 라벨이 없는데 첫 처리가 긴급 사인 요청입니다(일반 문항은 X-1 이 아닙니다)")
    if not risk and "Critical" in lb["우선순위"]:
        p.append(f"{k}: 위험 라벨이 없는데 우선순위 정답에 Critical 이 있습니다(X-1 후보가 위험 집계에서 빠집니다)")
    if item["track"] == "A":
        if not POSTED.match(inp.get("posted_at", "")) or inp.get("board") not in BOARDS:
            p.append(f"{k}: 트랙 A 의 게시 시각은 2026-11-19 00:00~2026-11-23 23:59 형식이고 게시판은 {sorted(BOARDS)} 중 하나여야 합니다")
    else:
        body = inp.get("body", "")
        if not all(f"[{h}]" in body for h in ("테스트 환경", "재현율", "이슈 설명", "기대 결과", "실제 결과", "참고 문서")):
            p.append(f"{k}: 트랙 B 본문에 양식 항목(테스트 환경·재현율·이슈 설명·기대 결과·실제 결과·참고 문서)이 빠졌습니다")
    return p


def check_similarity(new_items, existing_items):
    """기존 문항·새 문항끼리 유사도. (문제 목록, 참고용 높은 유사도 목록)"""
    problems, notes = [], []
    pool = [(i["id"], text_of(i)) for i in existing_items]
    for idx, it in enumerate(new_items):
        t = text_of(it)
        others = pool + [(o["id"], text_of(o)) for o in new_items[:idx]]
        for oid, ot in others:
            r = difflib.SequenceMatcher(None, t, ot).ratio()
            if r >= SIMILARITY_LIMIT:
                problems.append(f"{it['id']} 가 {oid} 와 글자 유사도 {r:.2f} (기준 {SIMILARITY_LIMIT} 미만)")
            elif r >= 0.6:
                notes.append(f"{it['id']} ~ {oid} 유사도 {r:.2f}")
    return problems, notes


def check_quota(items):
    """계획 §1 할당표. 전체 세트에서만 호출한다."""
    p = []
    ids = [i["id"] for i in items]
    if len(set(ids)) != len(ids):
        p.append("문항 번호가 겹칩니다")
    want = {f"NA{n:02d}" for n in range(1, 41)} | {f"NB{n:02d}" for n in range(1, 21)}
    if set(ids) != want:
        p.append(f"문항 번호가 NA01~NA40, NB01~NB20 과 다릅니다(빠짐 {sorted(want - set(ids))[:5]}, 더함 {sorted(set(ids) - want)[:5]})")
    if Counter(i["track"] for i in items) != Counter(TRACK_COUNT):
        p.append(f"트랙별 문항 수가 {TRACK_COUNT} 와 다릅니다")
    by_risk = Counter(risk_of(i) for i in items)
    if dict(by_risk) != QUOTA_RISK:
        p.append(f"위험 문항 수가 할당표와 다릅니다: {dict(by_risk)} (할당 {QUOTA_RISK})")
    by_tr = Counter((i["track"], risk_of(i)) for i in items)
    for key, n in QUOTA_TRACK_RISK.items():
        if by_tr.get(key, 0) != n:
            p.append(f"트랙 {key[0]} 위험 {key[1]} 문항 수 {by_tr.get(key, 0)} (할당 {n})")
    general_a = Counter(i["quality"] for i in items if i["track"] == "A" and not risk_of(i))
    if dict(general_a) != QUOTA_A_GENERAL:
        p.append(f"트랙 A 일반 문항 구성이 다릅니다: {dict(general_a)} (할당 {QUOTA_A_GENERAL})")
    general_b = [i for i in items if i["track"] == "B" and not risk_of(i)]
    clear = [i for i in general_b if i["quality"] == "명확"]
    if len(clear) != 7 or Counter(i["labels"]["우선순위"][0] for i in clear) != Counter(QUOTA_B_GENERAL_PRIORITY):
        p.append("트랙 B 일반 명확 문항은 7건(첫 정답 Major 3·Minor 3·Trivial 1)이어야 합니다")
    rest = Counter(i["quality"] for i in general_b if i["quality"] != "명확")
    if rest.get("중복") != 1 or rest.get("건의", 0) + rest.get("문의", 0) != 1 or sum(rest.values()) != 2:
        p.append(f"트랙 B 일반 나머지 2건은 중복 1건과 건의 또는 문의 1건이어야 합니다: {dict(rest)}")
    mods = {m for i in items for m in i["labels"]["모듈"]}
    if not set(MODULES10) <= mods:
        p.append(f"판정 모듈이 빠졌습니다: {sorted(set(MODULES10) - mods)}")
    x1 = [i for i in items if risk_of(i) == "X-1"]
    if sum(i["labels"]["모듈"][0] in X1_ACCOUNT_PAYMENT for i in x1) < 4:
        p.append("X-1 문항 중 첫 모듈이 결제·재화 또는 계정·로그인인 문항이 4건 미만입니다")
    if sum(bool(re.search(r"H-1b(?!가 아니|이 아니| 아님)", i["rationale"])) for i in x1) < 2:
        p.append("X-1 문항 중 rationale 에 H-1b(서버 쪽 장애 의심)가 있는 문항이 2건 미만입니다")
    return p


def validate(items, existing_items, full=True, review="pending"):
    problems = []
    for it in items:
        problems += check_item(it, review)
    sim, notes = check_similarity(items, existing_items)
    problems += sim
    if full:
        problems += check_quota(items)
    return problems, notes


def existing_items():
    return json.loads(EXISTING.read_text(encoding="utf-8"))["items"]


def load_part(path):
    """`items_*.py` 파일에서 ITEMS 를 읽는다."""
    sys.path.insert(0, str(TOOLS))
    mod = importlib.import_module(Path(path).stem)
    return list(mod.ITEMS)


def main(argv=None):
    ap = argparse.ArgumentParser(description="v1.5 새 최종 평가용 세트 초안 검증")
    ap.add_argument("--part", help="부분 검사: items_*.py 파일 경로 (할당표는 보지 않는다)")
    args = ap.parse_args(argv)
    if args.part:
        items = load_part(args.part)
        problems, notes = validate(items, existing_items(), full=False)
    else:
        sys.path.insert(0, str(TOOLS))
        import build_final_v15
        items = build_final_v15.collect()
        problems, notes = validate(items, existing_items(), full=True)
    for n in notes:
        print("참고:", n)
    for q in problems:
        print("문제:", q)
    print(f"문항 {len(items)}건 · 문제 {len(problems)}건")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
