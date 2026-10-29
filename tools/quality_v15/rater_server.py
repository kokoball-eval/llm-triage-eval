"""v1.5 요약 품질 — 검토자 채점 화면 서버 (계획 §4). 표준 라이브러리만 쓴다.

사용법
------
    uv run python tools/quality_v15/rater_server.py           # http://127.0.0.1:8765 (앱 브라우저 패널 quality-rater)
    uv run python tools/quality_v15/rater_server.py --status  # 진행 건수만 출력

출력
----
    data/quality_v15/rater_answers_v15.json     검토자 판정 (받은 순서·고친 이력 포함)
    data/quality_v15/practice_answers_v15.json  연습 문항 답 (지표에 쓰지 않음)

[설계 의도]
1. 측정 세트와 연습 문항 파일만 읽는다. 정답 대응표·개발용 자료는 읽지 않는다. 화면은 측정 세트의 원문 칸과 요약을 그대로
   보여 주므로, 사람이 원문을 옮겨 적는 단계가 없다(ISSUE-013 재발 방지).
2. 판정은 누르는 즉시 파일에 쓴다. 같은 번호를 다른 값으로 다시 저장하면 이전 값을 changes 에 남기고, 같은 값이면 남기지 않는다.
   저장은 한 번에 하나씩(잠금) 하고, 임시 파일에 쓴 뒤 바꿔 끼워 쓰는 도중 끊겨도 답 파일이 깨지지 않게 한다.
3. "있음"이면 유형(Q1~Q3)을 하나 이상 고르고, "없음"이면 유형을 비운다. 확신도는 정의를 붙인 세 단계다. 날조 측정에서
   확신도가 거의 모두 "높음"이었으므로, "다른 QA도 같게 볼 것 같은가"로 정의를 준다.
4. 연습 문항의 참고 답은 그 문항을 저장한 뒤에만 서버가 내려 준다(저장 전에는 화면 코드에도 없다). 연습 답은 다른 파일에 둔다.
5. 127.0.0.1 에서만 열고, Host가 이 기기가 아니거나 JSON이 아닌 저장 요청은 거부한다(다른 사이트가 같은 브라우저에서
   답을 써 넣지 못하게).
6. 형식이 어긋난 요청(객체가 아닌 JSON, 잘못된 값 형태)은 400으로 답하고 서버는 계속 돈다. 답 파일이 깨져 읽을 수 없으면
   500으로 알리고 덮어쓰지 않는다.
"""

import argparse
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from triage_eval.common.paths import ROOT

OUT_DIR = ROOT / "data" / "quality_v15"
MEASURE = OUT_DIR / "measure_set_v15.json"
PRACTICE = OUT_DIR / "practice_v15.json"
ANSWERS = OUT_DIR / "rater_answers_v15.json"
PRACTICE_ANSWERS = OUT_DIR / "practice_answers_v15.json"
PAGE = Path(__file__).resolve().parent / "rater_ui.html"
VERDICTS = ["품질 문제 없음", "품질 문제 있음"]
KINDS = ["Q1", "Q2", "Q3"]
CONFIDENCE = ["높음", "보통", "낮음"]
SHOWN = ["입력 종류", "게시판 / 작성자", "게시 일시", "제목", "본문", "참고: 업데이트 공지", "참고: 기존 등록 이슈", "요약"]
REFERENCE = ("참고 답", "참고 설명")
LOCK = threading.Lock()                                                 # 설계 의도 2
LOCAL_HOSTS = ("127.0.0.1", "localhost")


class BrokenFile(Exception):
    """답 파일을 읽을 수 없음 (설계 의도 6)."""


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BrokenFile(f"{path.name} 를 읽을 수 없습니다: {e}") from e


