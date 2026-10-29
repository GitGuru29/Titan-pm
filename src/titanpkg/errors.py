"""Exception hierarchy for titanpkg.

Every user-facing failure should raise a :class:`TitanError` subclass so the CLI
can map it to a stable exit code (see :mod:`titanpkg.cli`).
"""

from __future__ import annotations


class TitanError(Exception):
    """Base class for all titanpkg failures."""

    exit_code = 1


class UsageError(TitanError):
    """Invalid command line or configuration."""

    exit_code = 2


class ResolutionError(TitanError):
    """Dependencies, conflicts or versions could not be resolved."""

    exit_code = 3


class SecurityError(TitanError):
    """Signature, hash, path or ownership policy violation."""

    exit_code = 4


class TransactionError(TitanError):
    """A filesystem mutation failed and was rolled back."""

    exit_code = 5


class DatabaseError(TitanError):
    """The installed database is missing, corrupt or locked."""


class RepositoryError(TitanError):
    """A repository index or package could not be fetched or parsed."""
