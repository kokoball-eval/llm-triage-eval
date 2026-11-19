"""링크 출처 설정 읽기·검사 — 설정/출처.toml 과 토큰(.env).

[설계 의도]
1. 잘못된 설정은 서버를 시작하기 전에 멈춘다(bts/settings.py 와 같은 방식). 모르는 키, 빠진 키, 측정한 값이 아닌
   reporter, 형식이 틀린 도메인을 시작 시점에 잡는다.
2. 토큰이 없는 출처는 켜지 않고 이유를 남긴다(오류로 멈추지 않는다). 붙여넣기는 언제나 쓸 수 있어야 하고, 출처 하나의
   토큰이 없다고 작업 페이지 전체가 멈추면 안 되기 때문이다. 꺼진 이유는 화면에 보여 준다.
3. Jira·Redmine 주소는 BTS 연결 설정(설정/bts_매핑*.toml)의 것을 쓴다. 같은 값을 두 파일에 적지 않기 위해서다.
"""

import os
import tomllib
from pathlib import Path

import soupsieve

from triage_service.bts.settings import SettingsError
from triage_service.contracts import REPORTERS

SCHEMA = {
    "fetch": {"timeout_sec": (int, float), "max_bytes": int},
    "jira": {"enabled": bool, "reporter": str},
    "redmine": {"enabled": bool, "reporter": str},
    "discord": {"enabled": bool, "board": str},
}
BOARD_KEYS = {"name": str, "domain": str, "board": str, "path_prefix": str, "title": str, "body": str, "posted_at": str}
SECRETS = {"jira": ("JIRA_EMAIL", "JIRA_API_TOKEN"), "redmine": ("REDMINE_API_KEY",), "discord": ("DISCORD_BOT_TOKEN",)}


def _read(path: Path) -> dict:
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SettingsError(f"설정 파일이 없습니다: {path}")
    except tomllib.TOMLDecodeError as e:
        raise SettingsError(f"설정 파일 문법 오류 ({path}): {e}")


def load(path: Path, bts_path: Path, env=os.environ) -> dict:
    """{"fetch", "jira"|None, "redmine"|None, "discord"|None, "boards": [...], "off": {출처: 꺼진 이유}}."""
    data, bts = _read(path), _read(bts_path)
    problems = [f"모르는 섹션: {k}" for k in data if k not in SCHEMA and k != "board"]
    for name, schema in SCHEMA.items():
        sec = data.get(name)
        if not isinstance(sec, dict):
            problems.append(f"[{name}] 섹션이 없습니다")
            continue
        problems += [f"[{name}] 모르는 키: {k}" for k in sec if k not in schema]
        problems += [f"[{name}] {k} 가 없거나 형식이 맞지 않습니다" for k, t in schema.items()
                     if not isinstance(sec.get(k), t) or (t is not bool and isinstance(sec.get(k), bool))]
    for name in ("jira", "redmine"):
        if isinstance(data.get(name), dict) and data[name].get("reporter") not in REPORTERS:
            problems.append(f"[{name}] reporter 는 {', '.join(REPORTERS)} 중 하나여야 합니다")
    fetch = data.get("fetch", {})
    if isinstance(fetch.get("max_bytes"), int) and fetch["max_bytes"] <= 0 or \
            isinstance(fetch.get("timeout_sec"), (int, float)) and fetch["timeout_sec"] <= 0:
        problems.append("[fetch] timeout_sec·max_bytes 는 0보다 커야 합니다")
    boards = data.get("board", [])
    if not isinstance(boards, list):
        problems.append("[[board]] 는 표 목록이어야 합니다")
        boards = []
    for i, b in enumerate(boards):
        where = f"[[board]] {i + 1}번째"
        if not isinstance(b, dict):
            problems.append(f"{where} 는 표여야 합니다")
            continue
        problems += [f"{where} 모르는 키: {k}" for k in b if k not in BOARD_KEYS]
        problems += [f"{where} {k} 가 없거나 형식이 맞지 않습니다" for k, t in BOARD_KEYS.items()
                     if not isinstance(b.get(k), t) or (k != "posted_at" and not b.get(k).strip())]
        if isinstance(b.get("domain"), str) and ("://" in b["domain"] or "/" in b["domain"]):
            problems.append(f"{where} domain 은 호스트[:포트]만 적습니다(예: forum.example.com)")
        if isinstance(b.get("path_prefix"), str) and not b["path_prefix"].startswith("/"):
            problems.append(f"{where} path_prefix 는 / 로 시작해야 합니다")
        for key in ("title", "body", "posted_at"):   # 설계 의도 1 — 잘못된 CSS 선택자도 시작 전에
            if isinstance(b.get(key), str) and b[key].strip():
                try:
                    soupsieve.compile(b[key])
                except Exception as e:
                    problems.append(f"{where} {key} 선택자 오류: {e}")
    if problems:
        raise SettingsError(f"출처 설정 문제 ({path}):\n- " + "\n- ".join(problems))

    out = {"fetch": fetch, "boards": boards, "off": {}, "jira": None, "redmine": None, "discord": None}
    for name in ("jira", "redmine", "discord"):   # 설계 의도 2
        if not data[name]["enabled"]:
            out["off"][name] = "설정에서 꺼 둠"
            continue
        secrets = {k: env.get(k, "") for k in SECRETS[name]}
        if not all(secrets.values()):
            out["off"][name] = f"토큰 없음({', '.join(k for k, v in secrets.items() if not v)})"
            continue
        if name == "discord":
            out[name] = {"board": data[name]["board"], "token": secrets["DISCORD_BOT_TOKEN"]}
            continue
        base = (bts.get(name) or {}).get("base_url", "")   # 설계 의도 3
        if not isinstance(base, str) or not base.startswith(("http://", "https://")):
            out["off"][name] = f"BTS 설정({bts_path.name})에 [{name}] base_url 이 없음"
            continue
        out[name] = {"base_url": base, "secrets": secrets, "reporter": data[name]["reporter"]}
    return out
