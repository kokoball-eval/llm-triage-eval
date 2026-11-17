"""v1.5 새 최종 평가용 세트 검증 도구(tools/dataset_v15_final/validate.py) 시험.

모델을 호출하지 않으므로 CI에서 자동 실행된다. 합성 문항으로 검사 규칙의 경계를 확인한다.
"""

import copy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "dataset_v15_final"))

import validate as v  # noqa: E402
from helpers import A, B, L  # noqa: E402


def x1(i="NA01", title="결제했는데 다이아가 안 들어옵니다"):
    return A(i, "명확", "2026-11-19 10:00", "자유게시판", title, "본문 " + title * 2,
             L(["결함"], ["Critical"], ["결제·재화"], ["충분"], ["긴급 사인 요청"], ["1회"], risk=["X-1"]), "C-1, 4.1 Critical")


def general(i="NA20", q="명확"):
    return A(i, q, "2026-11-19 10:00", "자유게시판", "상점 아이콘이 조금 어긋나 보입니다 " + i, "상점 화면 " + i * 5,
             L(["결함"], ["Minor"], ["UI·텍스트"], ["충분"], ["등록(재현 대기)"], ["항상"]), "C-1")


def test_valid_items_pass_item_check():
    assert v.check_item(x1()) == [] and v.check_item(general()) == []


def test_id_and_track_must_match():
    it = x1()
    it["track"] = "B"
    assert any("번호와 맞지" in p for p in v.check_item(it))
    assert v.check_item({"id": "F01"})[0].startswith("F01")


@pytest.mark.parametrize("field,value", [("분류", ["결함", "없는분류"]), ("우선순위", []), ("처리", ["보류"]), ("재현 정보", ["충분", "충분"])])
def test_label_vocabulary_and_duplicates_rejected(field, value):
    it = general()
    it["labels"][field] = value
    assert v.check_item(it)


def test_only_one_risk_per_item():
    it = x1()
    it["labels"]["risk"] = ["X-1", "X-3"]
    assert any("하나여야" in p for p in v.check_item(it))


def test_x1_requires_critical_only_and_urgent_action():
    it = x1()
    it["labels"]["우선순위"] = ["Critical", "Major"]
    assert any("X-1" in p for p in v.check_item(it))
    it = x1()
    it["labels"]["처리"] = ["등록(재현 대기)"]
    assert any("X-1" in p for p in v.check_item(it))


def test_x2_rules():
    it = general("NA09")
    it["labels"]["risk"] = ["X-2"]
    assert v.check_item(it) == []
    it["labels"]["우선순위"] = ["Critical"]
    assert any("X-2" in p for p in v.check_item(it))
    it = general("NA09")
    it["labels"].update(risk=["X-2"], 처리=["폐기"])
    assert any("X-2" in p for p in v.check_item(it))


def test_x3_requires_human_review_and_boundary_rule():
    it = general("NA13")
    it["labels"]["risk"] = ["X-3"]
    assert any("X-3" in p for p in v.check_item(it))
    it["labels"]["human_review"] = True
    assert any("X-3" in p for p in v.check_item(it))
    it["rationale"] = "P-B2(법적 파장), H-8"
    assert v.check_item(it) == []


def test_general_item_may_not_be_urgent():
    it = general()
    it["labels"]["처리"] = ["긴급 사인 요청"]
    assert any("일반 문항" in p for p in v.check_item(it))


def test_similarity_rejects_near_copies_and_reports_close_ones():
    a, b = general("NA20"), general("NA21")
    b["input"]["title"], b["input"]["body"] = a["input"]["title"], a["input"]["body"] + "."
    problems, _ = v.check_similarity([b], [a])
    assert problems and ("0.9" in problems[0] or "1.0" in problems[0])
    c = general("NA22")
    c["input"]["title"], c["input"]["body"] = "완전히 다른 제목 이것은 다른 글", "전혀 다른 내용입니다 길드 창고에 관한 이야기"
    assert v.check_similarity([c], [a]) == ([], [])
    # 새 문항끼리도 본다
    d = copy.deepcopy(a)
    d["id"] = "NA23"
    assert v.check_similarity([a, d], [])[0]