def write_json(path: Path, data: dict) -> None:
    """설계 의도 2 — 임시 파일에 쓴 뒤 바꿔 끼운다."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def empty_answers(source: Path) -> dict:
    return {"version": "v15", "source": source.relative_to(ROOT).as_posix(), "method": "채점 화면 (계획 §4)",
            "answers": {}, "changes": []}


def practice_view(item: dict, answered: bool) -> dict:
    """설계 의도 4 — 저장하지 않은 연습 문항에서는 참고 답을 뺀다."""
    return item if answered else {k: v for k, v in item.items() if k not in REFERENCE}


def state() -> dict:
    """설계 의도 1 — 화면에 주는 것: 측정 세트(원문 칸·요약만), 연습 문항, 지금까지의 답."""
    measure = load_json(MEASURE, {"items": []})["items"]
    practice = load_json(PRACTICE, {"items": []})["items"]
    p_answers = load_json(PRACTICE_ANSWERS, empty_answers(PRACTICE))["answers"]
    return {"measure": [{k: r.get(k, "") for k in ["번호", *SHOWN]} for r in measure],
            "practice": [practice_view(p, p["번호"] in p_answers) for p in practice],
            "answers": load_json(ANSWERS, empty_answers(MEASURE))["answers"],
            "practice_answers": p_answers}


def validate(payload, ids: set[str]) -> dict:
    """설계 의도 3·6 — 저장할 답. 형식이 어긋나면 ValueError."""
    if not isinstance(payload, dict):
        raise ValueError("요청 형식이 맞지 않습니다")
    qid = payload.get("번호")
    verdict, kinds, conf = payload.get("판정"), payload.get("유형", []), payload.get("확신도")
    if not isinstance(qid, str) or qid not in ids:
        raise ValueError(f"없는 번호: {qid}")
    if verdict not in VERDICTS:
        raise ValueError("판정(품질 문제 없음 / 있음)을 고르세요")
    if not isinstance(kinds, list) or not all(isinstance(k, str) and k in KINDS for k in kinds) or len(set(kinds)) != len(kinds):
        raise ValueError("유형은 Q1·Q2·Q3 중에서 고르세요")
    if verdict == VERDICTS[1] and not kinds:
        raise ValueError("품질 문제 있음이면 유형을 하나 이상 고르세요")
    if verdict == VERDICTS[0] and kinds:
        raise ValueError("품질 문제 없음이면 유형을 비워 두세요")
    if conf not in CONFIDENCE:
        raise ValueError("확신도(높음 / 보통 / 낮음)를 고르세요")
    return {"판정": verdict, "유형": sorted(kinds), "확신도": conf, "메모": str(payload.get("메모") or "").strip()}


def record(data: dict, qid: str, new: dict) -> dict:
    """설계 의도 2."""
    old = data["answers"].get(qid)
    if old and {k: old[k] for k in new} == new:
        return data
    now = datetime.now().isoformat(timespec="seconds")
    if old:
        data["changes"].append({"번호": qid, "이전": old, "recorded_at": now})
    data["answers"][qid] = {**new, "recorded_at": now}
    return data


def save_answer(payload) -> dict:
    """저장한 답. 연습 문항이면 참고 답을 함께 돌려준다(설계 의도 4)."""
    which = payload.get("세트") if isinstance(payload, dict) else None
    if which not in ("measure", "practice"):
        raise ValueError("세트는 measure 또는 practice")
    src, out = (MEASURE, ANSWERS) if which == "measure" else (PRACTICE, PRACTICE_ANSWERS)
    items = {r["번호"]: r for r in load_json(src, {"items": []})["items"]}
    new = validate(payload, set(items))
    with LOCK:
        data = record(load_json(out, empty_answers(src)), payload["번호"], new)
        write_json(out, data)
    saved = dict(data["answers"][payload["번호"]])
    if which == "practice":
        saved["reference"] = {k: items[payload["번호"]].get(k, "") for k in REFERENCE}
    return saved


def status() -> str:
    n = len(load_json(MEASURE, {"items": []})["items"])
    a = load_json(ANSWERS, empty_answers(MEASURE))
    p = load_json(PRACTICE_ANSWERS, empty_answers(PRACTICE))
    return f"연습 {len(p['answers'])}건, 측정 {len(a['answers'])}/{n}건, 고친 이력 {len(a['changes'])}건"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _local(self) -> bool:
        """설계 의도 5."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in LOCAL_HOSTS

    def do_GET(self):  # noqa: N802
        if not self._local():
            return self._json(403, {"error": "이 기기에서만 열 수 있습니다"})
        try:
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(200, state())
            else:
                self._json(404, {"error": "없는 경로"})
        except BrokenFile as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):  # noqa: N802
        if self.path != "/api/answer":
            return self._json(404, {"error": "없는 경로"})
        if not self._local() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(403, {"error": "이 화면에서만 저장할 수 있습니다"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            self._json(200, {"saved": save_answer(payload), "status": status()})
        except BrokenFile as e:
            self._json(500, {"error": str(e)})
        except (ValueError, UnicodeDecodeError) as e:      # JSONDecodeError 는 ValueError
            self._json(400, {"error": str(e)})

    def log_message(self, fmt, *args):  # 요청마다 찍지 않는다
        pass


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="요약 품질 검토자 채점 화면 (계획 §4)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--status", action="store_true")
    p.add_argument("--answers-dir", type=Path, help="답을 쓸 폴더 (화면 시험용. 기본은 data/quality_v15/)")
    args = p.parse_args(argv)
    if args.answers_dir:                                                   # 실제 답 파일을 건드리지 않고 화면을 시험한다
        global ANSWERS, PRACTICE_ANSWERS
        args.answers_dir.mkdir(parents=True, exist_ok=True)
        ANSWERS, PRACTICE_ANSWERS = args.answers_dir / ANSWERS.name, args.answers_dir / PRACTICE_ANSWERS.name
    if args.status:
        print(status())
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)       # 설계 의도 5
    print(f"채점 화면: http://127.0.0.1:{args.port}  ({status()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
