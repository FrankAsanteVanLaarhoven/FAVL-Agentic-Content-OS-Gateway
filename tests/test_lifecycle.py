"""Connector lifecycle state machine.

The properties here are the ones a collection of endpoints would let drift:
that no state is executable by accident, that revocation is one-way, and that
every reachable state is actually reachable.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "connector-registry"))

from app.lifecycle import (  # noqa: E402
    EXECUTABLE_STATES,
    ONE_WAY_INTO,
    TERMINAL_STATES,
    TRANSITIONS,
    ConnectorState,
    TransitionError,
    find,
    is_executable,
    is_idempotent_repeat,
    permitted_targets,
)


class TestExecutability:
    def test_only_enabled_may_serve_an_invocation(self):
        for state in ConnectorState:
            assert is_executable(state.value) == (state is ConnectorState.ENABLED), (
                f"{state.value} executability is wrong"
            )

    def test_an_unknown_state_is_not_executable(self):
        """A migration that adds a state must not open the gate by default."""
        assert not is_executable("some_future_state")
        assert not is_executable("")

    def test_revoked_cannot_serve(self):
        # The M1.4 security invariant, at its narrowest: revocation stops use.
        assert not is_executable(ConnectorState.REVOKED.value)

    def test_executable_set_is_a_set_not_a_boolean(self):
        """Adding a state must not silently make it executable."""
        assert frozenset({ConnectorState.ENABLED}) == EXECUTABLE_STATES


class TestTransitions:
    def test_a_connector_must_be_validated_before_it_can_be_enabled(self):
        # The edge that stops an unvalidated configuration reaching a provider.
        find(ConnectorState.VALIDATED.value, ConnectorState.ENABLED.value)
        for source in (
            ConnectorState.DRAFT,
            ConnectorState.INSTALLED,
            ConnectorState.CONFIGURED,
        ):
            with pytest.raises(TransitionError):
                find(source.value, ConnectorState.ENABLED.value)

    def test_reconfiguration_forces_revalidation(self):
        """A config change must not keep a stale validated status."""
        transition = find(
            ConnectorState.VALIDATED.value, ConnectorState.CONFIGURED.value
        )
        assert transition.event == "connector.configured"
        with pytest.raises(TransitionError):
            find(ConnectorState.CONFIGURED.value, ConnectorState.ENABLED.value)

    def test_revocation_is_one_way(self):
        """Restoring a revoked connector would skip credential rotation."""
        for target in ConnectorState:
            if target is ConnectorState.REVOKED:
                continue
            if target in (
                ConnectorState.DELETION_REQUESTED,
                ConnectorState.ARCHIVED,
            ):
                continue  # the only permitted exits
            with pytest.raises(TransitionError):
                find(ConnectorState.REVOKED.value, target.value)

    def test_terminal_states_have_no_exit(self):
        assert frozenset({ConnectorState.DELETED}) == TERMINAL_STATES
        for terminal in TERMINAL_STATES:
            for target in ConnectorState:
                if target is terminal:
                    continue
                with pytest.raises(TransitionError, match="terminal"):
                    find(terminal.value, target.value)

    def test_archived_retains_exactly_one_privileged_exit(self):
        """Archival is the normal end state; deletion is a further decision."""
        targets = permitted_targets(ConnectorState.ARCHIVED.value)
        assert targets == ["deleted"]
        assert find(
            ConnectorState.ARCHIVED.value, ConnectorState.DELETED.value
        ).privileged

    def test_deletion_cannot_be_requested_for_a_live_connector(self):
        """Deleting something still serving hides the suspension step."""
        for source in (
            ConnectorState.ENABLED,
            ConnectorState.VALIDATED,
            ConnectorState.CONFIGURED,
        ):
            with pytest.raises(TransitionError):
                find(source.value, ConnectorState.DELETION_REQUESTED.value)

    def test_physical_deletion_is_privileged_and_reasoned(self):
        transition = find(ConnectorState.ARCHIVED.value, ConnectorState.DELETED.value)
        assert transition.privileged
        assert transition.requires_reason

    def test_every_revocation_and_suspension_demands_a_reason(self):
        """Only state CHANGES into a suspension state need a motive.

        A credentials_rotated record whose target happens to be DISABLED is
        not a suspension — the connector was already suspended — so requiring
        a reason there would be noise.
        """
        for transition in TRANSITIONS:
            if transition.source == transition.target:
                continue
            if transition.target in (
                ConnectorState.REVOKED,
                ConnectorState.DISABLED,
                ConnectorState.DELETION_REQUESTED,
            ):
                assert transition.requires_reason, (
                    f"{transition.source.value} -> {transition.target.value} "
                    "records no reason; the audit entry cannot answer why"
                )

    def test_an_unknown_state_is_rejected_not_ignored(self):
        with pytest.raises(TransitionError, match="unknown state"):
            find("nonsense", ConnectorState.ENABLED.value)


class TestReachability:
    def test_every_state_is_reachable_from_draft(self):
        """A state nothing can reach is dead code pretending to be a feature."""
        reached = {ConnectorState.DRAFT}
        changed = True
        while changed:
            changed = False
            for transition in TRANSITIONS:
                if transition.source in reached and transition.target not in reached:
                    reached.add(transition.target)
                    changed = True
        unreachable = set(ConnectorState) - reached
        assert not unreachable, f"unreachable: {[s.value for s in unreachable]}"

    def test_no_transition_leaves_a_terminal_state(self):
        for transition in TRANSITIONS:
            if transition.source in TERMINAL_STATES:
                assert transition.source == transition.target, (
                    f"{transition.source.value} is terminal but routes to "
                    f"{transition.target.value}"
                )

    def test_one_way_states_are_never_exited_into_a_live_state(self):
        live = {
            ConnectorState.DRAFT,
            ConnectorState.INSTALLED,
            ConnectorState.CONFIGURED,
            ConnectorState.VALIDATED,
            ConnectorState.ENABLED,
            ConnectorState.DISABLED,
        }
        for transition in TRANSITIONS:
            if transition.source in ONE_WAY_INTO:
                assert transition.target not in live, (
                    f"{transition.source.value} -> {transition.target.value} "
                    "returns a one-way state to service"
                )

    def test_permitted_targets_guides_a_rejected_caller(self):
        targets = permitted_targets(ConnectorState.ENABLED.value)
        assert "disabled" in targets
        assert "revoked" in targets
        assert permitted_targets("nonsense") == []


class TestIdempotency:
    @pytest.mark.parametrize("state", ["disabled", "revoked", "deletion_requested"])
    def test_repeating_a_suspension_is_a_success(self, state):
        # An operator retrying after a timeout cannot know whether the first
        # attempt landed.
        assert is_idempotent_repeat(state, state)

    def test_repeating_enable_is_not_idempotent(self):
        """Re-enabling is a decision, and a surprised caller should learn."""
        assert not is_idempotent_repeat("enabled", "enabled")

    def test_a_different_target_is_never_an_idempotent_repeat(self):
        assert not is_idempotent_repeat("enabled", "disabled")
