"""Shared fixtures for the SimpleAudit test suite."""

import pytest

from simpleaudit.utils import image_data_uri


@pytest.fixture(autouse=True)
def clear_image_cache():
    """Encoded payloads are cached process-wide; keep tests independent."""
    image_data_uri.cache_clear()
    yield
    image_data_uri.cache_clear()
