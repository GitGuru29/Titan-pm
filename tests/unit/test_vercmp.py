import pytest

from titanpkg.vercmp import parse_evr, vercmp


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
        ("1.0~alpha", "1.0~beta", -1),
        ("1.0^git1", "1.0", 1),
        ("1.0^git1", "1.0.1", -1),
        ("1:1.0", "2.0", 1),
        ("0:1.0", "1.0", 0),
        ("01.002", "1.2", 0),
        ("1.0+1", "1.0.1", 1),
        ("1.0_1", "1.0.1", 1),
        ("3.1.0-1", "3.1.0-2", -1),
    ],
)
def test_vercmp(left, right, expected):
    assert vercmp(left, right) == expected
    assert vercmp(right, left) == -expected


def test_parse_evr():
    assert parse_evr("1.0") == ("0", "1.0", None)
    assert parse_evr("3.1.0-1") == ("0", "3.1.0", "1")
    assert parse_evr("2:1.4.0-3") == ("2", "1.4.0", "3")
