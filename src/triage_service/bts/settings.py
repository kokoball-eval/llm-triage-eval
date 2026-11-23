"""BTS 연결 설정 읽기·검사 — 설정/bts_매핑.toml 과 환경 변수의 토큰.

[설계 의도]
1. 잘못된 설정은 서버를 시작하기 전에 멈춘다(fail loud). 모르는 키(오타), 빠진 키, 우선순위 대응 누락, 토큰 없음을
   시작 시점에 잡는다. 대응이 빠진 채 돌면 등록할 때마다 엉뚱한 값이 BTS 에 조용히 들어간다.
2. 우선순위 대응은 8칸 [우선순위]의 허용 값(triage_eval.contracts.ENUMS)을 빠짐없이 덮어야 한다. 허용 값이 바뀌면
   이 검사가 먼저 실패해 설정 파일을 고치게 한다.
3. 토큰은 환경 변수에서만 읽고 돌려주는 값에도 따로 담는다. 설정 내용을 화면·로그에 보여 줄 일이 생겨도 토큰이
   섞이지 않게 하기 위해서다.
4. 자동 배정([assign], assign.py)은 쓰지 않아도 된다. 쓰면 10개 모듈이 모두 팀에 대응해야 하고, 고른 BTS 의 [<bts>.teams]
   가 그 팀을 모두 BTS 계정(Jira 계정 ID, Redmine 사용자·그룹 번호)에 대응해야 한다. 빠진 모듈은 매번 QA팀으로,
   빠진 팀은 매번 등록 실패로 이어지므로 시작 시점에 막는다.
"""

import os
import tomllib
from pathlib import Path

from triage_eval.contracts import ENUMS, MODULES
from triage_service import assign

TRACKERS = ("fake", "jira", "redmine")
SCHEMA = {
    "jira": {"base_url": str, "project_key": str, "issue_type": str, "labels": list, "timeout_sec": (int, float),
             "priority": dict},
    "redmine": {"base_url": str, "project_id": str, "tracker_id": int, "timeout_sec": (int, float), "priority": dict},
}
OPTIONAL = {"teams": dict}   # 자동 배정을 쓸 때만 (설계 의도 4)
PRIORITY_TYPE = {"jira": str, "redmine": int}   # 팀 → 계정 값의 형식도 같다(Jira 는 글자, Redmine 은 번호)
SECRETS = {"fake": (), "jira": ("JIRA_EMAIL", "JIRA_API_TOKEN"), "redmine": ("REDMINE_API_KEY",)}


class SettingsError(Exception):
    """설정 파일·환경 변수 문제. 메시지를 그대로 보여 주고 서버를 시작하지 않는다."""


