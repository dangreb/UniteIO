import pytest

from uio import UIOPool
from uio.uniteio import UIOMeta, UIOPoolMeta, UIOTask


@pytest.fixture(autouse=True)
def clean_uio_state():
    """Keep singleton applications and executors isolated between tests."""
    yield

    pool = UIOPoolMeta._instances.get(UIOPool)
    if pool is not None:
        pool.shutdown(cancel_futures=True)

    # These should already be empty after pool shutdown. Clearing them here
    # also keeps a failed construction from contaminating the next test.
    UIOMeta._instances.clear()
    UIOTask._coroarg.clear()

