"""저장소 정합성 테스트 — 커밋한 설정·데이터 파일이 실제로 읽히는지 확인한다.

v1.5 라벨 일치율 기준 파일(label_agreement_v15.toml)은 그 파일을 읽는 코드보다 먼저 커밋되어, TOML 문법 오류
(따옴표 없는 한글 키)가 계산 도구를 만들 때까지 드러나지 않았다. 측정 전에 커밋하는 기준 파일은 읽는 코드가 늦게
생기는 경우가 많으므로, 파일이 커밋되는 시점에 바로 읽어 보는 테스트를 둔다.
"""

import importlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".git", ".venv", "dist", "__pycache__", ".pytest_cache", "Claude outputs"}


def repo_files(suffix: str) -> list[Path]:
    """저장소에 커밋된 파일만 본다. 로컬 작업 폴더(.gitignore 대상)에 둔 사본·생성물은 저장소 결과물이 아니다.
    git을 쓸 수 없는 환경이면 폴더를 훑되 알려진 작업 폴더는 뺀다."""
    try:
        out = subprocess.run(["git", "ls-files", "-z", f"*{suffix}"], cwd=ROOT, capture_output=True, check=True)
        return sorted(ROOT / f for f in out.stdout.decode("utf-8").split("\0") if f)
    except (OSError, subprocess.CalledProcessError):
        return sorted(p for p in ROOT.rglob(f"*{suffix}") if not SKIP & set(p.relative_to(ROOT).parts))


@pytest.mark.parametrize("path", repo_files(".toml"), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_toml_parses(path):
    tomllib.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", repo_files(".json"), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_json_parses(path):
    json.loads(path.read_text(encoding="utf-8"))


CRITERIA_DIR = ROOT / "기준"


def loaders():
    """기준/ 폴더의 기준 파일 → 그 파일을 읽는 함수. 새 기준 파일을 추가하면 여기에 등록해야 테스트가 통과한다.
    측정이 끝난 옛 기준 파일과 읽던 코드는 태그 v1.5-archive 에 있다."""
    from triage_eval.evaluation import gate

    return {"회귀_게이트_기준.toml": gate.load_criteria}


def test_every_criteria_file_has_a_loader():
    """기준 파일은 모두 기준/ 에 있고 읽는 함수가 등록되어 있다 — 읽는 코드 없이 커밋된 기준 파일을 막는다."""
    assert {p.name for p in CRITERIA_DIR.glob("*.toml")} == set(loaders())
    assert {p.name for p in ROOT.glob("*.toml")} == {"pyproject.toml"}


@pytest.mark.parametrize("name", sorted(loaders()))
def test_every_criteria_file_passes_its_loader(name):
    """문법뿐 아니라 각 도구의 키 검사(모르는 섹션·키, 빠진 키)까지 통과한다."""
    loaders()[name](CRITERIA_DIR / name)


def test_criteria_baselines_exist():
    """기준 파일이 가리키는 기준선 실행 기록이 실제로 있다 — 폴더를 옮긴 뒤 경로가 끊기지 않게."""
    crit = tomllib.loads((CRITERIA_DIR / "회귀_게이트_기준.toml").read_text(encoding="utf-8"))
    from triage_eval.config import BASELINE_DIR
    paths = [ROOT / p for p in crit["meta"]["baseline"]]
    assert all(p.is_file() and p.parent == BASELINE_DIR for p in paths)


# 구조 정리(B3) 전의 폴더 이름 — 코드·시험·기준 파일이 이 경로를 다시 가리키면 파일을 못 찾는다.
OLD_PATHS = ("data/", "docs/", "report/", "tools/", "gate_criteria_v15.toml")


def test_no_reference_to_removed_folders():
    for path in [*repo_files(".py"), *repo_files(".toml")]:
        text = path.read_text(encoding="utf-8")
        hits = [o for o in OLD_PATHS if o in text and path.name != "test_repo_integrity.py"]
        # 기준 파일 머리말은 원본 이름을 기록으로 남긴다
        if path.name == "회귀_게이트_기준.toml":
            hits = [o for o in hits if o not in ("gate_criteria_v15.toml", "report/")]
        assert not hits, (path.relative_to(ROOT).as_posix(), hits)


def test_every_console_script_resolves():
    """pyproject.toml 의 실행 명령이 모두 실제 함수를 가리킨다."""
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    for name, target in scripts.items():
        module, func = target.split(":")
        assert callable(getattr(importlib.import_module(module), func)), name


MD_LINK = re.compile(r"\]\(([^)\s]+)\)")


@pytest.mark.parametrize("path", repo_files(".md"), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_markdown_links_point_to_existing_files(path):
    """문서 안의 상대 링크는 실제 파일·폴더를 가리킨다 — 폴더를 옮기거나 지운 뒤 끊긴 링크를 막는다.
    지운 파일은 태그 v1.5-archive 주소(http 링크)로 가리키므로 이 검사에서 빠진다."""
    text = path.read_text(encoding="utf-8")
    broken = [t for t in MD_LINK.findall(text)
              if not t.startswith(("http://", "https://", "#", "mailto:"))
              and not (path.parent / t.partition("#")[0]).exists()]
    assert not broken, broken
