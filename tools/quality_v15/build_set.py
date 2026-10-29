"""v1.5 요약 품질 LLM-as-judge — 측정 세트·정답 대응표·개발용 자료·연습 문항을 만든다.

계획: report/quality_v15_plan.md (§3 측정 세트, §4 채점 화면, §5 채점관) · 기준: quality_v15.toml

사용법
------
    uv run python tools/quality_v15/build_set.py        # data/quality_v15/ 4개 파일 (없을 때만 쓴다)

출력
----
    data/quality_v15/measure_set_v15.json   측정 세트 46건 (Q01~Q46, 리포트 원문과 요약만, 출처·결함 여부 없음)
    data/quality_v15/answer_key_v15.json    정답 대응표 (번호 → 문항·실행 기록·결함 주입 여부·유형·원래 요약)
    data/quality_v15/dev_set_v15.json       채점관 프롬프트 개발용 자료 (측정 세트와 다른 요약)
    data/quality_v15/practice_v15.json      채점 전 연습 문항 5개 (가상 리포트, 측정 세트 밖)
    검토자는 채점을 마칠 때까지 answer_key·dev_set 과 이 파일을 열지 않는다(결함 주입 여부·주입 문장이 들어 있다).

[설계 의도]
1. 모델을 새로 실행하지 않는다. 저장된 qwen2.5:7b 실행 기록(v1.4 개발용, M0·M1·M2, 여러 seed, 1·2회차)의 [요약]만 쓴다.
   채점관(gemma4:12b)이 쓴 요약은 넣지 않는다(자기 글 채점 편향 방지).
2. 검토자가 이미 본 요약은 쓰지 않는다. 날조 측정 세트(data/judge_v15/)의 요약 63건과 그중 주입 요약의 원래 요약 20건을
   기준으로, 같은 리포트에서 글자가 같거나 거의 같은(공백을 뺀 difflib 유사도 0.85 이상) 요약을 뺀다. 같은 요약을 다시 보면
   앞선 판정·사후 의견이 따라온다. 계획 단계에서는 채택 구성 seed 21만 쓰려 했으나 그 요약 63건 중 28건이 날조 측정 세트와
   글자까지 같아 출처를 넓혔다(계획 §3).
3. 측정 세트에서 빼는 리포트(EXCLUDED_REPORTS): (가) 쓸 수 있는 요약이 남지 않은 리포트, (나) 이 세트를 만드는 대화에서
   주입 대상·원래 요약의 결함 여부가 검토자에게 알려진 리포트 8개, (다) 날조 측정에서 내용이 노출된 A06(ISSUE-012).
   남은 리포트마다 고정 seed로 요약 하나를 고른다. 자연 31 + 결함 주입 15 = 46건.
4. 결함 주입 15건(Q1 대상 누락·Q2 오해 소지·Q3 문장 결함 각 5건)은 고정 seed로 리포트를 뽑고 원래 요약을 손으로 고쳤다.
   원래 요약부터 품질 결함이 있는 리포트(DROPPED)는 깨끗한 양성 표본이 되지 않으므로, 같은 seed로 뽑은 예비 후보를 순서대로
   쓰되 예비 후보도 결함이 있으면 건너뛴다. 유형은 요약 내용에 맞춰 손으로 배정했다. 어느 리포트를 바꿨는지는 검토자 채점이
   끝난 뒤 결과 보고서에 적는다.
5. 결함 주입은 날조를 넣지 않는다. 주입 요약의 낱말은 원래 요약이나 리포트 원문에 있어야 하고, 그렇지 않은 낱말은
   INJECTIONS의 added 목록에 적는다(조사·어미·틀 바꾸기에 쓴 일반어). added 항목은 주입 요약을 쪼갠 낱말과 정확히 같아야
   한다. v1.2 탐지기가 원래 요약에 없던 구체 사실을 새로 잡으면 실패한다. Q2(오해 소지)는 이슈의 성격을 바꾸는 틀(질문 → 버그,
   불법 광고 → 공식 안내, 공략 → 결함 아님 등)로만 만들고, 원문에 없는 새 사건·수치·평가를 넣지 않는다. 정의(계획 §2)는
   "성격을 바꾸는 서술은 날조가 아니라 Q2로 본다"는 우선순위를 둔다. 정의의 예시는 측정 세트의 주입 틀과 겹치지 않는
   일반 예만 쓴다(예시가 단서가 되지 않게).
6. 개발용 자료는 측정 세트와 다른 요약(같은 리포트의 다른 미사용 요약)으로 만들고, 개발용 결함 주입 6건(유형별 2건)은
   측정용 주입 리포트와 겹치지 않는다. 측정 세트에서 뺀 리포트도 개발용에는 쓸 수 있다(검토자가 보지 않는 자료).
7. 연습 문항은 가상 리포트다. 측정 세트의 리포트·요약을 쓰지 않는다. 연습 답은 기록하지만 지표에 쓰지 않는다.
8. 측정 세트·대응표·개발용 자료·연습 문항은 봉인한다. 파일이 이미 있고 내용이 다르면 쓰지 않고 종료 코드 2로 끝낸다.
9. 주입 요약이 겉모양으로 드러나지 않게 평균 길이, "~다" 종결 비율, 명사형 종결(-임·-음·-함·-됨) 비율을 자연 요약과
   비슷하게 맞추고, 그 수치를 기록한다.
"""

