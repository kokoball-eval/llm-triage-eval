"""BTS 어댑터. config.TRACKER 이름으로 고른다. Jira·Redmine 은 S2 에서 붙인다."""

from triage_service.bts.fake import FakeTracker
from triage_service.store import Store


def make_tracker(name: str, store: Store):
    if name == "fake":
        return FakeTracker(store.path)
    raise ValueError(f"모르는 BTS: {name}")
