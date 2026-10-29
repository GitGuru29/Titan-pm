"""Staged, reversible filesystem transactions (Phase 3).

Lifecycle::

    plan -> preflight -> download -> stage -> backup -> commit -> rollback
"""

from __future__ import annotations

# TODO(phase-3): implement transaction engine, conffile handling, scriptlets.
