"""v1.5 새 최종 평가용 세트 — 검토자 검토 화면 서버 (계획: report/final_v15_plan.md §3 순서 3). 표준 라이브러리만 쓴다.

사용법
------
    uv run python tools/dataset_v15_final/review_server.py           # http://127.0.0.1:8767
    uv run python tools/dataset_v15_final/review_server.py --status  # 진행 건수만 출력

출력
----
    data/eval_v15_final/review_answers_v15_final.json   검토자 결정 (받은 순서·고친 이력 포함)

[설계 의도]
1. 초안 파일(draft_v15_final.json)과 검토 답 파일만 읽는다. 모델 응답·실행 기록·측정 결과는 읽지 않는다(계획 §2-5).
   화면은 문항 원문과 라벨 제안을 그대로 보여 주므로 사람이 옮겨 적는 단계가 없다(ISSUE-013 재발 방지).
2. 결정은 누르는 즉시 파일에 쓴다. 같은 번호를 다른 내용으로 다시 저장하면 이전 값을 changes 에 남기고, 같으면 남기지 않는다.
   저장은 한 번에 하나씩(잠금) 하고, 임시 파일에 쓴 뒤 바꿔 끼워 도중에 끊겨도 답 파일이 깨지지 않게 한다.
3. 결정은 승인 또는 수정 필요 둘이다. 수정 필요이면 고칠 칸과 맞는 값(또는 본문·교체 메모)을 적어야 한다. 승인이면 수정 내용을 비운다.
   라벨 값은 기준서 어휘 안에서만 받는다(검증 도구 validate.py 의 어휘와 같다).
4. 답에는 그 시점의 문항 해시를 함께 저장한다. 문항이 고쳐져 해시가 바뀌면 이전 승인은 효력이 없고 화면에 다시 검토할 문항으로 뜬다
   (계획 §3: 고친 문항만 재승인). 문항이 아직 고쳐지지 않은 수정 필요 건도 다시 검토 대상으로 남는다.
5. 작성 중 판단이 갈린 지점(FLAGS)은 해당 문항 화면에 보여 준다. 새로 정하는 결정이 필요한 곳(NA03)은 화면에서 명시적으로 묻는다.
6. 127.0.0.1 에서만 열고, Host가 이 기기가 아니거나 JSON이 아닌 저장 요청은 거부한다. 형식이 어긋난 요청은 400으로 답하고 서버는 계속 돈다.
   답 파일이 깨져 읽을 수 없으면 500으로 알리고 덮어쓰지 않는다.
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

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import validate as v  # noqa: E402

ROOT = v.ROOT
OUT_DIR = ROOT / "data" / "eval_v15_final"
DRAFT = OUT_DIR / "draft_v15_final.json"
ANSWERS = OUT_DIR / "review_answers_v15_final.json"
PAGE = HERE / "review_ui.html"
DECISIONS = ["승인", "수정 필요"]
LABEL_FIELDS = list(v.CATEGORIES)                      # 분류·우선순위·모듈·재현 정보·처리·발생 빈도
EDIT_FIELDS = LABEL_FIELDS + ["위험", "사람 검토", "본문", "교체"]
LOCK = threading.Lock()                                # 설계 의도 2
LOCAL_HOSTS = ("127.0.0.1", "localhost")

# 설계 의도 5 — 작성 중 판단이 갈린 지점 (report/final_v15_draft_notes.md 와 같은 내용을 문항 화면에 붙인다)
FLAGS = {
    "NA03": ["**결정 필요:** 단일 유저 1회 사건인 '타인 계정 열림'을 Critical 단일 정답(X-1)으로 둘지. 기준서 F-2는 1회 사건을 Major~Minor까지 열고 예외로 결제·재화·데이터 소실·다수 유저 서버 장애를 든다. 타인 계정 노출은 예외 목록에 없고 §4.1 Critical의 '유저 데이터에 치명적'에 근거한다. 이견이 있으면 '수정 필요'로 표시하고 메모에 적는다(이 문항은 X-1에서 빠지고 교체한다)."],
    "NA04": ["재현 정보를 '부족'으로 달았다(기기 정보 없음, R-4). R-2로 읽으면 '충분'도 가능하다."],
    "NA05": ["재현 정보를 '부족'으로 달았다(기기 정보 없음, R-4). R-2로 읽으면 '충분'도 가능하다."],
    "NA06": ["재실행 후 보상이 들어와 우회처럼 보이나 §4.1이 우회 가능해도 같다고 해 Critical 단일."],
    "NA08": ["점유율이 낮은 기기가 아니라 P-B3 대상이 아닌 일반 크래시로 보아 Critical 단일."],
    "NA12": ["빈도는 항상을 첫 정답, 오늘 아침 한정 서술이라 간헐적도 허용했다."],
    "NA14": ["욕설로 읽히는 오타를 '시발의 탑'이라고 직접 적었다. 공개 저장소 문체로 괜찮은지 확인이 필요하다."],
    "NA16": ["제재 요청이 아니라 지형 수정 요청이라 C-8이 아닌 P-B4로 보았다. 모듈 첫 값은 게임플레이·밸런스, 보안·어뷰징 허용."],
    "NA19": ["이벤트 종료 시점이 글에 없어 재현 정보·처리를 두 값 모두 허용했다."],
    "NA15": ["'길드원 2명도 똑같다'는 언급이 있으나 기기 한정 현상이라 H-1b가 아니라 P-B3."],
    "NA27": ["첫 정답 Major. 우회 가능 여부로 Minor 여지가 있고 판단보류도 허용했다."],
    "NA28": ["창모드 전환 두 현상은 같은 상황으로 하나, 힐러 건은 다른 상황(C-9)."],
    "NA29": ["쿨타임 표시 불일치가 밸런스 이슈로 읽힐 여지가 있어 기존 이슈와 같은 현상으로 판단했다."],
    "NA30": ["우선순위는 기존 이슈를 따른다(입력에서 알 수 없음)."],
    "NA31": ["우선순위는 기존 이슈를 따른다(입력에서 알 수 없음)."],
    "NA33": ["문의·건의 단일 정답. 개선 제안(C-2)으로 볼 여지가 있다."],
    "NA39": ["개선 제안·문의로 읽힐 여지가 조금 있으나 C-5 무관으로 두었다."],
    "NA40": ["게임 관련 홍보라 경계 사례. C-5 무관으로 두었다."],
    "NB03": ["서버 장애(H-1b). 본문의 '개발 QA가 별도로 재현할 수 있는 현상이 아님'은 기준서 문구와 닮았다. 다듬을지 정한다."],
    "NB06": ["모듈 첫 값 보안·어뷰징(결제·재화 병기). 본문의 '재현 가능한 절차가 확인됨'은 기준서 문구와 닮았다."],
    "NB08": ["앱 재실행 규칙으로 Critical 단일. 우회(창모드로 시작)가 가능해 Major 허용 여부가 논쟁될 수 있다."],
    "NB19": ["본문의 '결함은 아니며 사용성 개선 건의임'은 정답 단서에 가깝다. 우선순위 기준이 기준서에 명시돼 있지 않다."],
    "NB20": ["기존 이슈의 우선순위를 입력에서 알 수 없다(개발용 중복 문항과 같은 구조)."],
}


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


def item_hash(item: dict) -> str:
    """설계 의도 4 — 문항 전체(입력·라벨·근거)의 해시."""
    return hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def empty_answers() -> dict:
    return {"version": "v15-final", "source": "data/eval_v15_final/draft_v15_final.json", "method": "검토 화면 (계획 §3)",
            "answers": {}, "changes": []}


def load_items() -> list[dict]:
    return load_json(DRAFT, {"items": []})["items"]


def view(item: dict) -> dict:
    """설계 의도 1 — 화면에 주는 것: 입력, 라벨 제안, 근거, 판단 지점."""
    return {"번호": item["id"], "track": item["track"], "quality": item["quality"], "input": item["input"],
            "labels": item["labels"], "rationale": item["rationale"], "flags": FLAGS.get(item["id"], []),
            "hash": item_hash(item)}


def state() -> dict:
    return {"items": [view(i) for i in load_items()], "answers": load_json(ANSWERS, empty_answers())["answers"],
            "vocab": {k: sorted(x) for k, x in v.VOCAB.items()}}


def parse_list(text, vocab: set, field: str) -> list[str]:
    vals = text if isinstance(text, list) else [t.strip() for t in str(text).replace("/", ",").split(",") if t.strip()]
    if not vals or not all(isinstance(x, str) and x in vocab for x in vals) or len(set(vals)) != len(vals):
        raise ValueError(f"{field} 값은 {sorted(vocab)} 중에서, 쉼표로 구분해(첫 값이 가장 바람직한 답) 적으세요")
    return vals


def validate_payload(payload, ids: set[str]) -> dict:
    """설계 의도 3·6 — 저장할 결정. 형식이 어긋나면 ValueError."""
    if not isinstance(payload, dict):
        raise ValueError("요청 형식이 맞지 않습니다")
    qid, decision, edits = payload.get("번호"), payload.get("결정"), payload.get("수정") or {}
    if not isinstance(qid, str) or qid not in ids:
        raise ValueError(f"없는 번호: {qid}")
    if decision not in DECISIONS:
        raise ValueError("결정(승인 / 수정 필요)을 고르세요")
    if not isinstance(edits, dict) or not set(edits) <= set(EDIT_FIELDS):
        raise ValueError(f"수정할 칸은 {EDIT_FIELDS} 중에서 고르세요")
    if decision == "승인" and edits:
        raise ValueError("승인이면 수정 내용을 비워 두세요")
    if decision == "수정 필요" and not edits:
        raise ValueError("수정 필요이면 고칠 칸을 하나 이상 고르고 맞는 값을 적으세요")
    clean = {}
    for field, value in edits.items():
        if field in LABEL_FIELDS:
            clean[field] = parse_list(value, v.VOCAB[field], field)
        elif field == "위험":
            vals = [] if value in ("", None, "없음", []) else parse_list(value, set(v.RISKS), field)
            if len(vals) > 1:
                raise ValueError("위험은 한 문항에 하나(X-1·X-2·X-3) 또는 없음")
            clean[field] = vals
        elif field == "사람 검토":
            if value not in ("예", "아니오"):
                raise ValueError("사람 검토는 예 또는 아니오")
            clean[field] = value
        else:   # 본문·교체: 사유 메모
            text = str(value or "").strip()
            if not text:
                raise ValueError(f"{field} 수정은 무엇을 어떻게 바꿀지 적으세요")
            clean[field] = text
    return {"결정": decision, "수정": clean, "메모": str(payload.get("메모") or "").strip()}


def record(data: dict, qid: str, new: dict, h: str) -> dict:
    """설계 의도 2·4."""
    old = data["answers"].get(qid)
    if old and old.get("hash") == h and {k: old[k] for k in new} == new:
        return data
    now = datetime.now().isoformat(timespec="seconds")
    if old:
        data["changes"].append({"번호": qid, "이전": old, "recorded_at": now})
    data["answers"][qid] = {**new, "hash": h, "recorded_at": now}
    return data


def save_answer(payload) -> dict:
    items = {i["id"]: i for i in load_items()}
    new = validate_payload(payload, set(items))
    qid = payload["번호"]
    with LOCK:
        data = record(load_json(ANSWERS, empty_answers()), qid, new, item_hash(items[qid]))
        write_json(ANSWERS, data)
    return dict(data["answers"][qid])


def needs_review(item: dict, answers: dict) -> bool:
    """설계 의도 4 — 답이 없거나, 수정 필요이거나, 승인 뒤 문항이 바뀐 건."""
    a = answers.get(item["id"])
    return not a or a["결정"] != "승인" or a.get("hash") != item_hash(item)


def status() -> str:
    items, a = load_items(), load_json(ANSWERS, empty_answers())
    approved = sum(1 for i in items if not needs_review(i, a["answers"]))
    fix = sum(1 for i in items if a["answers"].get(i["id"], {}).get("결정") == "수정 필요")
    return f"승인 {approved}/{len(items)}건, 수정 필요 {fix}건, 고친 이력 {len(a['changes'])}건"


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
        """설계 의도 6."""
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
    p = argparse.ArgumentParser(description="새 최종 평가용 세트 검토 화면 (계획 §3)")
    p.add_argument("--port", type=int, default=8767)
    p.add_argument("--status", action="store_true")
    p.add_argument("--answers-dir", type=Path, help="답을 쓸 폴더 (화면 시험용. 기본은 data/eval_v15_final/)")
    args = p.parse_args(argv)
    if args.answers_dir:                                                   # 실제 답 파일을 건드리지 않고 화면을 시험한다
        global ANSWERS
        args.answers_dir.mkdir(parents=True, exist_ok=True)
        ANSWERS = args.answers_dir / ANSWERS.name
    if args.status:
        print(status())
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)       # 설계 의도 6
    print(f"검토 화면: http://127.0.0.1:{args.port}  ({status()})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
