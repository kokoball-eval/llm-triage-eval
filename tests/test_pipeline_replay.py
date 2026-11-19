"""한 건 처리 파이프라인 재생 시험 — 저장된 실제 실행 기록을 가짜 모델로 다시 돌린다 (모델은 호출하지 않는다).

[설계 의도]
1. 구조 정리(B단계) 전후로 파이프라인 동작이 같은지 확인하는 기준이다. 실행 기록의 attempts 에는 모델이 호출마다
   돌려준 원문이 순서대로 남아 있다. 가짜 모델이 그 원문을 같은 순서로 돌려주면, 파이프라인(프롬프트 → 칸 재시도 →
   M2 Critical 체크리스트 → 폐기 점검 → 안전장치 g2)은 기록과 같은 최종 응답을 내야 한다.
2. 최종 응답만 같고 모델에 보낸 내용이 달라지면 실제 모델에서는 결과가 바뀐다. 그래서 호출마다 보낸 프롬프트·옵션·
   스키마를 모두 이어 SHA-256 하나로 고정한다(PROMPTS_SHA256). 이 값은 구조 정리 전 코드(87c94a1)에서 계산했고,
   새 구조(triage/pipeline.py)에서도 같은 값이 나온다.
3. 대상은 채택 구성(qwen2.5:7b + M2)의 기록 전부다: 게이트 기준선 3건(v1.4 개발용 378응답)과 9단계 최종 실행
   3건(봉인 세트 360응답). 경로가 틀리면 파일을 못 읽어 실패한다(건너뛰지 않는다).
"""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from triage_eval.triage.pipeline import OPTIONS, triage

from triage_eval.config import BASELINE_DIR, DEV_INPUTS, FINAL_RUNS_DIR, FINAL_SET
SOURCES = [   # (실행 기록, 그 기록이 본 평가셋)
    *[(BASELINE_DIR / f"v14_dev_m2_seed{s}_baseline.json", DEV_INPUTS)
      for s in (1, 11, 21)],
    *[(FINAL_RUNS_DIR / f"v15final_final_m2_{t}.json", FINAL_SET)
      for t in ("20261117_194656", "20261117_195200", "20261117_195652")],
]
PROMPTS_SHA256 = "c4bdbe821395e295fb19329ac4e880f104d0a544ddaf328a5615cb3e3ae82971"


class ReplayClient:
    """기록된 응답을 순서대로 돌려주고, 받은 요청을 해시에 이어 붙인다."""

    def __init__(self, replies: list[str], digest):
        self.replies, self.digest = list(replies), digest

    def generate(self, model, prompt, options=None, think=None, format=None, **_):
        self.digest.update(json.dumps([model, prompt, options, think, format], ensure_ascii=False,
                                      sort_keys=True).encode("utf-8"))
        if not self.replies:
            raise AssertionError("기록보다 모델 호출이 많다")
        return {"response": self.replies.pop(0), "eval_count": 1, "eval_duration": 1}

    def ps(self):
        return {"models": []}


def _records():
    for log_path, ds_path in SOURCES:
        items = {i["id"]: i for i in json.loads(Path(ds_path).read_text(encoding="utf-8"))["items"]}
        for rec in json.loads(Path(log_path).read_text(encoding="utf-8"))["results"]:
            yield log_path, rec, items[rec["item_id"]]


def _replay(rec, item, digest):
    client = ReplayClient([a["response_text"] for a in rec["attempts"]], digest)
    res = triage(client, item, {**OPTIONS, "seed": rec["seed"]}, model=rec["model"])
    assert client.replies == [], "기록보다 모델 호출이 적다"
    return res


def test_replay_reproduces_every_recorded_final_answer():
    digest, n = hashlib.sha256(), 0
    for log_path, rec, item in _records():
        out = _replay(rec, item, digest)
        where = f"{log_path} {rec['eval_id']}"
        assert out["response_text"] == rec["response_text"], where
        assert out["raw_response_text"] == rec["raw_response_text"], where
        assert out["guardrail"] == rec["guardrail"], where
        for key in ("retried", "retry_adopted", "critical_checked", "critical_upgraded", "discard_checked", "discard_held"):
            assert out.get(key) == rec.get(key), f"{where} {key}"
        assert [a["kind"] for a in out["attempts"]] == [a["kind"] for a in rec["attempts"]], where
        n += 1
    assert n == 378 + 360
    assert digest.hexdigest() == PROMPTS_SHA256


def test_replay_notices_a_changed_reply():
    """재생 시험이 실제로 차이를 잡는지 — 첫 응답의 [요약]을 바꾸면 최종 응답이 달라진다."""
    _, rec, item = next(r for r in _records() if r[1]["attempts"][0]["kind"] == "first" and len(r[1]["attempts"]) == 1)
    changed = {**rec, "attempts": [{**rec["attempts"][0],
                                    "response_text": rec["attempts"][0]["response_text"].replace("[요약]:", "[요약]: 변경된")}]}
    out = _replay(changed, item, hashlib.sha256())
    assert out["response_text"] != rec["response_text"]


def test_replay_fails_when_a_source_file_is_missing(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "SOURCES", [(BASELINE_DIR / "없는_기록.json", SOURCES[0][1])])
    with pytest.raises(FileNotFoundError):
        list(_records())
