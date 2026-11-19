"""BTS 이슈 링크 출처 — Jira·Redmine 에 이미 등록된 이슈를 가져와 다시 트리아지한다.

[설계 의도]
1. QA 가 BTS 에 써 둔 이슈의 우선순위·처리가 맞는지 다시 점검하는 용도다. 측정 때의 입력 종류 B(QA 가 쓴 BTS 이슈)와
   같은 상황이므로 track B 로 넣고, 작성자는 설정의 reporter(측정한 평가셋 값 중 하나)로 둔다.
2. 주소·토큰은 BTS 연결 설정의 것을 쓰고, 접속은 등록 어댑터와 같은 함수(bts/clients.py)로 만든다. 이 PC 의 설정에 없는 BTS 주소의 링크는 맡지 않는다(SSRF 방지).
   요청은 설정의 주소에 링크에서 뽑은 키·번호만 붙여 보내므로, 링크의 나머지(쿼리 등)는 요청에 쓰이지 않는다.
3. 링크 모양은 각 BTS 의 기본 주소 형식만 받는다: Jira <주소>/browse/<키>, Redmine <주소>/issues/<번호>. 주소에 하위
   경로가 있어도(예: https://host/redmine) 그 아래의 링크를 맡는다. 접수할 때는 하나로 맞춘 링크(canonical)를 쓴다.
4. Jira 설명은 문서 형식(ADF)이라 글자만 꺼낸다. 문단·제목은 줄바꿈으로 나누고, 멘션·이모지·링크 카드는 표시 글자를 쓴다.
"""

import re
from urllib.parse import urlsplit

import httpx2

from triage_service.bts import clients
from triage_service.contracts import Report
from triage_service.intake.fetching import IntakeError, get_json

JIRA_PATH = re.compile(r"^/browse/([A-Za-z][A-Za-z0-9_]+-\d+)/?$")
REDMINE_PATH = re.compile(r"^/issues/(\d+)/?$")


def _key(url: str, base_url: str, pattern: re.Pattern) -> str | None:
    """링크가 설정 주소 아래의 이슈 링크이면 키·번호 (설계 의도 2·3)."""
    u, b = urlsplit(url), urlsplit(base_url)
    if (u.scheme.lower(), u.netloc.lower()) != (b.scheme.lower(), b.netloc.lower()):
        return None
    base_path = b.path.rstrip("/")
    if not u.path.startswith(base_path + "/"):
        return None
    m = pattern.match(u.path[len(base_path):])
    return m.group(1) if m else None


class JiraSource:
    name = "jira"

    def __init__(self, base_url: str, secrets: dict, reporter: str, fetch: dict, http: httpx2.Client | None = None):
        self.base_url, self.reporter, self.max_bytes = base_url.rstrip("/"), reporter, fetch["max_bytes"]
        self.http = http or clients.jira(self.base_url, secrets, fetch["timeout_sec"])

    def matches(self, url: str) -> bool:
        return _key(url, self.base_url, JIRA_PATH) is not None

    def canonical(self, url: str) -> str:
        return f"{self.base_url}/browse/{_key(url, self.base_url, JIRA_PATH).upper()}"

    def fetch(self, url: str) -> Report:
        key = _key(url, self.base_url, JIRA_PATH).upper()
        data = get_json(self.http, f"/rest/api/3/issue/{key}?fields=summary,description", self.max_bytes)
        fields = data.get("fields") if isinstance(data.get("fields"), dict) else {}
        if not isinstance(fields.get("summary"), str) or not fields["summary"].strip():
            raise IntakeError(f"Jira 이슈 {key} 의 제목을 읽지 못했습니다")
        return Report(track="B", title=fields["summary"].strip(), body=adf_text(fields.get("description")).strip(),
                      reporter=self.reporter, source=self.name, url=self.canonical(url))


class RedmineSource:
    name = "redmine"

    def __init__(self, base_url: str, secrets: dict, reporter: str, fetch: dict, http: httpx2.Client | None = None):
        self.base_url, self.reporter, self.max_bytes = base_url.rstrip("/"), reporter, fetch["max_bytes"]
        self.http = http or clients.redmine(self.base_url, secrets, fetch["timeout_sec"])

    def matches(self, url: str) -> bool:
        return _key(url, self.base_url, REDMINE_PATH) is not None

    def canonical(self, url: str) -> str:
        return f"{self.base_url}/issues/{int(_key(url, self.base_url, REDMINE_PATH))}"

    def fetch(self, url: str) -> Report:
        issue_id = int(_key(url, self.base_url, REDMINE_PATH))
        issue = get_json(self.http, f"/issues/{issue_id}.json", self.max_bytes).get("issue")
        if not isinstance(issue, dict) or not isinstance(issue.get("subject"), str) or not issue["subject"].strip():
            raise IntakeError(f"Redmine 이슈 #{issue_id} 의 제목을 읽지 못했습니다")
        body = (issue.get("description") or "").replace("\r\n", "\n").strip()
        return Report(track="B", title=issue["subject"].strip(), body=body, reporter=self.reporter,
                      source=self.name, url=self.canonical(url))


BLOCKS = {"paragraph", "heading", "blockquote", "codeBlock", "rule", "tableRow"}
INLINE_ATTR = {"mention": "text", "emoji": "text", "inlineCard": "url", "date": "timestamp", "status": "text"}


def adf_text(node) -> str:
    """설계 의도 4 — Jira 문서 형식에서 글자만. 목록 항목은 안의 문단이 이미 줄을 나누므로 따로 나누지 않는다."""
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind in INLINE_ATTR:
        return str((node.get("attrs") or {}).get(INLINE_ATTR[kind], ""))
    inner = "".join(adf_text(c) for c in node.get("content", []) if isinstance(c, dict))
    if kind in ("tableCell", "tableHeader"):
        return inner.strip() + " | "
    return inner + "\n" if kind in BLOCKS else inner
