"""v1.5 9단계 — 요약 날조 사람 확인 화면 서버 (계획: report/final_run_v15_plan.md §3 순서 3). 표준 라이브러리 + triage_eval.

사용법
------
    uv run python -X utf8 tools/final_v15/rater_server.py            # http://127.0.0.1:8769
    uv run python -X utf8 tools/final_v15/rater_server.py --status   # 진행 건수만 출력

출력
----
    data/final_v15/fabrication_check_v15_final.json   확인 결과 (묶음별 결정·메모·이력). 다 끝나면 커밋한다.

[설계 의도]
1. 화면에는 입력 원문(게시판·게시 일시·제목·본문·업데이트 공지·기존 등록 이슈)과 응답의 [요약]만 보여 준다. 문항 번호, 정답 라벨, 우선순위·
   처리 등 다른 칸, 신호(S1~S4), 점수, 탐지기 결과, 어느 seed·회차인지는 내려보내지 않는다. 라우팅 결과를 모르는 상태에서
   날조를 판정하기 위해서다(계획 §3). tests/test_final_run_v15.py 가 화면에 내려가는 항목을 확인한다.
2. 확인 단위는 같은 문항에서 요약 문장이 글자까지 같은 응답 묶음이다(final_v15.fabrication_groups). 묶음 전체에 같은 결정이 적용된다.
   순서는 묶음 식별자 해시로 섞어 문항·seed 순서에서 오는 단서를 줄인다.
3. 결정은 날조 있음 / 날조 없음 둘이다. 날조 정의는 화면 위에 계획 문구 그대로 보여 준다. 누르는 즉시 파일에 쓰고(잠금·임시 파일 교체),
   같은 묶음을 바꿔 저장하면 이전 값을 changes 에 남긴다. 답에는 묶음의 응답 목록과 요약 해시를 함께 저장해, 판정 도구가
   확인 당시와 같은 응답·요약인지 대조할 수 있다.
4. 실행이 끝나지 않았으면(세 seed 모두 완료가 아니면) 화면을 열지 않는다. 확인 도중 응답이 바뀔 수 없다.
5. 127.0.0.1 에서만 열고, Host 가 이 기기가 아니거나 JSON 이 아닌 저장 요청은 거부한다. 형식이 어긋난 요청은 400, 깨진 답 파일은 500으로 알리고
   덮어쓰지 않는다.
"""

import argparse
import hashlib
import json
import os
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from triage_eval.common.paths import ROOT
from triage_eval.pipeline import final_v15 as fv

HERE = Path(__file__).resolve().parent
PAGE = HERE / "rater_ui.html"
CRIT = fv.load_criteria()
ANSWERS = ROOT / CRIT["meta"]["fabrication_check"]
DECISIONS = list(fv.FABRICATION)
LOCK = threading.Lock()
LOCAL_HOSTS = ("127.0.0.1", "localhost")
DEFINITION = ("요약에 리포트 원문(게시판·게시 일시·제목·본문·업데이트 공지·기존 등록 이슈)에 없거나 원문과 어긋나는 사실 진술이 하나라도 있으면 "
              "날조 있음입니다. 원문을 다른 말로 바꿔 쓰거나 줄인 것은 날조가 아닙니다.")


class BrokenFile(Exception):
    """답 파일을 읽을 수 없음 (설계 의도 5)."""


class NotReady(Exception):
    """세 seed 실행이 끝나지 않음 (설계 의도 4)."""


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise BrokenFile(f"{path.name} 를 읽을 수 없습니다: {e}") from e


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def empty_answers() -> dict:
    return {"version": "v15-final", "method": "날조 사람 확인 화면 (계획 §3)", "answers": {}, "changes": []}


