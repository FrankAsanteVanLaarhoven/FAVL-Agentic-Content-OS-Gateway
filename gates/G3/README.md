# G3 — connector lifecycle state machine (M1.4)

Evidence for the milestone gate: **revocation prevents new use immediately.**

| File | What it proves |
|---|---|
| `verify_lifecycle.sh.txt` | 42 assertions against the live stack, through APISIX, with a real token |
| `mutations.txt` | 25/25 mutations killed, 8 of them on the lifecycle |
| `revocation_failure_validated.txt` | The gate observed FAILING with the defect reintroduced |
| `migration_rollback.txt` | 0008 down to 0007 and back to head |
| `verify_outbox_regression.txt` | M1.2's delivery guarantee still holds under M1.4 |

## What the gate actually tested

Section 3 of `verify_lifecycle.sh` starts a six-second invocation, revokes the
connector 1.5 s in — while the first request is still on the wire — and issues
a second invocation. The second is refused with 403 `CONNECTOR_REVOKED`,
observed 15 ms after the revoke response. No cache flush, no worker restart,
no consumer convergence.

## The other half of the policy

The in-flight invocation **completes** (200), and since ADR 0002 that is
asserted rather than reported. Standard revocation blocks new use and lets
running work finish under the authority snapshot pinned at acceptance.

Both directions are gated deliberately. A revocation that terminated running
work would be as much a departure from the policy as one that kept serving new
requests — and the more tempting mistake, because it looks stricter.

The platform does not claim to cancel provider-side work already accepted
remotely. Emergency revocation, which requests cancellation, is designed in
ADR 0002 and NOT implemented.

## Failure validation

`EXECUTABLE_STATES` was widened to include `REVOKED`, the service rebuilt, and
the gate re-run. It failed as it should:

    FAIL  NEXT invocation is refused          got=200 want=403
    FAIL  refusal distinguishes revoked       got=None want=CONNECTOR_REVOKED

The defect was reverted and the suite returned to 41/41. A gate never watched
failing is a gate that has not been tested.

## Regression check on M1.2

Every lifecycle transition now stages an outbox event and an audit row in the
same transaction as the state change, so M1.4 writes into the machinery M1.2
gated. `verify_outbox_regression.txt` is a full re-run of `verify_outbox.sh`
with all M1.4 code deployed — 22/22 across all seven sections, including four
SIGKILLs during a 300-write load. A milestone that quietly weakened the
delivery guarantee while passing its own tests is the failure mode this
rules out.

## Defects this gate found that the unit tests did not

1. `transitions.apply` searched for an idempotent self-edge rather than an
   idempotent edge into the target, so every retried disable and revoke
   returned 409. All unit tests were green.
2. The console's `ConnectorStatus` union still listed five states while the
   API returned ten, and its hand-written test list drifted with it. Now a
   compile-time exhaustiveness check, failure-validated.
