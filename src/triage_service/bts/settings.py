"""BTS 연결 설정 읽기·검사 — 설정/bts_매핑.toml 과 환경 변수의 토큰.

[설계 의도]
1. 잘못된 설정은 서버를 시작하기 전에 멈춘다(fail loud). 모르는 키(오타), 빠진 키, 우선순위 대응 누락, 토큰 없음을
   시작 시점에 잡는다. 대응이 빠진 채 돌면 등록할 때마다 엉뚱한 값이 BTS 에 조용히 들어간다.
2. 우선순위 대응은 8칸 [우선순위]의 허용 값(triage_eval.contracts.ENUMS)을 빠짐없이 덮어야 한다. 허용 값이 바뀌면
   이 검사가 먼저 실패해 설정 파일을 고치게 한다.
3. 토큰은 환경 변수에서만 읽고 돌려주는 값에도 따로 담는다. 설정 내용을 화면·로그에 보여 줄 일이 생겨도 토큰이
   섞이지 않게 하기 위해서다.
"""

import os
import tomllib
from pathlib import Path

from triage_eval.contracts import ENUMS

TRACKERS = ("fake", "jira", "redmine")
SCHEMA = {
    "jira": {"base_url": str, "project_key": str, "issue_type": str, "labels": list, "timeout_sec": (int, float),
             "priority": dict},
    "redmine": {"base_url": str, "project_id": str, "tracker_id": int, "timeout_sec": (int, float), "priority": dict},
}
PRIORITY_TYPE = {"jira": str, "redmine": int}
SECRETS = {"fake": (), "jira": ("JIRA_EMAIL", "JIRA_API_TOKEN"), "redmine": ("REDMINE_API_KEY",)}


class SettingsError(Exception):
    """설정 파일·환경 변수 문제. 메시지를 그대로 보여 주고 서버를 시작하지 않는다."""


def load(path: Path, env=os.environ) -> dict:
    """{"use": 이름, "settings": 그 BTS 의 설정(없으면 {}), "secrets": 토큰}."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError(f"BTS 설정 파일이 없습니다: {path}")
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"BTS 설정 파일 문법 오류: {e}")
    problems = []
    if any(not isinstance(data.get(k, {}), dict) for k in ("bts", *SCHEMA)):
        raise SettingsError(f"BTS 설정 파일 형식 오류 ({path}): [bts]·[jira]·[redmine] 은 표(섹션)여야 합니다")
    unknown = set(data) - {"bts", *SCHEMA}
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
    secrets = {k: env.get(k, "") for k in SECRETS.get(use, ())}
    problems += [f"환경 변수 {k} 가 없습니다 (.env 또는 시스템 환경 변수)" for k, v in secrets.items() if not v]
    if problems:
        raise SettingsError(f"BTS 설정 문제 ({path}):\n- " + "\n- ".join(problems))
    settings = dict(data.get(use, {}))
    if "base_url" in settings:
        settings["base_url"] = settings["base_url"].rstrip("/")
    return {"use": use, "settings": settings, "secrets": secrets}


def _check_section(name: str, section: dict, schema: dict) -> list[str]:
    out = [f"[{name}] 모르는 키: {k}" for k in section if k not in schema]
    for key, typ in schema.items():
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