import argparse
import difflib
import importlib.util
import json
import random
import re
import sys

from triage_eval.common.detect_hallucination import detect
from triage_eval.common.paths import ROOT

# 날조 측정의 읽기 함수(문항·요약·원문 칸)를 그대로 쓴다. 파일 이름이 같아(build_set) 경로로 따로 불러온다.
_spec = importlib.util.spec_from_file_location("judge_build_set", ROOT / "tools" / "judge_v15" / "build_set.py")
jb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jb)

SEED = 1518
OUT_DIR = ROOT / "data" / "quality_v15"
FILES = ("measure_set_v15.json", "answer_key_v15.json", "dev_set_v15.json", "practice_v15.json")
MODEL = "qwen2.5:7b"
SEEN_KEY = ROOT / "data" / "judge_v15" / "answer_key_v15.json"
SIMILAR = 0.85                                                # 설계 의도 2
CHAT_EXCLUDED = ["A04", "A06", "A18", "A20", "B09", "B18", "S04b", "S10", "S16"]   # 설계 의도 3 (나)·(다)
EXCLUDED_REPORTS = ["A04", "A05", "A06", "A11", "A13", "A14", "A18", "A20", "B04", "B05", "B08", "B09", "B12", "B18",
                    "S04b", "S10", "S16"]                       # 설계 의도 3 — (가)·(나)·(다)를 합친 결과
DROPPED = {"S04": "원래 요약이 어색함('미남기')", "S20": "원래 요약에 원문에 없는 '몹량'"}   # 설계 의도 4
KINDS = {"Q1": "대상 누락", "Q2": "오해 소지", "Q3": "문장 결함"}

