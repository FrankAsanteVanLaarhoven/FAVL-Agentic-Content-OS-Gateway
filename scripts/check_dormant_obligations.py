#!/usr/bin/env python3
"""Tripwires for obligations that are dormant, not satisfied.

Three clauses of ADR 0002's standard revocation hold today only because the
capability they constrain does not exist:

    REV-001  "cancel queued invocations" — there is no queue
    REV-002  "credentials unavailable for retries" — there is no retry path
    REV-003  emergency-mode checkpoints — there is no emergency mode

A requirement that nothing can violate reads exactly like a requirement that
is met. The difference only becomes visible at the moment someone builds the
missing capability — which is the worst moment to discover it, because they
are busy building something else and the suite is green.

So each obligation is bound to its ACTIVATION CONDITION rather than left as
prose. When a trigger appears in the source, this check fails until the
corresponding control appears with it.

    python3 scripts/check_dormant_obligations.py
    python3 scripts/check_dormant_obligations.py --explain REV-002

What this is NOT: proof. These are source-pattern heuristics, and a
sufficiently creative implementation will evade them. They are a tripwire —
something that makes a dormant obligation impossible to walk past silently,
not something that verifies the control is correct once written. The test
that the control WORKS is a failure-injection test, and each obligation names
the one it needs before it may be closed.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "services" / "connector-registry"


@dataclass(frozen=True)
class Obligation:
    """One dormant requirement, its activation triggers, and its control."""

    id: str
    clause: str
    why_dormant: str
    triggers: dict[str, str]
    """Human-readable trigger -> regex that detects it in the source."""
    control: str
    """Regex proving the required control is present alongside the trigger."""
    control_description: str
    closes_when: str
    search: tuple[str, ...] = field(default=("app",))


OBLIGATIONS: tuple[Obligation, ...] = (
    Obligation(
        id="REV-001",
        clause="standard revocation cancels queued invocations",
        why_dormant=(
            "Not applicable under synchronous execution. accept() commits "
            "ACCEPTED and execute() sets RUNNING inside the same request, so "
            "no invocation is ever queued and nothing is positioned to cancel "
            "one. The clause is not satisfied — there is nothing for it to be "
            "satisfied by."
        ),
        triggers={
            "worker process or background dispatch": (
                r"asyncio\.create_task\(|BackgroundTasks|"
                r"def\s+worker|class\s+\w*Worker"
            ),
            "queue table or claim operation": (
                r"FOR UPDATE SKIP LOCKED|def\s+dequeue|def\s+claim_invocation|"
                r'__tablename__\s*=\s*"\w*queue'
            ),
            "invocation queue message subject": (
                r'"connector\.invocation\.(queued|dispatch)'
            ),
            "asynchronous retry scheduler": (
                r"def\s+schedule_retry|def\s+retry_scheduler|"
                r"next_attempt_at|scheduled_for"
            ),
        },
        # Deliberately NOT satisfiable by `check_executable(`, which already
        # exists on the synchronous path. Accepting it would have made this
        # obligation self-satisfying the instant a worker was added — the very
        # "reads as met because nothing violates it" failure this script
        # exists to catch, reproduced inside the catcher. The control must be
        # a named function someone chose to write.
        control=r"revocation_aware_dequeue",
        control_description=(
            "revocation-aware dequeue: the connector's executability must be "
            "re-read when an invocation leaves the queue, BEFORE it reaches "
            "RUNNING — not only when it was accepted"
        ),
        closes_when=(
            "an async path exists, cancels ACCEPTED invocations on revocation, "
            "and a live test revokes BETWEEN accept and start and asserts the "
            "invocation never runs — observed failing with the control removed"
        ),
    ),
    Obligation(
        id="REV-002",
        clause="credentials are unavailable for subsequent retries",
        why_dormant=(
            "True only because no internal retry mechanism exists. attempt is "
            "always 1 and a caller retrying issues a NEW invocation, which "
            "passes check_executable. This is an architectural absence, not a "
            "security control."
        ),
        triggers={
            "attempt is incremented": r"attempt\s*\+=|attempt\s*=\s*record\.attempt\s*\+",
            "retry scheduler": r"def\s+schedule_retry|def\s+retry_invocation",
            # Bound to the retry-DRIVER signature — reading retryable rows
            # back out of the database — not to the mere existence of the
            # status. The first version of this pattern matched the enum's own
            # declaration, `FAILED_RETRYABLE = "failed_retryable"`, and fired
            # on a codebase with no retry path at all. A tripwire that trips on
            # the definition of the thing it watches gets muted within a week,
            # and a muted tripwire is worse than none: it looks like coverage.
            "retryable status re-execution": (
                r"where\([^)]*FAILED_RETRYABLE|"
                r"InvocationRecord\.status\s*==\s*InvocationStatus\.FAILED_RETRYABLE"
            ),
            "delayed invocation task": r"def\s+delayed_invocation|run_after|delay_seconds",
            "provider backoff loop": r"def\s+backoff|exponential_backoff|for\s+_?attempt\s+in\s+range",
        },
        control=r"reauthorise_attempt|def\s+reauthorise",
        control_description=(
            "a reauthorise_attempt() that re-evaluates ALL of: connector "
            "status, tenant ownership, credential version, revocation "
            "timestamp, authority snapshot, deadline. A retry must not "
            "inherit credentials or authority merely because the first "
            "attempt was accepted"
        ),
        closes_when=(
            "the retry path re-authorises before resolving secrets, and a "
            "mutation removing that call kills a named test"
        ),
    ),
    Obligation(
        id="REV-003",
        clause="emergency revocation re-checks before each external action",
        why_dormant=(
            "There is no emergency mode, no actor able to trigger one, and no "
            "cancellation propagation. Under STANDARD revocation a running "
            "invocation is authorised to complete, so re-checking revocation "
            "between redirect hops would contradict the chosen semantics — "
            "the absence is correct, not an oversight."
        ),
        triggers={
            "an emergency revocation mode exists": (
                r"revocation_mode|EMERGENCY|emergency_revoke"
            ),
            "cancellation propagation": (
                r"cancellation_requested|termination_requested|"
                r'"connector\.invocation\.cancell'
            ),
            "credential invalidation": r"credentials_invalidated_at|def\s+invalidate_credential",
        },
        control=r"revocation_checkpoint",
        control_description=(
            "a revocation_checkpoint() consulted before secret resolution, "
            "each provider call, each retry, each redirect hop, and each "
            "side-effecting step — the redirect loop in security/outbound.py "
            "being the only genuine multi-hop path today"
        ),
        closes_when=(
            "emergency revocation exists AND a test proves interruption "
            "between redirect hops or provider calls — not merely that the "
            "checkpoint function is called"
        ),
    ),
)


def _sources(obligation: Obligation) -> list[Path]:
    files: list[Path] = []
    for relative in obligation.search:
        base = REGISTRY / relative
        if base.is_dir():
            files.extend(sorted(base.rglob("*.py")))
        elif base.is_file():
            files.append(base)
    return [f for f in files if "__pycache__" not in f.parts]


def evaluate(obligation: Obligation) -> tuple[list[str], bool]:
    """Return (fired triggers, whether the control is present)."""
    blob = "\n".join(
        f.read_text(encoding="utf-8", errors="replace") for f in _sources(obligation)
    )
    fired = [
        name
        for name, pattern in obligation.triggers.items()
        if re.search(pattern, blob)
    ]
    controlled = bool(re.search(obligation.control, blob))
    return fired, controlled


def explain(obligation: Obligation) -> None:
    print(f"{obligation.id} — {obligation.clause}\n")
    print(f"  Current status: DORMANT.\n    {obligation.why_dormant}\n")
    print("  Activation triggers:")
    for name in obligation.triggers:
        print(f"    - {name}")
    print(f"\n  Required control when activated:\n    {obligation.control_description}")
    print(f"\n  Closes when:\n    {obligation.closes_when}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--explain", metavar="ID", help="describe one obligation")
    parser.add_argument(
        "--list", action="store_true", help="show all obligations and their status"
    )
    args = parser.parse_args()

    by_id = {o.id: o for o in OBLIGATIONS}
    if args.explain:
        target = by_id.get(args.explain.upper())
        if target is None:
            print(f"unknown obligation: {args.explain}", file=sys.stderr)
            return 2
        explain(target)
        return 0

    print(f"checking {len(OBLIGATIONS)} dormant obligations\n")
    breached: list[str] = []

    for obligation in OBLIGATIONS:
        fired, controlled = evaluate(obligation)
        if not fired:
            print(f"  dormant  {obligation.id}  {obligation.clause}")
            continue
        if controlled:
            print(f"  ACTIVE   {obligation.id}  {obligation.clause}")
            print(f"           triggered by: {', '.join(fired)}")
            print("           control present — close the item once its")
            print(f"           failure-injection test exists: {obligation.closes_when}")
            continue
        breached.append(obligation.id)
        print(f"  BREACHED {obligation.id}  {obligation.clause}")
        print(f"           triggered by: {', '.join(fired)}")
        print(f"           MISSING: {obligation.control_description}")

    print()
    if breached:
        print(
            f"FAILED: {len(breached)} dormant obligation(s) activated without control"
        )
        print()
        print("A capability was added that one of ADR 0002's clauses constrains,")
        print("and the clause is now unenforced rather than inapplicable. Either")
        print("add the control, or amend ADR 0002 to say the clause is")
        print("deliberately deferred — with the date and the reason. Do not")
        print("delete the trigger to make this pass.")
        return 1

    print(f"all {len(OBLIGATIONS)} obligations still dormant — nothing activated them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
