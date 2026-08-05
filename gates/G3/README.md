# G3 — connector lifecycle state machine (M1.4)

Evidence for the milestone gate: **revocation prevents new use immediately.**

| File | What it proves |
|---|---|
| `verify_lifecycle.sh.txt` | 41 assertions against the live stack, through APISIX, with a real token |
| `mutations.txt` | 25/25 mutations killed, 8 of them on the lifecycle |
| `revocation_failure_validated.txt` | The gate observed FAILING with the defect reintroduced |
| `migration_rollback.txt` | 0008 down to 0007 and back to head |

## What the gate actually tested

Section 3 of `verify_lifecycle.sh` starts a six-second invocation, revokes the
connector 1.5 s in — while the first request is still on the wire — and issues
a second invocation. The second is refused with 403 `CONNECTOR_REVOKED`,
observed 15 ms after the revoke response. No cache flush, no worker restart,
no consumer convergence.

## What it deliberately did not test

The in-flight invocation **completes** (200). Revocation prevents new use;
it does not cancel work already accepted. The script reports that outcome
rather than asserting it, because asserting either way would imply a decision
that has not been made. Recorded as residual risk in ADR 0001.

## Failure validation

`EXECUTABLE_STATES` was widened to include `REVOKED`, the service rebuilt, and
the gate re-run. It failed as it should:

    FAIL  NEXT invocation is refused          got=200 want=403
    FAIL  refusal distinguishes revoked       got=None want=CONNECTOR_REVOKED

The defect was reverted and the suite returned to 41/41. A gate never watched
failing is a gate that has not been tested.

## Defects this gate found that the unit tests did not

1. `transitions.apply` searched for an idempotent self-edge rather than an
   idempotent edge into the target, so every retried disable and revoke
   returned 409. All unit tests were green.
2. The console's `ConnectorStatus` union still listed five states while the
   API returned ten, and its hand-written test list drifted with it. Now a
   compile-time exhaustiveness check, failure-validated.