# 설계 의도 4·5 — {문항 ID: (유형, 원래 요약, 주입 요약, 원문·원래 요약에 없는 일반어)}
INJECTIONS = {
    "B01": ("Q1", "신규 계정 생성 후 첫 로딩 중 앱 강제 종료", "특정 계정으로 첫 로딩 중 앱이 강제 종료됨", ["특정", "계정으로", "앱이", "종료됨"]),
    "B02": ("Q1", "메인 퀘스트 4-7 NPC 리안 스폰 불가", "메인 퀘스트 진행 중 NPC가 스폰되지 않아 진행 불가", ["NPC가", "스폰되지", "않아"]),
    "B14": ("Q1", "펼친 화면에서 스킬 버튼이 화면 밖으로 잘림", "일부 화면에서 스킬 버튼이 화면 밖으로 잘림", ["일부"]),
    "S09": ("Q1", "갤럭시 탭 S4에서 PC 계정으로 로그인하면 앱이 꺼집니다", "일부 기기에서 PC 계정으로 로그인하면 앱이 꺼집니다", ["일부", "기기에서"]),
    "B10": ("Q1", "메인 스토리 4장 대사 오타 제보", "스토리 진행 중 대사에 오타가 있음", ["진행", "대사에", "오타가", "있음"]),
    "A15": ("Q2", "길드 가입 버튼 회색으로 표시되어 가입 불가", "길드 가입 버튼이 회색으로 표시되는 버그 제보",
            ["버튼이", "표시되는", "버그", "제보"]),
    "S03": ("Q2", "보스전 맵 구석 바위 위에 점프하면 보스가 공격하지 않음", "보스전 맵 구석 바위 위에 점프하면 보스 공격을 피하는 공략 공유",
            ["공격을", "피하는", "공략", "공유"]),
    "A17": ("Q2", "다이아 대리충전 광고 게시글", "다이아 충전 할인 안내 게시글", ["할인", "안내"]),
    "S19": ("Q2", "추석 이벤트 보상 내용에 대한 유저 불만", "추석 이벤트 보상 확대를 건의함", ["확대를", "건의함"]),
    "A16": ("Q2", "게임에 대한 불만 표현", "게임에 대한 개선 의견 건의", ["개선", "의견", "건의"]),
    "A03": ("Q3", "PC에서 로그인 불가", "PC 환경에서 로그인을 시도하면 계속", ["환경에서", "로그인을", "시도하면", "계속"]),
    "A07": ("Q3", "2026-09-17 업데이트 후 계정 데이터 손실", "2026-09-17 업데이트 후 계정 데이터를 손실되어",
            ["데이터를", "손실되어"]),
    "S08": ("Q3", "욕설 필터 우회 가능", "월드 채팅 욕설 필터를 숫자나 점으로 우회를", ["필터를", "점으로", "우회를"]),
    "S15": ("Q3", "보스 잡으면 앱 꺼짐, 보상 미지급", "신규 레이드 보스 잡으면 앱이 꺼지고 보상을", ["앱이", "꺼지고", "보상을"]),
    "B13": ("Q3", "와이파이에서 LTE로 전환 시 끊김 현상 제보", "LTE로 와이파이에서 끊김 전환 시 현상 제보", []),
}

# 설계 의도 6 — 개발용 결함 주입 6건 (유형별 2건). 원래 요약은 개발용 자료에 고른 요약이어야 한다.
DEV_INJECTIONS = {
    "A02": ("Q1", "보스방에 들어가면 튕김 현상 발생", "특정 장소에 들어가면 튕김 현상 발생", ["특정", "장소에"]),
    "B05": ("Q1", "서브 퀘스트 '잃어버린 등불' 완료 불가", "일부 퀘스트 완료 불가", ["일부"]),
    "A18": ("Q2", "추석 픽업 10연차에서 SSR 두 장 뽑음", "추석 픽업 10연차에서 SSR 두 장이 뽑히는 확률 오류 제보",
            ["장이", "뽑히는", "확률", "오류", "제보"]),
    "S04": ("Q2", "랭크전에서 패배 기록이 안 남는 현상 제보", "랭크전 패배 기록 삭제 기능 건의", ["랭크전", "삭제", "기능", "건의"]),
    "B17": ("Q3", "전투 중 전화 수신 후 복귀 시 사운드 끊김과 화면 정지 현상 발생", "전투 중 전화 수신 후 복귀 시 사운드를 끊김과 화면 정지",
            ["사운드를", "끊김과"]),
    "B04": ("Q3", "카일의 궁극기 '천벌' 데미지 미적용", "카일의 궁극기 '천벌'이 데미지 미적용을", ["미적용을"]),
}

