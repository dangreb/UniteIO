import pytest

from uniteio import Runtime


@pytest.fixture(autouse=True)
def close_default_runtime():
    """Close the process-wide runtime if a test created it."""
    yield
    if Runtime._default is not None:
        Runtime._default.close()
        Runtime._default = None
