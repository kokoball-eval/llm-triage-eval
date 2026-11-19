"""설정 — 모델·생성 옵션·파일 위치를 이 파일 한 곳에서만 정한다.

[설계 의도]
1. 값이 여러 파일에 흩어져 있으면 한쪽만 바뀌어 실행 조건과 기록이 어긋난다. 다른 모듈은 이 파일에서 읽기만 한다.
2. 저장소 루트는 이 파일의 위치에서 한 번만 계산한다(<루트>/src/triage_eval/config.py → parents[2]).
3. 생성 옵션의 근거: num_ctx 8192는 판정 규칙이 든 프롬프트(약 3,000자)와 출력이 4,096 토큰 기본값을 넘을 수 있어서다.
   Ollama는 넘친 앞부분을 조용히 잘라 내므로, 잘린 채 실행되면 "규칙을 못 지킨 모델"로 잘못 측정된다.
   num_predict 512는 8칸 출력이 잘리지 않는 길이다. temperature 0.2와 seed 고정으로 같은 입력에 같은 응답을 얻는다.
4. think=False — 생각 과정을 먼저 출력하는 모델은 8칸 형식과 지연이 모두 달라진다. qwen2.5:7b 는 이 인자를 거부하지 않는다.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# ── 모델 ──────────────────────────────────────────────
MODEL = "qwen2.5:7b"
THINK = False
OPTIONS = {"temperature": 0.2, "num_predict": 512, "num_ctx": 8192}
OUTPUT_LANG = "ko"          # 응답 언어 — BTS를 쓰는 팀이 읽는 언어 (contracts.py R7)

# ── 평가 ──────────────────────────────────────────────
REPEAT = 2                  # seed 하나당 반복 횟수 (1회차 seed N, 2회차 N+1)
DEV_INPUTS = ROOT / "data" / "eval_v14" / "aether_raid_v14.json"     # 개발용 63건 — 모델이 보는 입력
DEV_LABELS = ROOT / "data" / "eval_v15" / "aether_raid_v15.json"     # 같은 63건의 정답표(기준서 v1.5)
GATE_CRITERIA = ROOT / "gate_criteria_v15.toml"
RUNS_DIR = ROOT / "data" / "results" / "runs"                          # triage-run 실행 기록
GATE_DIR = ROOT / "data" / "results" / "gate"                          # triage-gate 판정 결과


def rel(path: Path) -> str:
    """저장소 안이면 루트 기준 상대 경로, 밖이면(시험 임시 폴더 등) 그대로."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)