# 설계 의도 7 — 연습 문항 (가상 리포트). 참고 답은 Claude 제안이며, 검토자가 답한 뒤에 보여 준다.
PRACTICE = [
    {"번호": "P1", "제목": "인벤토리 아이콘 깨짐", "본문": "PC에서 인벤토리를 열면 아이템 아이콘이 깨져 보입니다. 재현율 3/5.",
     "요약": "인벤토리 아이템 아이콘 깨짐", "참고 답": "없음", "참고 설명": "무엇이(아이템 아이콘)·어디서(인벤토리)가 있고 뜻이 분명하다"},
    {"번호": "P2", "제목": "출석 보상 안 들어옴", "본문": "7일 출석 이벤트 마지막 날 보상(강화석 30개)이 우편함에 안 들어왔어요. 모바일입니다.",
     "요약": "보상이 안 들어옴", "참고 답": "있음 (Q1 대상 누락)", "참고 설명": "어떤 보상인지(7일 출석 마지막 날) 빠져 다른 보상 문제와 구분되지 않는다"},
    {"번호": "P3", "제목": "[건의] 친구 목록 정렬 기능", "본문": "친구가 100명이 넘으니 접속 순으로 정렬하는 기능이 있으면 좋겠습니다.",
     "요약": "친구 목록 정렬 기능 오류", "참고 답": "있음 (Q2 오해 소지)", "참고 설명": "기능 건의를 오류처럼 썼다. 읽는 사람이 결함으로 등록할 수 있다"},
    {"번호": "P4", "제목": "길드 창고 아이템 사라짐", "본문": "길드 창고에 넣은 전설 무기가 다음 날 사라졌습니다. 창고 기록에도 없습니다.",
     "요약": "길드 창고에 넣은 전설 무기를 다음 날", "참고 답": "있음 (Q3 문장 결함)", "참고 설명": "서술어가 없어 무슨 일이 있었는지 문장으로 완성되지 않는다"},
    {"번호": "P5", "제목": "채팅창 글자 겹침", "본문": "모바일에서 길드 채팅창 글씨가 두 줄이 겹쳐 보여요. 가끔 그럽니다.",
     "요약": "모바일 길드 채팅창에서 글자가 간헐적으로 겹쳐 보임", "참고 답": "없음", "참고 설명": "'가끔'을 '간헐적으로'로 바꿔 쓴 것은 품질 문제가 아니다"},
]

TOKEN = re.compile(r"[^\s,.'\"()]+")


def run_logs() -> list[str]:
    """설계 의도 1 — qwen2.5:7b 개발용 실행 기록 (저장소 상대 경로, 정렬)."""
    hist = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "data/results/v14/history").glob("v14_dev_*.json"))
    base = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / "data/results/v14/baseline").glob("v14_dev_*.json"))
    return hist + base


def seen_by_report() -> dict[str, list[str]]:
    """설계 의도 2 — {문항 ID: 검토자가 날조 측정에서 본 요약(주입 원래 요약 포함)}."""
    out: dict[str, list[str]] = {}
    for e in json.loads(SEEN_KEY.read_text(encoding="utf-8"))["map"].values():
        out.setdefault(e["item_id"], []).append(e["summary"].strip())
        if e["injected"]:
            out[e["item_id"]].append(e["original_summary"].strip())
    return out


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, re.sub(r"\s", "", a), re.sub(r"\s", "", b), autojunk=False).ratio()


def all_summaries() -> dict[str, dict[str, tuple[str, int]]]:
    """{문항 ID: {요약: (처음 나온 실행 기록, 회차)}} — 설계 의도 1의 실행 기록 전체."""
    pool: dict[str, dict[str, tuple[str, int]]] = {}
    for log in run_logs():
        for ri in (1, 2):
            for i, s in jb.summaries(log, MODEL, ri).items():
                pool.setdefault(i, {}).setdefault(s.strip(), (log, ri))
    return pool