def load(path: Path, env=os.environ) -> dict:
    """{"use": 이름, "settings": 그 BTS 의 설정(없으면 {}), "secrets": 토큰, "assign": 자동 배정 규칙(없으면 None)}."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError(f"BTS 설정 파일이 없습니다: {path}")
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"BTS 설정 파일 문법 오류: {e}")
    problems = []
    if any(not isinstance(data.get(k, {}), dict) for k in ("bts", "assign", *SCHEMA)):
        raise SettingsError(f"BTS 설정 파일 형식 오류 ({path}): [bts]·[assign]·[jira]·[redmine] 은 표(섹션)여야 합니다")
    unknown = set(data) - {"bts", "assign", *SCHEMA}
    if unknown:
        problems.append(f"모르는 섹션: {sorted(unknown)}")
    use = data.get("bts", {}).get("use")
    if use not in TRACKERS:
        problems.append(f"[bts] use 는 {', '.join(TRACKERS)} 중 하나여야 합니다 (지금: {use!r})")
    if set(data.get("bts", {})) - {"use"}:
        problems.append(f"[bts] 모르는 키: {sorted(set(data['bts']) - {'use'})}")
    for name, schema in SCHEMA.items():
        if name in data:
            problems += _check_section(name, data[name], schema)
    if use in SCHEMA and use not in data:
        problems.append(f"[{use}] 섹션이 없습니다")
    rules = data.get("assign")
    if rules is not None:
        problems += _check_assign(rules)
        if use in SCHEMA and use in data and not problems:
            problems += _check_teams(use, rules, data[use].get("teams"))
    secrets = {k: env.get(k, "") for k in SECRETS.get(use, ())}
    problems += [f"환경 변수 {k} 가 없습니다 (.env 또는 시스템 환경 변수)" for k, v in secrets.items() if not v]
    if problems:
        raise SettingsError(f"BTS 설정 문제 ({path}):\n- " + "\n- ".join(problems))
    settings = dict(data.get(use, {}))
    if "base_url" in settings:
        settings["base_url"] = settings["base_url"].rstrip("/")
    return {"use": use, "settings": settings, "secrets": secrets, "assign": rules}


def _check_section(name: str, section: dict, schema: dict) -> list[str]:
    out = [f"[{name}] 모르는 키: {k}" for k in section if k not in schema and k not in OPTIONAL]
    for key, typ in {**schema, **{k: t for k, t in OPTIONAL.items() if k in section}}.items():
        if key not in section:
            out.append(f"[{name}] {key} 가 없습니다")
        elif not isinstance(section[key], typ) or isinstance(section[key], bool):
            out.append(f"[{name}] {key} 의 형식이 맞지 않습니다")
    url = section.get("base_url", "")
    if isinstance(url, str) and (not url.startswith(("http://", "https://")) or not url.split("://", 1)[1].strip("/")):
        out.append(f"[{name}] base_url 은 http:// 또는 https:// 로 시작하는 주소여야 합니다")
    for key, typ in schema.items():
        if typ is str and isinstance(section.get(key), str) and not section[key].strip():
            out.append(f"[{name}] {key} 가 비어 있습니다")
    timeout = section.get("timeout_sec")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout <= 0:
        out.append(f"[{name}] timeout_sec 는 0보다 커야 합니다")
    labels = section.get("labels")
    if isinstance(labels, list) and any(not isinstance(x, str) or not x or " " in x for x in labels):
        out.append(f"[{name}] labels 는 공백 없는 글자 목록이어야 합니다")
    prio = section.get("priority")
    if isinstance(prio, dict):   # 설계 의도 2
        missing = [p for p in ENUMS["우선순위"] if p not in prio]
        extra = [p for p in prio if p not in ENUMS["우선순위"]]
        bad = [p for p, v in prio.items() if not isinstance(v, PRIORITY_TYPE[name]) or isinstance(v, bool)]
        out += [f"[{name}.priority] 대응이 없는 우선순위: {p}" for p in missing]
        out += [f"[{name}.priority] 허용 값이 아닌 우선순위: {p}" for p in extra]
        out += [f"[{name}.priority] {p} 의 값 형식이 맞지 않습니다" for p in bad]
    return out


def _check_assign(rules: dict) -> list[str]:
    """설계 의도 4 — 처리 → 팀(qa·cs)과 모듈 → 팀 표."""
    out = [f"[assign] 모르는 키: {k}" for k in rules if k not in ("qa", "cs", "modules")]
    for key in ("qa", "cs"):
        if key in rules and not isinstance(rules[key], str):
            out.append(f"[assign] {key} 의 형식이 맞지 않습니다")
        elif key == "cs" and key in rules and rules[key] and not rules[key].strip():
            out.append("[assign] cs 팀 이름이 공백뿐입니다 (CS팀을 쓰지 않으려면 cs 줄을 지운다)")
    if not isinstance(rules.get("qa"), str) or not rules.get("qa", "").strip():
        out.append("[assign] qa 팀 이름이 없습니다 (재현이 필요한 건을 받을 팀)")
    modules = rules.get("modules")
    if not isinstance(modules, dict):
        return out + ["[assign.modules] 표가 없습니다 (모듈 → 개발팀)"]
    out += [f"[assign.modules] 팀이 없는 모듈: {m}" for m in MODULES if m not in modules]
    out += [f"[assign.modules] 허용 값이 아닌 모듈: {m}" for m in modules if m not in MODULES]
    out += [f"[assign.modules] {m} 의 팀 이름 형식이 맞지 않습니다" for m, t in modules.items()
            if not isinstance(t, str) or not t.strip()]
    if not out:   # " 클라"와 "클라"가 다른 팀이 되지 않게
        out += [f"[assign] 팀 이름 앞뒤에 공백이 있습니다: {t!r}" for t in assign.teams(rules) if t != t.strip()]
    return out


def _check_teams(name: str, rules: dict, teams) -> list[str]:
    """설계 의도 4 — 고른 BTS 에서 각 팀을 받을 계정."""
    if not isinstance(teams, dict):
        return [f"[{name}.teams] 표가 없습니다 (자동 배정의 팀 → {name} 계정)"]
    typ = PRIORITY_TYPE[name]
    out = [f"[{name}.teams] 계정이 없는 팀: {t}" for t in assign.teams(rules) if t not in teams]
    out += [f"[{name}.teams] {t} 의 계정 형식이 맞지 않습니다" for t, v in teams.items()
            if not isinstance(v, typ) or isinstance(v, bool) or (typ is str and not v.strip())]
    return out
