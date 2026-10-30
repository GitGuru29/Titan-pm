"""Arch-compatible version comparison.

Implements ``epoch:pkgver-pkgrel`` parsing and alternating numeric/alpha
segment comparison following ``docs/IMPLEMENTATION-PLAN.md``:

- ``epoch`` defaults to ``0``, ``pkgrel`` defaults to ``0`` when absent;
- numeric segments compare by value (length after stripping leading zeros,
  then lexicographically) and sort newer than alpha segments;
- ``~`` sorts older than everything (including end-of-version);
- ``^`` sorts newer than end-of-version, older than regular suffixes;
- ``+`` and ``_`` post-release separators sort newer than ``.``.

The returned value is ``-1`` (``a`` older), ``0`` (equal) or ``1`` (``a`` newer).
"""

from __future__ import annotations

_SEP_ORDER = {".": 0, "+": 1, "_": 2}


def _is_alnum(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _is_alpha(char: str) -> bool:
    return char.isascii() and char.isalpha()


def _is_digit(char: str) -> bool:
    return char.isascii() and char.isdigit()


def _sep_key(sep: str) -> tuple[int, tuple[int, ...]]:
    return (len(sep), tuple(_SEP_ORDER.get(ch, ord(ch)) for ch in sep))


def _rpmvercmp(a: str, b: str) -> int:
    """Compare two version fragments using segment and pre/post-release rules."""
    if a == b:
        return 0

    len_a = len(a)
    len_b = len(b)
    one = 0
    two = 0

    while one < len_a or two < len_b:
        a_tilde = one < len_a and a[one] == "~"
        b_tilde = two < len_b and b[two] == "~"
        if a_tilde or b_tilde:
            if not a_tilde:
                return 1
            if not b_tilde:
                return -1
            one += 1
            two += 1
            continue

        a_caret = one < len_a and a[one] == "^"
        b_caret = two < len_b and b[two] == "^"
        if a_caret or b_caret:
            if one >= len_a:
                return -1
            if two >= len_b:
                return 1
            if not a_caret:
                return 1
            if not b_caret:
                return -1
            one += 1
            two += 1
            continue

        sep_a_start = one
        sep_b_start = two
        while one < len_a and not _is_alnum(a[one]) and a[one] not in ("~", "^"):
            one += 1
        while two < len_b and not _is_alnum(b[two]) and b[two] not in ("~", "^"):
            two += 1

        if (one < len_a and a[one] in ("~", "^")) or (
            two < len_b and b[two] in ("~", "^")
        ):
            continue

        if not (one < len_a and two < len_b):
            break

        sep_a = a[sep_a_start:one]
        sep_b = b[sep_b_start:two]
        if sep_a != sep_b:
            return -1 if _sep_key(sep_a) < _sep_key(sep_b) else 1

        ptr1 = one
        ptr2 = two

        if _is_digit(a[ptr1]):
            while ptr1 < len_a and _is_digit(a[ptr1]):
                ptr1 += 1
            while ptr2 < len_b and _is_digit(b[ptr2]):
                ptr2 += 1
            is_num = True
        else:
            while ptr1 < len_a and _is_alpha(a[ptr1]):
                ptr1 += 1
            while ptr2 < len_b and _is_alpha(b[ptr2]):
                ptr2 += 1
            is_num = False

        seg1 = a[one:ptr1]
        seg2 = b[two:ptr2]

        if not seg1:
            return -1
        if not seg2:
            return 1 if is_num else -1

        if is_num:
            seg1 = seg1.lstrip("0")
            seg2 = seg2.lstrip("0")
            if len(seg1) > len(seg2):
                return 1
            if len(seg2) > len(seg1):
                return -1

        if seg1 != seg2:
            return -1 if seg1 < seg2 else 1

        one = ptr1
        two = ptr2

    if one >= len_a and two >= len_b:
        return 0
    return -1 if one >= len_a else 1


def parse_evr(evr: str) -> tuple[str, str, str | None]:
    """Split ``[epoch:]version[-release]``."""
    index = 0
    while index < len(evr) and _is_digit(evr[index]):
        index += 1

    if index < len(evr) and evr[index] == ":":
        epoch = evr[:index] or "0"
        rest = evr[index + 1 :]
    else:
        epoch = "0"
        rest = evr

    release_index = rest.rfind("-")
    if release_index == -1:
        return epoch, rest, None
    return epoch, rest[:release_index], rest[release_index + 1 :]


def vercmp(a: str, b: str) -> int:
    """Compare two ``epoch:pkgver-pkgrel`` strings."""
    if a == b:
        return 0

    epoch_a, version_a, release_a = parse_evr(a)
    epoch_b, version_b, release_b = parse_evr(b)

    result = _rpmvercmp(epoch_a, epoch_b)
    if result != 0:
        return result

    result = _rpmvercmp(version_a, version_b)
    if result != 0:
        return result

    if release_a is None and release_b is None:
        return 0
    return _rpmvercmp(release_a or "0", release_b or "0")
