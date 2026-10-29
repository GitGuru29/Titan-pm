import pytest

pytestmark = pytest.mark.skip(reason="transaction engine is implemented in Phase 3")


def test_install_upgrade_rollback_remove(tmp_path):
    """End-to-end contract for Phase 3: local file:// repo into a temp root."""
    raise AssertionError("implemented in Phase 3")
