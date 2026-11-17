"""v1.5 새 최종 평가용 세트 검토 화면 서버(tools/dataset_v15_final/review_server.py) 시험.

모델을 호출하지 않으므로 CI에서 자동 실행된다. 실제 답 파일(data/eval_v15_final/review_answers_v15_final.json)은 건드리지 않는다.
"""

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib import request
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools" / "dataset_v15_final"))

import review_server as rs  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    """초안 문항 3건짜리 임시 환경 — 답 파일은 임시 폴더."""
    items = [{"id": f"NA0{i}", "track": "A", "quality": "명확", "input": {"title": f"제목{i}", "body": "본문", "context": {}},
              "labels": {"분류": ["결함"], "우선순위": ["Major"], "모듈": ["UI·텍스트"], "재현 정보": ["충분"], "처리": ["등록(재현 대기)"],
                         "발생 빈도": ["항상"], "human_review": False, "risk": [], "must_request": [], "must_recommend": []},
              "rationale": "C-1"} for i in (1, 2, 3)]
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(rs, "DRAFT", draft)
    monkeypatch.setattr(rs, "ANSWERS", tmp_path / "answers.json")
    return items, draft


def payload(qid="NA01", decision="승인", edits=None, memo=""):
    return {"번호": qid, "결정": decision, "수정": edits or {}, "메모": memo}


def test_approve_saves_and_status(env):
    saved = rs.save_answer(payload())
    assert saved["결정"] == "승인" and saved["수정"] == {} and saved["hash"]
    assert rs.status() == "승인 1/3건, 수정 필요 0건, 고친 이력 0건"


def test_same_payload_twice_leaves_no_history(env):
    rs.save_answer(payload())
    rs.save_answer(payload())
    assert json.loads(rs.ANSWERS.read_text(encoding="utf-8"))["changes"] == []


def test_changed_answer_keeps_previous_in_history(env):
    rs.save_answer(payload())
    rs.save_answer(payload(decision="수정 필요", edits={"우선순위": "Minor, Trivial"}))
    data = json.loads(rs.ANSWERS.read_text(encoding="utf-8"))
    assert len(data["changes"]) == 1 and data["changes"][0]["이전"]["결정"] == "승인"
    assert data["answers"]["NA01"]["수정"] == {"우선순위": ["Minor", "Trivial"]}


@pytest.mark.parametrize("bad", [
    {"번호": "NA99", "결정": "승인"},
    {"번호": "NA01", "결정": "보류"},
    {"번호": "NA01", "결정": "승인", "수정": {"분류": "결함"}},          # 승인인데 수정 내용
    {"번호": "NA01", "결정": "수정 필요", "수정": {}},                    # 수정인데 내용 없음
    {"번호": "NA01", "결정": "수정 필요", "수정": {"모르는칸": "x"}},
    {"번호": "NA01", "결정": "수정 필요", "수정": {"분류": "없는분류"}},
    {"번호": "NA01", "결정": "수정 필요", "수정": {"분류": "결함, 결함"}},
    {"번호": "NA01", "결정": "수정 필요", "수정": {"위험": "X-1, X-3"}},
    {"번호": "NA01", "결정": "수정 필요", "수정": {"사람 검토": "모름"}},
    {"번호": "NA01", "결정": "수정 필요", "수정": {"본문": "   "}},
    ["목록"], "문자열",
])
def test_invalid_payloads_are_rejected_without_saving(env, bad):
    with pytest.raises(ValueError):
        rs.save_answer(bad)
    assert not rs.ANSWERS.exists()


def test_valid_edit_kinds(env):
    ok = rs.save_answer(payload(decision="수정 필요", edits={"위험": "없음", "사람 검토": "예", "본문": "문구를 다듬기", "모듈": ["UI·텍스트", "네트워크"]}))
    assert ok["수정"]["위험"] == [] and ok["수정"]["사람 검토"] == "예" and ok["수정"]["모듈"] == ["UI·텍스트", "네트워크"]


def test_fix_needed_and_changed_items_need_review_again(env):
    items, draft = env
    rs.save_answer(payload("NA01"))
    rs.save_answer(payload("NA02", "수정 필요", {"우선순위": "Minor"}))
    answers = json.loads(rs.ANSWERS.read_text(encoding="utf-8"))["answers"]
    loaded = rs.load_items()
    assert [rs.needs_review(i, answers) for i in loaded] == [False, True, True]   # 승인 / 수정 필요 / 미검토
    items[0]["input"]["title"] = "고친 제목"                                       # 승인한 문항이 바뀜 → 다시 검토
    draft.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
    assert rs.needs_review(rs.load_items()[0], answers)
    rs.save_answer(payload("NA01"))
    assert rs.status().startswith("승인 1/3")
    assert len(json.loads(rs.ANSWERS.read_text(encoding="utf-8"))["changes"]) == 1   # 이전 승인이 이력에 남는다


def test_broken_answers_file_is_not_overwritten(env):
    rs.ANSWERS.write_text("{깨진", encoding="utf-8")
    with pytest.raises(rs.BrokenFile):
        rs.save_answer(payload())
    assert rs.ANSWERS.read_text(encoding="utf-8") == "{깨진"


def test_state_shows_input_labels_flags_but_no_model_fields(env):
    s = rs.state()
    r = s["items"][0]
    assert set(r) == {"번호", "track", "quality", "input", "labels", "rationale", "flags", "hash"}
    assert s["vocab"]["처리"] and s["answers"] == {}


def test_flags_point_at_existing_draft_items():
    real = json.loads((ROOT / "data" / "eval_v15_final" / "draft_v15_final.json").read_text(encoding="utf-8"))
    ids = {i["id"] for i in real["items"]}
    assert set(rs.FLAGS) <= ids and "NA03" in rs.FLAGS and rs.FLAGS["NA03"][0].startswith("**결정 필요")


def test_server_reads_only_draft_and_answers_files():
    """모델 응답·측정 결과를 읽는 코드가 없다(계획 §2-5)."""
    src = (ROOT / "tools" / "dataset_v15_final" / "review_server.py").read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]
    for word in ("results", "judge_v15", "quality_v15", "routing", "baseline"):
        assert word not in code


def test_http_endpoints_host_and_content_type_checks(env):
    server = ThreadingHTTPServer(("127.0.0.1", 0), rs.Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        base = f"http://127.0.0.1:{port}"
        assert json.loads(request.urlopen(base + "/api/state").read())["items"][0]["번호"] == "NA01"
        assert b"<html" in request.urlopen(base + "/").read()
        body = json.dumps(payload()).encode("utf-8")
        ok = request.urlopen(request.Request(base + "/api/answer", body, {"Content-Type": "application/json"}))
        assert json.loads(ok.read())["saved"]["결정"] == "승인"
        with pytest.raises(HTTPError) as e:      # JSON 이 아닌 저장 요청
            request.urlopen(request.Request(base + "/api/answer", body, {"Content-Type": "text/plain"}))
        assert e.value.code == 403
        with pytest.raises(HTTPError) as e:      # 이 기기가 아닌 Host
            request.urlopen(request.Request(base + "/api/state", headers={"Host": "evil.example"}))
        assert e.value.code == 403
        with pytest.raises(HTTPError) as e:      # 형식이 어긋난 요청은 400, 서버는 계속 돈다
            request.urlopen(request.Request(base + "/api/answer", b"[1]", {"Content-Type": "application/json"}))
        assert e.value.code == 400
        assert json.loads(request.urlopen(base + "/api/state").read())["answers"]
    finally:
        server.shutdown()
        server.server_close()
