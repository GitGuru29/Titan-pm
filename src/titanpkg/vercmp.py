"""Arch-compatible version comparison.

This is a faithful port of libalpm's ``rpmvercmp`` / ``parseEVR`` logic from
pacman (``lib/libalpm/version.c``).  It intentionally keeps libalpm's quirks:

- epoch defaults to ``0`` and is only parsed when the version starts with
  ``<digits>:``;
- the release is the text after the **last** hyphen;
- if either side has no release, releases are not compared at all;
- ``~`` is *not* special (it is just a separator, unlike RPM).

The returned value is ``-1`` (``a`` older), ``0`` (equal) or ``1`` (``a`` newer).
"""

from __future__ import annotations


def _is_alnum(char: str) -> bool:
    return char.isascii() and char.isalnum()


def _is_alpha(char: str) -> bool:
    return char.isascii() and char.isalpha()


def _is_digit(char: str) -> bool:
    return char.isascii() and char.isdigit()


def _rpmvercmp(a: str, b: str) -> int:
    """Compare two version fragments using libalpm's segment rules."""
    if a == b:
        return 0

    len_a = len(a)
    len_b = len(b)
    one = 0
    two = 0
    ptr1 = 0
    ptr2 = 0

    while one < len_a and two < len_b:
        while one < len_a and not _is_alnum(a[one]):
            one += 1
        while two < len_b and not _is_alnum(b[two]):
            two += 1

        if not (one < len_a and two < len_b):
            break

        if (one - ptr1) != (two - ptr2):
            return -1 if (one - ptr1) < (two - ptr2) else 1

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

    one_empty = one >= len_a
    two_empty = two >= len_b
    one_is_alpha = not one_empty and _is_alpha(a[one])
    two_is_alpha = not two_empty and _is_alpha(b[two])

    if (one_empty and not two_is_alpha) or one_is_alpha:
        return -1
    return 1


def parse_evr(evr: str) -> tuple[str, str, str | None]:
    """Split ``[epoch:]version[-release]`` the way libalpm does."""
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

    if release_a is not None and release_b is not None:
        return _rpmvercmp(release_a, release_b)
    return 0