def unseen_pool() -> dict[str, dict[str, tuple[str, int]]]:
    """설계 의도 2·3 — 측정 세트에 쓸 수 있는 요약. 본 요약과 유사도 SIMILAR 이상과 제외 리포트를 뺀다."""
    seen = seen_by_report()
    pool = {i: {s: v for s, v in d.items() if all(similarity(s, x) < SIMILAR for x in seen.get(i, []))}
            for i, d in all_summaries().items() if i not in CHAT_EXCLUDED}
    return {i: d for i, d in pool.items() if d}


def check_injections(injections: dict, items: dict, base: dict[str, str]) -> None:
    """설계 의도 5."""
    problems = []
    for i, (kind, original, injected, added) in injections.items():
        if kind not in KINDS:
            problems.append(f"{i} 유형 {kind!r}")
        if base.get(i) != original:
            problems.append(f"{i} 원래 요약이 고른 요약과 다릅니다")
        if injected == original:
            problems.append(f"{i} 주입 요약이 원래 요약과 같습니다")
        ref = original + "\n" + jb.source_text(items[i])
        missing = [t for t in TOKEN.findall(injected) if len(t) >= 2 and t not in ref and t not in added]
        if missing:
            problems.append(f"{i} 원문·원래 요약에 없는 낱말 {missing} (added 에 적지 않음)")
        unused = [a for a in added if a not in TOKEN.findall(injected)]
        if unused:
            problems.append(f"{i} added 에 적었지만 주입 요약의 낱말과 맞지 않는 항목 {unused}")
        before = {(f["category"], f["entity"]) for f in detect("[요약]: " + original, ref)["ungrounded"]}
        after = {(f["category"], f["entity"]) for f in detect("[요약]: " + injected, ref)["ungrounded"]}
        if after - before:
            problems.append(f"{i} 탐지기가 새 구체 사실을 잡았습니다 {sorted(after - before)}")
    if problems:
        raise ValueError("; ".join(problems))


