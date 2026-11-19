"""모델 호출 입구 — Ollama 를 부르는 코드는 이 파일에만 있다.

[설계 의도]
1. 호출이 한 곳에 모여 있어야 모델 서버를 바꾸거나(다른 로컬 런타임, 사내 API) 시험에서 가짜 모델로 바꿔 끼우기 쉽다.
   함수는 client 를 인자로 받으므로 generate(model, prompt, options, think, format, keep_alive)·ps()·list() 가 있는 객체면
   무엇이든 된다. list() 가 없으면 모델 digest 가 비어 기록되고, 회귀 게이트는 기준선과 digest 가 달라 판정하지 않는다.
2. 호출 한 번의 실패로 실행 전체가 멈추지 않게, 예외는 실패 기록(success=False)으로 바꿔 돌려준다.
3. 응답과 함께 지연·입력 토큰 수·모델 로드 시간·VRAM 을 남긴다. 입력 토큰 수는 num_ctx 를 넘는지 확인하는 데,
   로드 시간은 실행 도중 모델이 다시 로드되어 측정이 오염됐는지 확인하는 데 쓴다(evaluation/gate.py).
4. schema 를 주면 응답을 그 JSON 스키마로 제한한다(Ollama 구조화 출력). 첫 응답에는 쓰지 않고 재요청·확인 질문에만 쓴다.
"""

import hashlib
import time

import ollama

from triage_eval.config import THINK


def client() -> ollama.Client:
    return ollama.Client()


def generate(client, model: str, prompt: str, options: dict, schema: dict | None = None) -> dict:
    """완성된 프롬프트로 1회 생성한다."""
    start = time.perf_counter()
    try:
        extra = {"format": schema} if schema else {}
        r = client.generate(model=model, prompt=prompt, options=options, think=THINK, **extra)
        eval_ns = r.get("eval_duration", 0)
        return {
            "success": True,
            "response_text": r.get("response", "").strip(),
            "elapsed_sec": round(time.perf_counter() - start, 3),
            "load_duration_sec": round(r.get("load_duration", 0) / 1e9, 3),
            "prompt_eval_count": r.get("prompt_eval_count"),
            "eval_count": r.get("eval_count", 0),
            "tokens_per_sec": round(r.get("eval_count", 0) / (eval_ns / 1e9), 2) if eval_ns > 0 else None,
            "vram_mib": vram_mib(client, model),
            "error_message": None,
        }
    except Exception as e:  # 설계 의도 2
        return {
            "success": False, "response_text": "", "elapsed_sec": round(time.perf_counter() - start, 3),
            "load_duration_sec": 0.0, "prompt_eval_count": None, "eval_count": 0, "tokens_per_sec": None,
            "vram_mib": vram_mib(client, model), "error_message": str(e),
        }


def unload(client, model: str) -> None:
    """모델을 메모리에서 내린다(keep_alive=0). 같은 seed라도 직전에 다른 프롬프트를 처리했으면 응답이 재현되지 않아서
    실행 전후에 내린다. 내릴 모델이 없거나 실패해도 계속한다."""
    try:
        client.generate(model=model, prompt="", keep_alive=0)
    except Exception:
        pass


def vram_mib(client, model: str) -> float:
    """호출 직후 ollama ps 에서 그 모델의 VRAM 점유량(MiB)."""
    try:
        for m in client.ps().get("models", []):
            name = m.get("name", "")
            if name == model or name.startswith(model.split(":")[0]):
                return round(m.get("size_vram", 0) / (1024 * 1024), 2)
    except Exception:
        pass
    return 0.0


def model_digests(client, models: list[str]) -> dict:
    """ollama list 의 모델별 digest 앞 12자리. 같은 태그라도 다시 받으면 바뀔 수 있어 실행 기록에 남긴다."""
    digests = {}
    try:
        for m in client.list().get("models", []):
            name = m.get("model") or m.get("name", "")
            if name in models:
                digests[name] = (m.get("digest") or "")[:12] or None
    except Exception:
        pass
    return {model: digests.get(model) for model in models}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
