from datetime import timedelta, tzinfo

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def fold_timezone():
    """A repeated-hour timezone that needs no platform timezone database."""

    class FoldTimezone(tzinfo):
        def utcoffset(self, dt):
            return timedelta(hours=-5 if dt is not None and dt.fold else -4)

    return FoldTimezone()
