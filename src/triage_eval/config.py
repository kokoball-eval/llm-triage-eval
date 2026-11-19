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
DATA = ROOT / "데이터"
SETS = DATA / "평가셋"
DEV_INPUTS = SETS / "개발용_63건_입력.json"      # 개발용 63건 — 모델이 보는 입력 (v1.4 평가셋)
DEV_LABELS = SETS / "개발용_63건_정답표.json"    # 같은 63건의 정답표 (기준서 v1.5)
FINAL_SET = SETS / "최종용_60건.json"            # 최종 평가용 60건 — 이미 한 번 사용함, 성능표 근거로만 쓴다
TEST_V13 = SETS / "시험용_v13.json"              # v1.3 평가셋 — 안전장치·형식 규칙 시험만 읽는다
GATE_CRITERIA = ROOT / "기준" / "회귀_게이트_기준.toml"

LOGS = DATA / "실행기록"
BASELINE_DIR = LOGS / "기준선"                   # 게이트 기준선 (seed 1·11·21)
FINAL_RUNS_DIR = LOGS / "최종용"                 # 최종 평가용 세트 실행 360응답
REFERENCE_DIR = DATA / "기준결과"                # 구조 정리 전 도구가 낸 판정·채점 (재현 시험의 정답)
RUNS_DIR = DATA / "새실행"                       # triage-run 실행 기록
GATE_DIR = DATA / "게이트결과"                   # triage-gate 판정 결과


def rel(path: Path) -> str:
    """저장소 안이면 루트 기준 상대 경로, 밖이면(시험 임시 폴더 등) 그대로."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)