def full_set():
    """할당표를 정확히 만족하는 합성 세트(60건). 내용은 번호로 서로 달라지게 한다."""
    import string
    words = [f"{c}{d}" for c in string.ascii_lowercase for d in range(10)]

    def uniq(i, text):
        n = int(i[2:]) + (40 if i[1] == "B" else 0)
        return f"{text} {words[n]} {words[n * 2 + 3]} {words[n * 3 + 7]}" + "가나다라마바사아자차카타파하"[n % 14] * (n % 9 + 3)

    items = []
    mods = list(v.MODULES10)

    def A_(i, q, labels, rationale="C-1", extra=""):
        return A(i, q, "2026-11-19 10:00", "자유게시판", uniq(i, "제목" + extra), uniq(i, "본문 내용" + extra * 2), labels, rationale)

    def B_(i, q, labels, rationale="4.1"):
        return B(i, q, "내부 QA", uniq(i, "이슈"), "모바일", "10/10", uniq(i, "설명"), "정상", uniq(i, "실제"), "문서", labels, rationale)

    n = 0
    for k in range(1, 9):   # A X-1
        mod = ["결제·재화", "계정·로그인"][k % 2] if k <= 4 else mods[k % 10]
        items.append(A_(f"NA{k:02d}", "명확", L(["결함"], ["Critical"], [mod], ["충분"], ["긴급 사인 요청"], ["1회"], risk=["X-1"]),
                        "H-1b" if k <= 2 else "C-1"))
    for k in range(9, 13):  # A X-2
        items.append(A_(f"NA{k:02d}", "감정적", L(["결함"], ["Major"], [mods[k % 10]], ["충분"], ["등록(재현 대기)"], ["1회"], risk=["X-2"])))
    for k in range(13, 20):  # A X-3
        items.append(A_(f"NA{k:02d}", "경계", L(["결함"], ["Major"], [mods[k % 10]], ["충분"], ["등록(재현 대기)"], ["1회"],
                                              human_review=True, risk=["X-3"]), "P-B1"))
    gen_a = ["명확"] * 3 + ["단문"] * 4 + ["복합"] * 2 + ["중복"] * 3 + ["문의"] * 3 + ["무관"] * 6
    for k, q in zip(range(20, 41), gen_a):
        items.append(A_(f"NA{k:02d}", q, L(["결함"], ["Minor"], [mods[k % 10]], ["충분"], ["등록(재현 대기)"], ["항상"])))
    for k in range(1, 9):
        items.append(B_(f"NB{k:02d}", "명확", L(["결함"], ["Critical"], [mods[k % 10]], ["충분"], ["긴급 사인 요청"], ["항상"], risk=["X-1"])))
    for k in range(9, 12):
        items.append(B_(f"NB{k:02d}", "명확 (경계)", L(["결함"], ["Major"], [mods[k % 10]], ["충분"], ["등록(재현 대기)"], ["항상"],
                                                     human_review=True, risk=["X-3"]), "P-B3"))
    pri = ["Major"] * 3 + ["Minor"] * 3 + ["Trivial"]
    for k, p in zip(range(12, 19), pri):
        items.append(B_(f"NB{k:02d}", "명확", L(["결함"], [p], [mods[k % 10]], ["충분"], ["개발 배정"], ["항상"])))
    items.append(B_("NB19", "건의", L(["개선 제안"], ["Minor"], ["UI·텍스트"], ["충분"], ["개발 배정"], ["해당 없음"])))
    items.append(B_("NB20", "중복", L(["중복 의심"], ["Major"], ["네트워크"], ["충분"], ["CS 응대"], ["항상"])))
    return items


def test_full_set_passes_quota():
    items = full_set()
    assert len(items) == 60
    assert v.check_quota(items) == []
    for it in items:
        assert v.check_item(it) == [], it["id"]


@pytest.mark.parametrize("mutate,fragment", [
    (lambda its: its.pop(), "번호"),
    (lambda its: its[0]["labels"].update(risk=[]) or its[0]["labels"].update(우선순위=["Major"], 처리=["등록(재현 대기)"]), "위험 문항 수"),
    (lambda its: its[24].update(quality="복합"), "트랙 A 일반"),
    (lambda its: its[8].__setitem__("rationale", its[8]["rationale"]) or its[0].update(rationale="C-1"), "H-1b"),
])
def test_quota_violations_are_reported(mutate, fragment):
    items = full_set()
    mutate(items)
    assert any(fragment in p for p in v.check_quota(items)), v.check_quota(items)


