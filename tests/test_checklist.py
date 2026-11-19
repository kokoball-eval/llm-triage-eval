"""Critical 체크리스트(triage/checklist.py)와 파이프라인 연결 시험 — 모델은 호출하지 않는다."""

import json
from pathlib import Path

import pytest

from conftest import FakeClient, NO_ALL
from triage_eval.contracts import parse_response
from triage_eval.triage import checklist as m
from triage_eval.triage.pipeline import triage

ROOT = Path(__file__).resolve().parent.parent
ITEMS = {i["id"]: i for i in json.loads((ROOT / "data/eval_v14/aether_raid_v14.json").read_text(encoding="utf-8"))["items"]}


# ── M2 Critical 체크리스트 ───────────────────────────────────

BASE = {"분류": "결함", "우선순위": "Major", "재현 정보": "충분"}


def test_checklist_has_six_questions_and_strict_schema():
    assert len(m.CHECKLIST) == 6 and m.SCHEMA["required"] == m.KEYS
    assert m.SCHEMA["properties"]["q6"]["enum"] == ["서버 장애", "민감한 텍스트", "아니오"]


@pytest.mark.parametrize("pred,answers,expected", [
    (BASE, {**NO_ALL, "q4": "예"}, True),                                   # 반복 악용 → 올림
    (BASE, NO_ALL, False),                                                  # 모두 아니오
    (BASE, {**NO_ALL, "q6": "민감한 텍스트"}, True),
    ({**BASE, "재현 정보": "부족"}, {**NO_ALL, "q1": "예"}, False),          # H-3a — 정보 부족 단문은 올리지 않음
    ({**BASE, "재현 정보": "부족"}, {**NO_ALL, "q6": "서버 장애"}, True),    # H-1b 예외
    ({**BASE, "재현 정보": "부족"}, {**NO_ALL, "q6": "민감한 텍스트"}, False),
    ({**BASE, "우선순위": "Critical"}, NO_ALL, False),                       # 내리지 않음
    ({**BASE, "우선순위": "중간"}, {**NO_ALL, "q2": "예"}, False),            # 허용 값 밖은 덮지 않음
    (BASE, {}, False),                                                      # 답을 읽지 못함
])
def test_decide_upgrade(pred, answers, expected):
    assert m.decide_upgrade(pred, answers)[0] is expected


def test_should_ask_only_defects():
    assert m.should_ask({"분류": "결함"})
    assert not m.should_ask({"분류": "중복 의심"}) and not m.should_ask({"분류": "문의·건의"})


def test_apply_upgrade_changes_priority_line_only():
    text = "[요약]: x\n[분류]: 결함\n[모듈]: 결제·재화\n[우선순위]: Major\n[재현 정보]: 충분"
    out = m.apply_upgrade(text)
    assert parse_response(out)["우선순위"] == "Critical"
    assert out.replace("Critical", "Major") == text


def test_assets_fingerprint_matches_baseline_runs():
    """기준선 실행 기록에 남은 방법 구성 지문과 같아야 회귀 게이트가 "방법 변경"으로 오인하지 않는다 (checklist.py 설계 의도 4)."""
    log = json.loads((ROOT / "data/results/v14/baseline/v14_dev_m2_seed1_baseline.json").read_text(encoding="utf-8"))
    assert m.assets_sha256() == log["metadata"]["run_config"]["method_assets_sha256"]


# ── 파이프라인 연결 ───────────────────────────────────────────────


def test_critical_check_upgrades_and_records():
    item = ITEMS["S04b"]   # 정답 Critical — 첫 응답을 Major로 바꿔 놓고 체크리스트가 올리는지 본다
    client = FakeClient(answers={**NO_ALL, "q4": "예"}, override={"S04b": {"우선순위": "Major", "처리": "등록(재현 대기)"}})
    res = triage(client, item, model="fake")
    assert res["critical_checked"] and res["critical_upgraded"]
    assert parse_response(res["raw_response_text"])["우선순위"] == "Critical"
    att = [a for a in res["attempts"] if a["kind"] == "critical_check"][0]
    assert att["answers"]["q4"] == "예" and att["upgraded"]
