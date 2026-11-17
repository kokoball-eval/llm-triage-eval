"""v1.5 새 최종 평가용 세트 초안 만들기 (계획: report/final_v15_plan.md §3 순서 2).

[설계 의도]
1. 문항은 부분 파일(items_*.py)로 나눠 쓰고, 이 도구가 하나로 합쳐 번호순으로 저장한다.
2. 검증을 통과하지 못하면 파일을 쓰지 않는다. 저장된 초안은 항상 할당표와 규칙을 만족한다.
3. 이 단계의 산출물은 초안(draft)이다. 봉인은 검토자 승인 뒤 별도 도구로 한다(계획 §3 순서 4).
4. 모델을 호출하지 않고 모델 응답 기록을 읽지 않는다.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import validate as v  # noqa: E402
from helpers import KNOWN_BASE, UPDATE_NOTE  # noqa: E402

OUT = v.ROOT / "data" / "eval_v15_final" / "draft_v15_final.json"
PARTS = ["items_b", "items_a_risk", "items_a_x3", "items_a_misc"]


def collect():
    import importlib
    items = []
    for name in PARTS:
        items += list(importlib.import_module(name).ITEMS)
    return sorted(items, key=lambda i: i["id"])


def build():
    items = collect()
    problems, notes = v.validate(items, v.existing_items(), full=True)
    doc = {
        "name": "Aether Raid v1.5 새 최종 평가용 세트 (초안)",
        "version": "v1.5-final-draft",
        "status": "draft — 검토자 승인 전, 어떤 모델에도 실행하지 않음",
        "plan": "report/final_v15_plan.md",
        "guideline": "docs/dataset/triage_guideline.md (v1.5)",
        "update_note": UPDATE_NOTE,
        "known_issues_base": KNOWN_BASE,
        "label_semantics": "labels의 각 필드는 허용 답 목록이며 첫 번째 값이 가장 바람직한 답",
        "quota": {"risk": {str(k): n for k, n in v.QUOTA_RISK.items()}, "total": len(items)},
        "items": items,
    }
    return doc, problems, notes


def main():
    doc, problems, notes = build()
    for n in notes:
        print("참고:", n)
    if problems:
        for q in problems:
            print("문제:", q)
        print("초안을 저장하지 않았습니다.")
        return 1
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"저장: {OUT} ({len(doc['items'])}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