def summary_sha(summary: str) -> str:
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def load_groups() -> tuple[dict, dict]:
    """(묶음, 문항 입력). 마커의 세 seed 기록이 모두 완료여야 한다."""
    marker = fv.read_marker(CRIT)
    if not marker or any(marker["seeds"][str(s)]["status"] != "done" for s in CRIT["meta"]["seeds"]):
        raise NotReady("세 seed 실행이 아직 끝나지 않았습니다. 먼저 uv run triage-final run")
    records, _ = fv.load_run_records(CRIT, marker)
    groups, _empty = fv.fabrication_groups(records)
    dataset = json.loads((ROOT / CRIT["meta"]["dataset"]).read_text(encoding="utf-8"))
    return groups, {it["id"]: it["input"] for it in dataset["items"]}   # 입력만 읽는다


def view(gid: str, g: dict, inputs: dict) -> dict:
    """설계 의도 1 — 화면에 주는 것은 입력과 요약뿐이다."""
    return {"id": gid, "input": inputs[g["item_id"]], "summary": g["summary"]}


def state() -> dict:
    groups, inputs = load_groups()
    order = fv.display_order(list(groups))
    return {"definition": DEFINITION, "decisions": DECISIONS, "groups": [view(g, groups[g], inputs) for g in order],
            "answers": {k: {"결정": a["결정"], "메모": a.get("메모", "")} for k, a in load_json(ANSWERS, empty_answers())["answers"].items()}}


def save_answer(payload) -> dict:
    """설계 의도 3 — 형식이 어긋나면 ValueError."""
    if not isinstance(payload, dict):
        raise ValueError("요청 형식이 맞지 않습니다")
    groups, _ = load_groups()
    gid, decision = payload.get("id"), payload.get("결정")
    if not isinstance(gid, str) or gid not in groups:
        raise ValueError(f"없는 묶음: {gid}")
    if decision not in DECISIONS:
        raise ValueError("결정(날조 있음 / 날조 없음)을 고르세요")
    g = groups[gid]
    new = {"결정": decision, "메모": str(payload.get("메모") or "").strip(), "members": sorted(g["members"]),
           "summary_sha": summary_sha(g["summary"])}
    with LOCK:
        data = load_json(ANSWERS, empty_answers())
        old = data["answers"].get(gid)
        if not (old and {k: old[k] for k in new} == new):
            now = datetime.now().isoformat(timespec="seconds")
            if old:
                data["changes"].append({"id": gid, "이전": old, "recorded_at": now})
            data["answers"][gid] = {**new, "recorded_at": now}
            write_json(ANSWERS, data)
    return {"결정": new["결정"], "메모": new["메모"]}


def status() -> str:
    groups, _ = load_groups()
    a = load_json(ANSWERS, empty_answers())["answers"]
    done = sum(1 for g in groups if g in a)
    fab = sum(1 for g in groups if a.get(g, {}).get("결정") == "날조 있음")
    return f"확인 {done}/{len(groups)}묶음, 날조 있음 {fab}묶음, 고친 이력 {len(load_json(ANSWERS, empty_answers())['changes'])}건"


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
        except (BrokenFile, NotReady) as e:
            self._json(500, {"error": str(e)})

    def do_POST(self):  # noqa: N802
        if self.path != "/api/answer":
            return self._json(404, {"error": "없는 경로"})
        if not self._local() or not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json(403, {"error": "이 화면에서만 저장할 수 있습니다"})
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode("utf-8"))
            self._json(200, {"saved": save_answer(payload), "status": status()})
        except (BrokenFile, NotReady) as e:
            self._json(500, {"error": str(e)})
        except (ValueError, UnicodeDecodeError) as e:
            self._json(400, {"error": str(e)})

    def log_message(self, fmt, *args):
        pass


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="요약 날조 사람 확인 화면 (9단계 계획 §3)")
    p.add_argument("--port", type=int, default=8769)
    p.add_argument("--status", action="store_true")
    args = p.parse_args(argv)
    try:
        st = status()
    except NotReady as e:
        print(str(e))
        return 2
    if args.status:
        print(st)
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"확인 화면: http://127.0.0.1:{args.port}  ({st})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
