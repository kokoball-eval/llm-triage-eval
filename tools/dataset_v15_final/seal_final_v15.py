"""v1.5 새 최종 평가용 세트 봉인 (계획: report/final_v15_plan.md §3 순서 4).

[설계 의도]
1. 봉인은 검토자가 60문항을 모두 승인했고 승인 뒤 문항이 바뀌지 않았을 때만 한다. 승인 기록의 문항 해시와 초안 문항의 해시를 대조한다.
2. 검증(형식·할당표·글자 유사도)을 다시 통과해야 한다. 하나라도 걸리면 파일을 쓰지 않는다.
3. 봉인 파일이 이미 있으면 덮어쓰지 않는다. 봉인 뒤 평가셋을 고치지 않는 규칙(CLAUDE.md)을 도구가 먼저 막는다.
4. 평가셋의 `review` 를 approved 로 바꾸고 상태를 sealed 로 표시한다. 문항의 입력·라벨·근거는 승인된 그대로 둔다.
5. 봉인 기록(`sealed_v15.json`)에 평가셋 SHA-256, 승인 기록 SHA-256, 문항 수, 할당표 결과, 유사도 검사 결과와 한계, 검토 중 바뀐 문항,
   기준서와 다른 검토자 판단을 남긴다. 9단계 도구는 같은 `verify()` 를 통과해야 실행된다.
6. 모델을 호출하지 않고 모델 응답 기록을 읽지 않는다.
7. 해시는 줄바꿈을 LF로 맞춘 뒤 계산한다. 저장소는 LF로 저장하고 Windows 체크아웃은 CRLF로 바뀌므로(.gitattributes), 바이트 그대로 해시하면 환경마다 값이 달라진다.
"""

import argparse
import difflib
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import review_server as rs  # noqa: E402
import validate as v  # noqa: E402

DIR = v.ROOT / "data" / "eval_v15_final"
DRAFT = DIR / "draft_v15_final.json"
ANSWERS = DIR / "review_answers_v15_final.json"
FINAL = DIR / "aether_raid_v15_final.json"
SEALED = DIR / "sealed_v15.json"

# 검토자가 기준서 v1.5 와 다르게 판단한 문항 (검토 기록 메모 기준, v1.6 후보 V16-2)
GUIDELINE_DIFFERENT = {
    "NA16": "P-B4(지형지물 공략)를 기준서의 Major~Minor·처리 등록(재현 대기) 대신 우선순위 Critical·Major, 처리 긴급 사인 요청(첫 값)·등록(재현 대기)으로 판단 — 악용이 반복될 여지",
    "NA18": "P-B6 3(공지와 다른 수치)을 기준서의 Critical·긴급 사인 요청(처리 순서 3) 대신 Major·등록(재현 대기)로 판단 — 게임 플레이에 지대한 영향까지는 아님",
    "NB11": "P-B6 3(공지와 다른 수치)을 기준서의 Critical·긴급 사인 요청(처리 순서 3) 대신 Major·등록(재현 대기)로 판단 — 게임 진행에 치명적이지 않음",
}


