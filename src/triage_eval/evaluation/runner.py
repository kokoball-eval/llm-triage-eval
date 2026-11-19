"""평가셋 실행 — 평가셋의 문항을 트리아지 파이프라인에 넣고 응답을 실행 기록으로 저장한다.

사용법:
    uv run triage-run --seed 1 --strict-env                  # 개발용 63건 × 2회 (회귀 게이트 후보)
    uv run triage-run --seed 1 --dataset <평가셋.json> --split final --final   # 최종 평가용 — 한 번만

출력: config.RUNS_DIR/run_<분할>_<시각>.json (항상 새 파일). 채점은 triage-eval, 회귀 판정은 triage-gate.

[설계 의도]
1. 기본 대상은 개발용(dev)이다. 최종 평가용(final·test)은 --final 을 함께 줘야만 실행된다. 프롬프트를 고치면서
   최종 평가 점수를 보면 그 문항에 맞춰 고치게 되어 최종 수치를 믿을 수 없게 된다. 실수를 사람의 주의가 아니라 실행 조건으로 막는다.
2. 실행 전후로 모델을 메모리에서 내린다. 같은 seed라도 직전에 다른 프롬프트를 처리했으면 응답이 재현되지 않고,
   남은 모델은 다음 실행의 환경 점검에서 다른 프로그램의 VRAM 사용으로 잡힌다.
3. 워밍업 1회를 본 실행과 따로 기록한다. 첫 호출에는 모델 로드 시간이 섞여 지연 통계를 왜곡한다.
4. seed 를 주면 1회차는 seed N, 2회차는 N+1 로 고정한다. 같은 구성·같은 문항 순서·실행 도중 재로드 없음이면
   날짜가 달라도 같은 응답이 나온다. 회귀 게이트는 seed 고정 실행만 판정한다.
5. 실행 조건(run_config)에 평가셋 지문·문항·옵션·모델 digest·처리 구성 지문·측정 환경을 남긴다.
   회귀 게이트는 이 값으로 기준선과 비교할 수 있는 실행인지 확인한다.
6. 입력 토큰 수와 출력 상한을 함께 본다. 입력+출력 상한이 num_ctx 를 넘을 수 있거나 출력이 num_predict 에서 잘린 회차를
   세어 알린다. 잘린 응답은 형식 채점에서 칸 누락으로 드러난다.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from triage_eval import llm
from triage_eval.config import DEV_INPUTS, MODEL, OPTIONS, REPEAT, RUNS_DIR, THINK, rel
from triage_eval.evaluation.environment import snapshot
from triage_eval.evaluation.gate import dataset_sha256
from triage_eval.triage.pipeline import run_config, triage
from triage_eval.triage.prompt import SYSTEM_PROMPT


def select_items(dataset: dict, split: str) -> list[dict]:
    items = dataset["items"]
    return items if split == "all" else [i for i in items if i["split"] == split]


def blocked_split(split: str, final: bool) -> str | None:
    """설계 의도 1 — 막을 때는 이유를 돌려준다."""
    if split in ("test", "final", "all") and not final:
        return ("최종 평가용 문항은 최종 측정 때만 실행합니다. 프롬프트 개선은 --split dev 로 하고, "
                "최종 측정이면 --final 을 함께 주세요.")
    return None


def options_for(seed: int | None, run_idx: int) -> dict:
    return dict(OPTIONS) if seed is None else {**OPTIONS, "seed": seed + run_idx - 1}


def run(client, dataset: dict, split: str, seed: int | None, repeat: int, environment: dict) -> dict:
    """문항 × 반복 횟수만큼 처리하고 실행 기록(dict)을 돌려준다."""
    items = select_items(dataset, split)
    config = {
        "contract": "v2", "options": OPTIONS, "repeat_count": repeat, "split": split,
        "set": "all", **run_config(), "think": THINK,
        "dataset_sha256": dataset_sha256(dataset), "item_ids": [i["id"] for i in items],
        "model_digests": llm.model_digests(client, [MODEL]),
        "seeds": None if seed is None else {str(i): seed + i - 1 for i in range(1, repeat + 1)},
        "environment": environment,
    }
    llm.unload(client, MODEL)   # 설계 의도 2
    warmup = llm.generate(client, MODEL, SYSTEM_PROMPT + "\n[리포트]\n워밍업 입력", options_for(seed, 1))
    print(f"[{MODEL}] 워밍업 {warmup['elapsed_sec']}s (로드 {warmup['load_duration_sec']}s)")
    results = []
    for run_idx in range(1, repeat + 1):
        opts = options_for(seed, run_idx)
        for n, item in enumerate(items, start=1):
            res = triage(client, item, opts)
            results.append({
                "eval_id": f"{MODEL.replace(':', '_')}_{item['id']}_run{run_idx}",
                "model": MODEL, "run_index": run_idx, "item_id": item["id"],
                "set": item["set"], "track": item["track"], "split": item["split"],
                "seed": opts.get("seed"), "timestamp": datetime.now().isoformat(),
                "hit_num_predict": res.get("eval_count", 0) >= OPTIONS["num_predict"],
                "context_near_limit": (res.get("prompt_eval_count") or 0) + OPTIONS["num_predict"] > OPTIONS["num_ctx"],
                **res,
            })
            status = "성공" if res["success"] else f"실패 - {res['error_message']}"
            print(f"[Run {run_idx}] ({n}/{len(items)}) {item['id']}: {status} ({res['elapsed_sec']}s)")
    llm.unload(client, MODEL)   # 설계 의도 2
    return {
        "metadata": {"project": dataset["name"], "dataset_version": dataset["version"],
                     "executed_at": datetime.now().isoformat(), "models": [MODEL],
                     "total_formal_runs": len(results), "warmup_runs": 1, "run_config": config},
        "warmup_runs": [{"model": MODEL, "is_warmup": True, **warmup}],
        "results": results,
    }


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="평가셋 실행")
    p.add_argument("--dataset", type=Path, default=DEV_INPUTS, help="평가셋 파일 (기본: 개발용 63건)")
    p.add_argument("--split", choices=["dev", "test", "final", "all"], default="dev")
    p.add_argument("--final", action="store_true", help="최종 평가용 문항 실행 허용 — 최종 측정 때만")
    p.add_argument("--seed", type=int, default=None, help="seed 고정 (1회차 N, 2회차 N+1)")
    p.add_argument("--repeat", type=int, default=REPEAT)
    p.add_argument("--strict-env", action="store_true", help="측정 환경 경고가 있으면 실행하지 않음")
    p.add_argument("--out-dir", type=Path, default=RUNS_DIR)
    args = p.parse_args(argv)

    reason = blocked_split(args.split, args.final)
    if reason:
        print(reason)
        return 2
    if not args.dataset.is_file():
        print(f"평가셋 파일이 없습니다: {args.dataset} (평가셋 파일 경로를 주세요. 기본값은 개발용 {rel(DEV_INPUTS)})")
        return 2
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    items = select_items(dataset, args.split)
    if not items:
        print(f"{rel(args.dataset)} 에 {args.split} 문항이 없어 실행하지 않습니다.")
        return 2
    client = llm.client()
    environment = snapshot(client)
    print("=== 측정 환경 점검 ===")
    for w in environment["warnings"]:
        print(f"  ⚠️  {w}")
    if environment["warnings"] and args.strict_env:
        print("--strict-env: 측정 환경 경고가 있어 실행하지 않습니다.")
        return 2
    if not environment["warnings"]:
        print("  ✅ 경고 없음")

    print(f"\n=== {MODEL} × {len(items)}문항({args.split}) × {args.repeat}회 ===")
    log = run(client, dataset, args.split, args.seed, args.repeat, environment)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    out = args.out_dir / f"run_{args.split}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    rs = log["results"]
    print(f"\n=== 완료: {len(rs)}회 → {rel(out)}")
    print(f"  형식 재요청 {sum(bool(r.get('retried')) for r in rs)}건 · Critical 확인 {sum(bool(r.get('critical_checked')) for r in rs)}건 · "
          f"폐기 확인 {sum(bool(r.get('discard_checked')) for r in rs)}건 · 안전장치 적용 {sum(bool(r['guardrail']['applied']) for r in rs)}건")
    if any(r["context_near_limit"] for r in rs):
        print("  ⚠️  입력+출력 상한이 num_ctx 를 넘을 수 있는 회차가 있습니다 — 프롬프트 길이를 확인하세요")
    if any(r["hit_num_predict"] for r in rs):
        print(f"  ⚠️  출력이 num_predict({OPTIONS['num_predict']}) 상한에 걸린 회차가 있습니다")
    print("채점: uv run triage-eval --log " + rel(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
