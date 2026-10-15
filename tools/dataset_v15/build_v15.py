"""v1.5 평가셋 생성 — v1.4 평가셋에 라벨 일치율 불일치 검토의 라벨 변경을 적용한다.

사용: uv run python tools/dataset_v15/build_v15.py
입력: data/eval_v14/aether_raid_v14.json, data/eval_v15/label_changes_v15.json
출력: data/eval_v15/aether_raid_v15.json

설계 메모
- v1.4 평가셋 파일은 건드리지 않는다. v1.4 실행 기록·보고서·게이트·라벨 일치율 계산(번호 대응표의 평가셋 지문)이
  그 파일을 기준으로 재현되어야 하기 때문이다. v1.5는 새 파일을 만든다.
- 바꾸는 내용은 모두 변경표(label_changes_v15.json)에 있다. 변경마다 v1.4 값(old)을 함께 적고, 생성할 때 v1.4 파일과
  대조한다. 다르면 파일을 쓰지 않는다. 엉뚱한 문항이나 이미 바뀐 값을 고치는 실수를 막는다.
- 바꾸는 범위는 개발용(dev) 63건뿐이다. 최종 평가용(final) 25건은 v1.4 최종 평가에서 한 번 사용했으므로 그대로 두고,
  v1.5 최종 평가는 새로 봉인할 세트로 한다. 변경표에 final 문항이 있으면 오류다.
- 허용 답은 v1.4 목록 뒤에 덧붙이는 것이 원칙이다(첫 정답 유지). 첫 정답을 바꾼 문항(라벨 변경)은 변경표의 new가 그대로 들어간다.
- 검증은 v1.4 생성기의 validate를 그대로 쓰고, 모듈 하나만 넓힌다: 모듈이 없는 문항(해당 없음)에 다른 모듈을 허용할 때는
  허용 답을 ["해당 없음", 모듈...]로 적는다. 채점 함수(score.field_correct)는 허용 답이 비어 있을 때만 "해당 없음"을
  따로 처리하므로, 목록에 "해당 없음"을 명시해야 기존 정답이 계속 정답으로 남는다. 이때 "해당 없음"은 첫 정답이어야 한다.
- Critical 측정 문항(X-1)은 Critical·긴급 사인 요청만 정답이어야 한다. Major나 다른 처리를 허용한 문항을 X-1로 두면
  "칸은 정답인데 Critical 누락"이 동시에 잡혀 두 지표가 서로 다른 말을 한다(기준서 v1.5 §7).
- 검증에 실패하면 파일을 쓰지 않고 종료 코드 1로 끝낸다.
"""

import copy
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tools" / "dataset_v14"))

import build_v14 as v14  # noqa: E402

V14 = ROOT / "data" / "eval_v14" / "aether_raid_v14.json"
CHANGES = ROOT / "data" / "eval_v15" / "label_changes_v15.json"
OUT = ROOT / "data" / "eval_v15" / "aether_raid_v15.json"
NO_MODULE = "해당 없음"


def validate(items):
    """v1.4 검증 + 모듈 "해당 없음" 명시 허용."""
    errors = [e for e in v14.validate(items) if not e.endswith(f"모듈 허용 밖 값 '{NO_MODULE}'")]
    for it in items:
        mods = it["labels"]["모듈"]
        if NO_MODULE in mods and mods[0] != NO_MODULE:
            errors.append(f"{it['id']}: 모듈 '{NO_MODULE}'은 첫 정답일 때만 다른 모듈과 함께 적을 수 있음")
        lb = it["labels"]
        if "X-1" in lb["risk"] and (set(lb["우선순위"]) != {"Critical"} or set(lb["처리"]) != {"긴급 사인 요청"}):
            errors.append(f"{it['id']}: X-1 측정 문항은 Critical·긴급 사인 요청만 정답이어야 함 (기준서 v1.5 §7)")
    return errors


def apply_changes(items, changes):
    """(바뀐 문항 ID 목록, 오류 목록). items를 그 자리에서 고친다."""
    by_id = {i["id"]: i for i in items}
    errors, touched = [], []
    for c in changes["labels"]:
        it = by_id.get(c["id"])
        if it is None:
            errors.append(f"{c['id']}: 평가셋에 없는 문항")
            continue
        if it["split"] != "dev":
            errors.append(f"{c['id']}: 개발용이 아닌 문항은 바꾸지 않음")
            continue
        if it["labels"][c["field"]] != c["old"]:
            errors.append(f"{c['id']} {c['field']}: v1.4 값 {it['labels'][c['field']]} ≠ 변경표 old {c['old']}")
            continue
        it["labels"][c["field"]] = list(c["new"])
        touched.append(c["id"])
    for c in changes["items"]:
        it = by_id.get(c["id"])
        if it is None or it["split"] != "dev":
            errors.append(f"{c['id']}: 개발용 문항이 아님")
            continue
        lb = it["labels"]
        if lb["risk"] != c["risk"]["old"]:
            errors.append(f"{c['id']} risk: v1.4 값 {lb['risk']} ≠ 변경표 old {c['risk']['old']}")
            continue
        if c.get("human_review") and lb["human_review"] != c["human_review"]["old"]:
            errors.append(f"{c['id']} human_review: v1.4 값이 변경표와 다름")
            continue
        lb["risk"] = list(c["risk"]["new"])
        if c.get("human_review"):
            lb["human_review"] = c["human_review"]["new"]
        if "rationale" in c:
            it["rationale"] = c["rationale"]
        if "rationale_append" in c:
            it["rationale"] = it["rationale"] + c["rationale_append"]
        touched.append(c["id"])
    for it in items:
        if it["id"] in touched:
            it["review_v15"] = "v1.5 라벨 일치율 불일치 검토 반영"
    return sorted(set(touched)), errors


def build():
    """(데이터, 오류 목록)을 돌려주고 파일은 쓰지 않는다."""
    v14_data = json.loads(V14.read_text(encoding="utf-8"))
    changes = json.loads(CHANGES.read_text(encoding="utf-8"))
    items = copy.deepcopy(v14_data["items"])
    touched, errors = apply_changes(items, changes)
    if errors:
        return None, errors
    errors = validate(items)
    if errors:
        return None, errors
    data = {
        "name": v14_data["name"],
        "version": "v1.5",
        "created": "2026-10-14",
        "guideline": "docs/dataset/triage_guideline.md (v1.5)",
        "review_log": "docs/dataset/review_log.md",
        "label_semantics": v14_data["label_semantics"],
        "split_policy": "dev = v1.4 개발용 63건(v1.5 라벨 일치율 불일치 검토 반영), "
                        "final = v1.4 최종 평가용 25건 그대로(v1.4에서 사용 완료, v1.5 최종 평가는 새 봉인 세트로 한다)",
        "label_policy": v14_data["label_policy"] + "; v1.5 변경은 data/eval_v15/label_changes_v15.json",
        "label_relaxed_ids": v14_data["label_relaxed_ids"],
        "changed_ids_v15": touched,
        "counts": {"total": len(items), **Counter(i["split"] for i in items)},
        "split_summary": v14.summary(items),
        "items": items,
    }
    return data, []


def main():
    data, errors = build()
    if errors:
        print("검증 실패 — 파일을 쓰지 않습니다")
        for e in errors:
            print("  -", e)
        sys.exit(1)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(v14.dump(data), encoding="utf-8")
    print(f"{data['counts']['total']}건 생성·검증 완료 (바뀐 문항 {len(data['changed_ids_v15'])}개): {OUT.relative_to(ROOT)}")
    for r in data["split_summary"]:
        print("  ", r)


if __name__ == "__main__":
    main()