def sha256_of(path: Path) -> str:
    """줄바꿈(CRLF·LF)에 관계없이 같은 값 — Windows 체크아웃과 CI(LF)에서 해시가 같도록 LF로 맞춘 뒤 계산한다(설계 의도 7)."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def similarity_summary(items: list[dict], existing: list[dict]) -> dict:
    """기존 88문항·새 문항끼리의 최고 유사도(참고) — 기준 미만이어야 봉인된다."""
    pool = [(i["id"], v.text_of(i)) for i in existing]
    best = (0.0, "", "")
    for idx, it in enumerate(items):
        t = v.text_of(it)
        for oid, ot in pool + [(o["id"], v.text_of(o)) for o in items[:idx]]:
            r = difflib.SequenceMatcher(None, t, ot).ratio()
            if r > best[0]:
                best = (r, it["id"], oid)
    return {"method": "difflib.SequenceMatcher(제목+본문) 비율", "limit": v.SIMILARITY_LIMIT, "compared_existing": len(existing),
            "max_ratio": round(best[0], 4), "max_pair": [best[1], best[2]]}


def seal(dry_run: bool = False) -> dict:
    if FINAL.exists() or SEALED.exists():
        raise SystemExit("이미 봉인되어 있습니다. 봉인 파일은 덮어쓰지 않습니다(CLAUDE.md: 이전 버전 평가셋은 고치지 않는다).")
    draft, answers = load(DRAFT), load(ANSWERS)
    items = draft["items"]
    problems, _ = v.validate(items, v.existing_items(), full=True)
    for it in items:
        a = answers["answers"].get(it["id"])
        if not a or a["결정"] != "승인":
            problems.append(f"{it['id']}: 검토자 승인이 없습니다")
        elif a.get("hash") != rs.item_hash(it):
            problems.append(f"{it['id']}: 승인 뒤 문항이 바뀌었습니다(다시 승인 필요)")
    if problems:
        for p in problems:
            print("문제:", p)
        raise SystemExit("봉인하지 않았습니다.")

    final_items = []
    for it in items:
        it = json.loads(json.dumps(it))
        it["review"] = "approved"
        it["provenance"]["labels"] = "Claude 제안 → 검토자 승인 (검토 화면, 2026-11-17)"
        final_items.append(it)
    doc = {k: val for k, val in draft.items() if k not in ("name", "version", "status", "items")}
    doc.update({
        "name": "Aether Raid v1.5 새 최종 평가용 세트",
        "version": "v1.5-final",
        "status": "sealed — 9단계에서 한 번만 평가용으로 쓴다. 이 파일은 고치지 않는다(해시는 sealed_v15.json)",
        "items": final_items,
    })
    if dry_run:
        print("검증·승인 확인 통과(파일은 쓰지 않았습니다).")
        return {}
    FINAL.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    by_risk = Counter(v.risk_of(i) for i in final_items)
    changed = sorted({c["번호"] for c in answers["changes"]})
    record = {
        "version": "v1.5-final",
        "sealed_on": "2026-11-17",
        "plan": "report/final_v15_plan.md",
        "dataset": {"path": "data/eval_v15_final/aether_raid_v15_final.json", "sha256": sha256_of(FINAL), "items": len(final_items)},
        "review_record": {"path": "data/eval_v15_final/review_answers_v15_final.json", "sha256": sha256_of(ANSWERS),
                          "approved": len(final_items), "items_with_review_history": changed},
        "counts": {"track": dict(Counter(i["track"] for i in final_items)),
                   "risk": {str(k): n for k, n in by_risk.items()},
                   "track_risk": {f"{t}/{r}": n for (t, r), n in sorted(Counter((i["track"], str(v.risk_of(i))) for i in final_items).items())}},
        "quota_check": "통과 (validate.check_quota)",
        "similarity_check": similarity_summary(final_items, v.existing_items()),
        "guideline_different_judgments": GUIDELINE_DIFFERENT,
        "limits": [
            "글자 유사도는 같은 상황을 말만 바꾼 문항을 잡지 못한다. 같은 기준서 경계 규칙(P-B6 변형 등)을 쓰는 문항 사이의 상황 중복은 검토자 눈으로만 확인했다.",
            "문항은 Claude가 썼고 정답 라벨은 같은 검토자가 승인했다. 외부 QA 판정이 아니므로 라벨 오류 가능성이 남는다.",
            "검토 화면에서 정답 라벨 초안을 보여 주었으므로 검토자가 초안에 끌렸을 가능성(앵커링)이 있다.",
            "X-3 문항에 P-B5 규칙은 쓰지 않았다.",
            "NA16·NA18·NB11의 정답은 기준서 v1.5와 다른 검토자 판단을 따른다(v1.6 후보 V16-2).",
            "NA05 근거 문구의 H-1b '정보가 부족해도 Critical' 서술은 초안 시점 문구다. 검토자가 재현 정보를 충분으로 고쳤고 라벨이 우선한다(승인된 문항이라 근거 문구는 고치지 않았다).",
            "라벨 오류가 발견되어도 이 파일은 고치지 않고 새 버전의 계획으로 넘긴다.",
        ],
    }
    SEALED.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"봉인: {FINAL.name} sha256={record['dataset']['sha256'][:16]}… ({len(final_items)}건), 기록: {SEALED.name}")
    return record


def verify() -> list[str]:
    """봉인 뒤 정합성 확인 — 문제 목록(비면 정상). 9단계 도구와 정합성 테스트가 쓴다."""
    p = []
    if not FINAL.exists() or not SEALED.exists():
        return ["봉인 파일이 없습니다"]
    rec = load(SEALED)
    if sha256_of(FINAL) != rec["dataset"]["sha256"]:
        p.append("평가셋 파일의 SHA-256 이 봉인 기록과 다릅니다")
    if sha256_of(ANSWERS) != rec["review_record"]["sha256"]:
        p.append("승인 기록 파일의 SHA-256 이 봉인 기록과 다릅니다")
    doc = load(FINAL)
    if not str(doc.get("status", "")).startswith("sealed"):
        p.append("평가셋 상태가 sealed 가 아닙니다")
    items = doc["items"]
    if len(items) != rec["dataset"]["items"]:
        p.append("문항 수가 봉인 기록과 다릅니다")
    q, _ = v.validate(items, v.existing_items(), full=True, review="approved")
    return p + q


def main(argv=None):
    ap = argparse.ArgumentParser(description="v1.5 새 최종 평가용 세트 봉인")
    ap.add_argument("--dry-run", action="store_true", help="검증과 승인 확인만 하고 파일은 쓰지 않는다")
    ap.add_argument("--verify", action="store_true", help="봉인된 파일의 정합성만 확인한다")
    args = ap.parse_args(argv)
    if args.verify:
        probs = verify()
        for q in probs:
            print("문제:", q)
        print("정상" if not probs else "이상 있음")
        return 1 if probs else 0
    seal(args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
