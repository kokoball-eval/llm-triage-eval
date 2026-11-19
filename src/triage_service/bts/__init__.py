"""BTS 어댑터. 설정/bts_매핑.toml 의 [bts] use 로 고른다 (settings.py 가 읽고 검사한다)."""

from triage_service.bts.fake import FakeTracker
from triage_service.bts.jira import JiraTracker
from triage_service.bts.redmine import RedmineTracker
from triage_service.store import Store


def make_tracker(loaded: dict, store: Store, http=None):
    """loaded: settings.load() 결과. http 는 시험에서 녹화 응답을 돌려주는 httpx2 클라이언트를 넣을 때 쓴다."""
    use = loaded["use"]
    if use == "fake":
        return FakeTracker(store.path)
    cls = {"jira": JiraTracker, "redmine": RedmineTracker}[use]
    return cls(loaded["settings"], loaded["secrets"], http)
