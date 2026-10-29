import pytest

from titanpkg.vercmp import vercmp

pytestmark = pytest.mark.skip(reason="vercmp is implemented in Phase 1")


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.0", "1.0", 0),
        ("1.0", "1.0-1", -1),
        ("1.0-1", "1.0-2", -1),
        ("1.0-2", "1.0-1", 1),
        ("1.0", "1.1", -1),
        ("1.0a", "1.0", 1),
        ("1.0~rc1", "1.0", -1),
        ("1:1.0", "2.0", 1),
        ("1.0+1", "1.0.1", 1),
        ("1.0_1", "1.0.1", 1),
    ],
)
def test_vercmp(left, right, expected):
    assert vercmp(left, right) == expected
    assert vercmp(right, left) == -expected