def build(seed: int = SEED) -> tuple[dict, dict, dict, dict]:
    """(측정 세트, 정답 대응표, 개발용 자료, 연습 문항)."""
    rng = random.Random(seed)
    items = jb.load_items()
    pool = unseen_pool()
    if sorted(set(items) - set(pool)) != EXCLUDED_REPORTS:
        raise ValueError(f"측정 세트에서 빠지는 리포트가 바뀌었습니다: {sorted(set(items) - set(pool))}")
    ids = sorted(pool)
    pick = {i: rng.choice(sorted(pool[i])) for i in ids}
    drawn = sorted(rng.sample(ids, 15))
    spare = [i for i in rng.sample([i for i in ids if i not in drawn], 12) if i not in DROPPED]   # 설계 의도 4
    targets = sorted([i for i in drawn if i not in DROPPED] + spare[:len([i for i in drawn if i in DROPPED])])
    if not set(DROPPED) <= set(drawn) or targets != sorted(INJECTIONS):
        raise ValueError(f"주입 대상이 설계 의도 4와 다릅니다: 뽑힘 {drawn}, 대상 {targets}")
    check_injections(INJECTIONS, items, pick)

    entries = []
    for i in ids:
        log, ri = pool[i][pick[i]]
        e = {"item_id": i, "log": log, "run_index": ri, "model": MODEL, "injected": i in INJECTIONS}
        if i in INJECTIONS:
            kind, original, injected, added = INJECTIONS[i]
            e.update(kind=kind, kind_name=KINDS[kind], added=added, original_summary=original, summary=injected)
        else:
            e["summary"] = pick[i]
        entries.append(e)
    rng.shuffle(entries)
    key, measure = {}, []
    for n, e in enumerate(entries, start=1):
        qid = f"Q{n:02d}"
        key[qid] = e
        measure.append({"번호": qid, **jb.report_fields(items[e["item_id"]]), "요약": e["summary"]})

    everything = all_summaries()                                                                   # 설계 의도 6
    used = {e["item_id"]: {e["summary"], e.get("original_summary", e["summary"])} for e in entries}
    dev_pick = {i: rng.choice(sorted(set(d) - used.get(i, set()))) for i, d in sorted(everything.items())
                if set(d) - used.get(i, set())}
    bad = sorted(set(DEV_INJECTIONS) & set(INJECTIONS)) + sorted(set(DEV_INJECTIONS) - set(dev_pick))
    if bad:
        raise ValueError(f"개발용 주입 대상 {bad} 가 측정용 주입과 겹치거나 개발용 요약이 없습니다")
    check_injections(DEV_INJECTIONS, items, dev_pick)
    dev = [{"item_id": i, "log": everything[i][dev_pick[i]][0], "run_index": everything[i][dev_pick[i]][1], "model": MODEL,
            "injected": i in DEV_INJECTIONS,
            **({"kind": DEV_INJECTIONS[i][0], "original_summary": dev_pick[i]} if i in DEV_INJECTIONS else {}),
            "summary": DEV_INJECTIONS[i][2] if i in DEV_INJECTIONS else dev_pick[i],
            **jb.report_fields(items[i])} for i in sorted(dev_pick)]

    meta = {"plan": "report/quality_v15_plan.md", "seed": seed, "dataset": jb.DATASET.relative_to(ROOT).as_posix()}
    return ({**meta, "count": len(measure), "items": measure},
            {**meta, "note": "검토자는 채점을 마칠 때까지 이 파일을 열지 않는다 (계획 §3)", "excluded_reports": EXCLUDED_REPORTS,
             "dropped_targets": DROPPED, "map": key},
            {**meta, "note": "채점관 프롬프트 개발용 (계획 §5). 측정 세트와 다른 요약. 검토자는 채점을 마칠 때까지 열지 않는다",
             "items": dev},
            {**meta, "note": "채점 전 연습 문항 (가상 리포트, 계획 §4). 지표에 쓰지 않는다", "items": PRACTICE})


def length_gap(key: dict) -> dict:
    """주입 요약과 자연 요약의 평균 글자 수 (계획 §8 한계에 적는 겉모양 단서)."""
    inj = [len(e["summary"]) for e in key.values() if e["injected"]]
    nat = [len(e["summary"]) for e in key.values() if not e["injected"]]
    return {"injected_mean": round(sum(inj) / len(inj), 1), "natural_mean": round(sum(nat) / len(nat), 1)}


def write_sealed(payloads: dict[str, dict]) -> list[str]:
    """설계 의도 8."""
    changed = [n for n, p in payloads.items() if (OUT_DIR / n).exists()
               and json.loads((OUT_DIR / n).read_text(encoding="utf-8")) != json.loads(jb.serialize(p))]
    if changed:
        return changed
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for n, p in payloads.items():
        if not (OUT_DIR / n).exists():
            (OUT_DIR / n).write_text(jb.serialize(p), encoding="utf-8")
    return []


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    argparse.ArgumentParser(description="요약 품질 측정 세트·대응표·개발용 자료·연습 문항 생성").parse_args(argv)
    payloads = dict(zip(FILES, build()))
    changed = write_sealed(payloads)
    if changed:
        print(f"봉인된 파일과 내용이 다릅니다 — 쓰지 않습니다: {', '.join(changed)}")
        return 2
    m, k, d, _ = (payloads[n] for n in FILES)
    print(f"측정 세트 {m['count']}건 (결함 주입 {sum(e['injected'] for e in k['map'].values())}건), "
          f"개발용 {len(d['items'])}건 (결함 주입 {sum(x['injected'] for x in d['items'])}건), 연습 {len(PRACTICE)}건")
    print(f"저장: data/quality_v15/ ({', '.join(FILES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
