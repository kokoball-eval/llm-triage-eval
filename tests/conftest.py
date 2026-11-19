"""pytest 공통 준비물.

- src/ 는 pyproject.toml 의 [tool.pytest.ini_options] pythonpath 설정으로 import 경로에 들어간다.
- 시험은 저장소의 실제 기록을 "읽기만" 한다. 파일이 필요하면 pytest 가 주는 임시 폴더(tmp_path)에 쓴다.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEV = json.loads((ROOT / "data/eval_v14/aether_raid_v14.json").read_text(encoding="utf-8"))
NO_ALL = {f"q{i}": "아니오" for i in range(1, 7)}


class FakeClient:
    """정답 라벨로 응답하는 가짜 모델. 체크리스트 질문에는 answers 를 JSON으로 돌려준다."""

    def __init__(self, answers=None, override=None, answers_for=None):
        self.answers = answers or NO_ALL
        self.answers_for = answers_for or {}   # 문항별 체크리스트 답 (없으면 answers)
        self.override = override or {}
        self.prompts = []
        self.by_title = {i["input"]["title"]: i for i in DEV["items"] if i["split"] == "dev"}

    def generate(self, model, prompt, options=None, think=None, keep_alive=None, format=None):
        if keep_alive == 0:
            return {}
        self.prompts.append(prompt)
        report = prompt.rsplit("\n[리포트]\n", 1)[1]
        item = next((it for t, it in self.by_title.items() if f"[제목] {t}\n" in report), None)
        if "[Critical 확인 질문]" in prompt:
            ans = self.answers_for.get(item["id"], self.answers) if item else self.answers
            return {"response": json.dumps(ans, ensure_ascii=False), "eval_count": 10, "eval_duration": 10**8,
                    "load_duration": 0, "prompt_eval_count": 3000}
        if item is None:
            text = "[요약]: 워밍업"
        else:
            lb = item["labels"]
            v = {f: lb[f][0] if lb[f] else "해당 없음" for f in ["분류", "모듈", "우선순위", "재현 정보", "발생 빈도", "처리"]}
            v.update(self.override.get(item["id"], {}))
            text = "\n".join([f"[요약]: {item['input']['title']}"] + [f"[{k}]: {x}" for k, x in v.items()]
                             + ["[누락 정보 및 권장 조치]: " + (" / ".join(lb["must_request"] + lb["must_recommend"]) or "없음")])
        return {"response": text, "eval_count": 100, "eval_duration": 10**9, "load_duration": 0, "prompt_eval_count": 2000}

    def ps(self):
        return {"models": []}

    def list(self):
        return {"models": []}


@pytest.fixture
def write_json(tmp_path):
    """dict 를 임시 폴더의 JSON 파일로 저장하고 경로를 돌려주는 도우미."""
    def _write(name: str, payload: dict) -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path
    return _write
