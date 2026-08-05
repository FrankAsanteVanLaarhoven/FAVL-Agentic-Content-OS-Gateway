"""Connector lifecycle as an explicit state machine.

Every transition is a table entry, not a hand-written endpoint. A collection
of loosely related endpoints lets a state pair exist that nobody considered —
the way `deletion_requested` and the old `enabled` boolean could disagree
about whether a connector might run.

The security-critical rule here:

    REVOCATION MUST PREVENT NEW USE IMMEDIATELY.

Not "once consumers converge", not "after a cache expires". `is_executable`
is evaluated at invocation time against the row the invocation reads inside
its own transaction, so a connector revoked between two requests cannot serve
the second. Nothing in this module is cached, and nothing about executability
is derived from an event — events describe what happened, they do not decide
what is permitted. See ADR 0001 I12.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConnectorState(str, Enum):
    """Lifecycle states, in the order a healthy connector moves through them."""

    DRAFT = "draft"
    INSTALLED = "installed"
    CONFIGURED = "configured"
    VALIDATED = "validated"
    ENABLED = "enabled"
    DISABLED = "disabled"
    REVOKED = "revoked"
    DELETION_REQUESTED = "deletion_requested"
    ARCHIVED = "archived"
    DELETED = "deleted"


# The ONLY state in which a connector may serve an invocation.
#
# Expressed as a set of one rather than a boolean on the row, so adding a
# state cannot accidentally make it executable: a new state is non-executable
# until someone puts it here deliberately.
EXECUTABLE_STATES: frozenset[ConnectorState] = frozenset({ConnectorState.ENABLED})

# States with no exit at all. Only DELETED qualifies: ARCHIVED deliberately
# retains one privileged, retention-gated edge to DELETED, so calling it
# terminal was a contradiction the state-machine tests caught immediately —
# the table routed out of a state the constants said was final.
TERMINAL_STATES: frozenset[ConnectorState] = frozenset({ConnectorState.DELETED})

# Revocation is deliberately NOT terminal-looking but is one-way: a revoked
# credential cannot be un-revoked, only replaced by a new connector. Allowing
# revoked -> enabled would mean a compromised connector could be restored
# without the credential rotation that made it safe again.
ONE_WAY_INTO: frozenset[ConnectorState] = frozenset(
    {ConnectorState.REVOKED, ConnectorState.ARCHIVED, ConnectorState.DELETED}
)


class TransitionError(Exception):
    """A transition that the machine does not permit."""

    def __init__(self, frm: str, to: str, reason: str) -> None:
        super().__init__(f"{frm} -> {to}: {reason}")
        self.frm = frm
        self.to = to
        self.reason = reason


@dataclass(frozen=True)
class Transition:
    """One permitted edge.

    `idempotent` marks a transition whose target state, if already current,
    is a success rather than a conflict — repeating a disable or a deletion
    request must not fail, because an operator retrying after a timeout has
    no way to know whether the first attempt landed.

    `requires_reason` marks transitions whose motive is operationally
    significant. A revocation with no reason is an audit record that cannot
    answer the only question anyone will ask of it later.
    """

    source: ConnectorState
    target: ConnectorState
    event: str
    idempotent: bool = False
    requires_reason: bool = False
    privileged: bool = False


TRANSITIONS: tuple[Transition, ...] = (
    # Installation and configuration.
    Transition(ConnectorState.DRAFT, ConnectorState.INSTALLED, "connector.installed"),
    Transition(
        ConnectorState.INSTALLED, ConnectorState.CONFIGURED, "connector.configured"
    ),
    # Reconfiguration is allowed from any pre-enabled state and from disabled,
    # and always drops back to CONFIGURED so validation must run again. A
    # config change that skipped revalidation would let an invalid connector
    # keep its validated status.
    Transition(
        ConnectorState.CONFIGURED, ConnectorState.CONFIGURED, "connector.configured"
    ),
    Transition(
        ConnectorState.VALIDATED, ConnectorState.CONFIGURED, "connector.configured"
    ),
    Transition(
        ConnectorState.DISABLED, ConnectorState.CONFIGURED, "connector.configured"
    ),
    # Validation.
    Transition(
        ConnectorState.CONFIGURED,
        ConnectorState.VALIDATED,
        "connector.validation_succeeded",
    ),
    Transition(
        ConnectorState.CONFIGURED,
        ConnectorState.CONFIGURED,
        "connector.validation_failed",
    ),
    # Only a validated connector may be enabled. This is the edge that stops
    # an unvalidated configuration reaching a provider.
    Transition(ConnectorState.VALIDATED, ConnectorState.ENABLED, "connector.enabled"),
    Transition(
        ConnectorState.DISABLED,
        ConnectorState.ENABLED,
        "connector.enabled",
        idempotent=False,
    ),
    # Suspension is reversible and idempotent.
    Transition(
        ConnectorState.ENABLED,
        ConnectorState.DISABLED,
        "connector.disabled",
        idempotent=True,
        requires_reason=True,
    ),
    # Credential rotation does not change state; it is recorded because the
    # audit trail must show when a secret last changed.
    Transition(
        ConnectorState.ENABLED,
        ConnectorState.ENABLED,
        "connector.credentials_rotated",
    ),
    Transition(
        ConnectorState.DISABLED,
        ConnectorState.DISABLED,
        "connector.credentials_rotated",
    ),
    # Revocation. Reachable from any live state, one-way, and always reasoned.
    *(
        Transition(
            source,
            ConnectorState.REVOKED,
            "connector.revoked",
            idempotent=True,
            requires_reason=True,
        )
        for source in (
            ConnectorState.DRAFT,
            ConnectorState.INSTALLED,
            ConnectorState.CONFIGURED,
            ConnectorState.VALIDATED,
            ConnectorState.ENABLED,
            ConnectorState.DISABLED,
        )
    ),
    # Deletion request follows suspension or revocation, never a live state:
    # asking to delete something still serving traffic hides the suspension
    # step that should have come first.
    Transition(
        ConnectorState.DISABLED,
        ConnectorState.DELETION_REQUESTED,
        "connector.deletion_requested",
        idempotent=True,
        requires_reason=True,
    ),
    Transition(
        ConnectorState.REVOKED,
        ConnectorState.DELETION_REQUESTED,
        "connector.deletion_requested",
        idempotent=True,
        requires_reason=True,
    ),
    # Archival retains the identity and the audit trail. This is the normal
    # end state; physical deletion is not.
    Transition(
        ConnectorState.DELETION_REQUESTED,
        ConnectorState.ARCHIVED,
        "connector.archived",
        idempotent=True,
    ),
    Transition(
        ConnectorState.REVOKED,
        ConnectorState.ARCHIVED,
        "connector.archived",
        idempotent=True,
    ),
    # Physical deletion is privileged and retention-gated; the row itself is
    # usually better kept as an anonymised tombstone, because invocation
    # history and provenance reference the identity.
    Transition(
        ConnectorState.ARCHIVED,
        ConnectorState.DELETED,
        "connector.deleted",
        requires_reason=True,
        privileged=True,
    ),
)

_BY_EDGE: dict[tuple[ConnectorState, ConnectorState], list[Transition]] = {}
for _t in TRANSITIONS:
    _BY_EDGE.setdefault((_t.source, _t.target), []).append(_t)


def is_executable(state: str) -> bool:
    """Whether a connector in this state may serve an invocation.

    Called at invocation time against freshly read state. A revoked or
    disabled connector stops serving on the very next request; there is no
    cache to expire and no consumer to converge.
    """
    try:
        return ConnectorState(state) in EXECUTABLE_STATES
    except ValueError:
        # An unrecognised state is not executable. A schema migration that
        # introduces one must not accidentally open the gate.
        return False


def find(source: str, target: str, event: str | None = None) -> Transition:
    """Look up a permitted transition, or explain why there is none."""
    try:
        frm, to = ConnectorState(source), ConnectorState(target)
    except ValueError as exc:
        raise TransitionError(source, target, f"unknown state: {exc}") from None

    if frm in TERMINAL_STATES and frm != to:
        raise TransitionError(source, target, f"{frm.value} is terminal")

    candidates = _BY_EDGE.get((frm, to), [])
    if event is not None:
        candidates = [c for c in candidates if c.event == event]
    if not candidates:
        raise TransitionError(source, target, "not a permitted transition")
    return candidates[0]


def permitted_targets(source: str) -> list[str]:
    """States reachable from here, for a 409 that tells the caller what to do."""
    try:
        frm = ConnectorState(source)
    except ValueError:
        return []
    return sorted({t.target.value for t in TRANSITIONS if t.source == frm})


def is_idempotent_repeat(current: str, target: str) -> bool:
    """Whether re-requesting a state the connector already holds is a success.

    An operator retrying after a timeout cannot know whether the first
    attempt landed, so a repeated disable or revoke returns success rather
    than a conflict. Enabling is deliberately excluded: re-enabling is a
    decision, and a caller who thinks a connector is disabled when it is
    enabled should learn that.
    """
    if current != target:
        return False
    return any(
        t.idempotent for t in TRANSITIONS if t.target.value == target and t.idempotent
    )