def test_quota_requires_all_modules_and_account_payment_x1():
    items = full_set()
    for it in items:
        it["labels"]["모듈"] = ["UI·텍스트"]
    problems = v.check_quota(items)
    assert any("판정 모듈이 빠졌" in p for p in problems) and any("결제·재화 또는 계정·로그인" in p for p in problems)


def test_b_general_priority_split_is_checked():
    items = full_set()
    for it in items:
        if it["id"] == "NB12":
            it["labels"]["우선순위"] = ["Trivial"]
    assert any("Major 3·Minor 3·Trivial 1" in p for p in v.check_quota(items))


def test_existing_set_is_loaded_and_new_ids_do_not_collide():
    old = v.existing_items()
    assert len(old) == 88 and not any(v.ID_PATTERN.match(i["id"]) for i in old)


# ── 저장된 초안 ────────────────────────────────────────────────

DRAFT = ROOT / "data" / "eval_v15_final" / "draft_v15_final.json"


def test_committed_draft_matches_generator_and_passes_all_checks():
    """저장된 초안이 부분 파일을 합친 결과와 같고, 할당표·규칙·유사도 검사를 모두 통과한다."""
    import json

    import build_final_v15 as b
    doc, problems, _ = b.build()
    assert problems == [] and len(doc["items"]) == 60
    assert json.loads(DRAFT.read_text(encoding="utf-8")) == json.loads(json.dumps(doc, ensure_ascii=False))
    assert v.validate(doc["items"], v.existing_items(), full=True)[0] == []


def test_draft_has_no_model_output_fields_and_is_pending():
    """초안 문항에는 모델 응답이 없고 모두 검토 대기다(검토자 승인 전에는 모델에 넣지 않는다)."""
    import json
    doc = json.loads(DRAFT.read_text(encoding="utf-8"))
    assert doc["version"] == "v1.5-final-draft" and "초안" in doc["name"]
    for it in doc["items"]:
        assert it["review"] == "pending" and it["split"] == "final"
        assert set(it) == {"id", "set", "track", "split", "quality", "defect_types", "input", "labels", "rationale", "provenance", "review"}


# ── 검토에서 지적된 검사 구멍 ───────────────────────────────────

def test_x1_allows_only_urgent_action_as_answer():
    it = x1()
    it["labels"]["처리"] = ["긴급 사인 요청", "등록(재현 대기)"]
    assert any("X-1" in p for p in v.check_item(it))


def test_x3_answer_may_not_include_closing_actions():
    it = general("NA13")
    it["labels"].update(risk=["X-3"], human_review=True)
    it["rationale"] = "P-B3"
    assert v.check_item(it) == []
    for closing in ("개발 배정", "CS 응대", "폐기"):
        it["labels"]["처리"] = ["등록(재현 대기)", closing]
        assert any("사람 확인 없이" in p for p in v.check_item(it)), closing


def test_non_risk_item_may_not_have_critical_answer():
    it = general()
    it["labels"]["우선순위"] = ["Major", "Critical"]
    assert any("Critical" in p for p in v.check_item(it))


@pytest.mark.parametrize("posted", ["2026-11-18 23:59", "2026-11-24 00:00", "2026-11-19 25:00", "11-19 10:00", "2026-11-20"])
def test_track_a_posted_time_must_be_in_window(posted):
    it = general()
    it["input"]["posted_at"] = posted
    assert any("게시 시각" in p for p in v.check_item(it))
    it["input"]["posted_at"] = "2026-11-23 23:59"
    assert v.check_item(it) == []


def test_track_a_board_and_track_b_form_are_checked():
    it = general()
    it["input"]["board"] = "다른 게시판"
    assert any("게시판" in p for p in v.check_item(it))
    b = B("NB01", "명확", "내부 QA", "제목", "환경", "10/10", "설명", "기대", "실제", "문서",
          L(["결함"], ["Minor"], ["UI·텍스트"], ["충분"], ["개발 배정"], ["항상"]), "4.1")
    assert v.check_item(b) == []
    b["input"]["body"] = b["input"]["body"].replace("[기대 결과]", "")
    assert any("양식" in p for p in v.check_item(b))


def test_h1b_count_ignores_negations():
    items = full_set()
    for it in items:
        if it["labels"]["risk"] == ["X-1"]:
            it["rationale"] = "H-1b가 아니라 H-1"
    assert any("H-1b" in p for p in v.check_quota(items))
