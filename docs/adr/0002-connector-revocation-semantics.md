# ADR 0002 — Connector revocation semantics

**Status:** accepted, 2026-08-05
**Supersedes:** nothing. **Amends:** the in-flight clause of ADR 0001 I12.
**Resolves:** DOC-003 in `docs/DEBT.md`.

M1.4 proved that revocation blocks new use immediately (I12, gated by
`tests/verify_lifecycle.sh` section 3, 15 ms observed). It deliberately left
one question open: what happens to work already in progress. The gate
*reported* that outcome rather than asserting it, because asserting either way
would have committed to a product decision nobody had made.

This ADR makes it.

## Decision

> Standard revocation blocks new and queued work immediately but permits
> already-running invocations to finish under their accepted authority
> snapshot.
>
> Emergency revocation additionally requests cancellation, invalidates
> credentials, and requires execution-time revocation checks before further
> external actions.
>
> The platform does not claim guaranteed cancellation of provider-side
> operations that have already been accepted remotely.

Two operations, not one flag with a boolean. The distinction is the point:
most revocations are hygiene — a rotated key, a decommissioned integration, an
offboarded team — and terminating live work for those is a self-inflicted
outage. A suspected compromise is a different event and deserves a different
verb.

## Why automatic termination is not the default

Killing every in-flight invocation on revocation trades one risk for six:

| Risk | What it looks like |
|---|---|
| Partial external side effects | The provider charged the card; we cancelled before recording it |
| Inconsistent workflow state | A multi-step orchestration abandoned between steps |
| Duplicate retries after cancellation | The caller retries what was actually completed remotely |
| Uncancellable provider operations | We report "cancelled"; the provider disagrees |
| Ambiguous audit records | No answer to "did this run or not?" |
| False assurance | Revocation reports success while remote execution continues |

The last is the worst, because it is the failure that *looks* like the fix.
An operator who believes a compromised connector was stopped, when work is
still executing against it, is in a worse position than one who knows the
truth. Hence the guarantee below is scoped to what the platform actually
controls.

## The guarantee

> The platform ceases authorising further connector actions as soon as the
> revocation becomes authoritative.

Not "all activity stops". Authorisation is the thing we own; the provider's
already-accepted work is not. Any wording stronger than this is a claim we
cannot back, and stating a weaker true guarantee is worth more in an incident
than a stronger false one.

## Standard revocation

```
status                → revoked
new invocations       → rejected
queued invocations    → cancelled
running invocations   → allowed to finish
credentials           → unavailable for subsequent retries
```

### What holds today, and why

| Element | Status | Evidence |
|---|---|---|
| `status → revoked` | **implemented** | `lifecycle.TRANSITIONS`, migration 0008 |
| New invocations rejected | **implemented** | `invocations.check_executable`; `verify_lifecycle.sh` §3, failure-validated |
| Queued invocations cancelled | **vacuous today** | see below |
| Running invocations finish | **implemented** — this is current behaviour, now the intended behaviour | `verify_lifecycle.sh` §3 asserts it |
| Credentials unavailable for retries | **implemented, by construction** | see below |

**Queued invocations cancelled — vacuous today.** Execution is synchronous:
`invocations.accept()` commits `ACCEPTED` and `invocations.execute()`
immediately sets `RUNNING` inside the same request. There is no queue, no
worker pool, and no external actor positioned to cancel anything in that
window. The clause is not satisfied; there is nothing for it to be satisfied
*by*. It becomes load-bearing the moment asynchronous execution lands, which
is exactly when it will be easiest to forget — recorded as REV-001.

**Credentials unavailable for retries — by construction.** Nothing in this
service automatically re-attempts a failed invocation; `attempt` is always 1
and no retry loop exists. A caller retrying is issuing a *new* invocation,
which passes through `check_executable` and is refused. Secrets are resolved
inside the adapter, after that check. So the property holds, but it holds
because of an architectural absence rather than a control — if automatic
retry is ever added, it must re-check revocation before resolving secrets, or
this line becomes false silently. Recorded as REV-002.

**The authority snapshot.** `InvocationRecord.connector_version` is pinned at
acceptance and already exists — it is what "under their accepted authority
snapshot" refers to. A running invocation completes against the connector as
it was configured when it was accepted, not as it is now.

## Emergency revocation

**Status: NOT IMPLEMENTED.** Nothing in this section is built. It is recorded
here so the standard mode's limits are legible, and so the design is settled
before the incident that needs it.

```
status                    → revoked
new invocations           → rejected
queued invocations        → cancelled
running invocations       → cancellation requested
credentials               → invalidated or rotated immediately
workers                   → re-check revocation before each external action
non-cancellable executions → marked termination_requested
```

`termination_requested` is a distinct terminal-adjacent marker on purpose. An
execution we asked to stop and could not stop is neither "cancelled" nor
"completed", and forcing it into either loses the only fact an incident
responder needs.

### Revocation checkpoints

Multi-step adapters must re-check revocation:

```
before secret resolution
before each provider call
before retry
before following redirects
before committing a side-effecting step
```

Where these stand today, honestly:

| Checkpoint | Today |
|---|---|
| Before secret resolution | **satisfied** — `check_executable` runs before the adapter, which resolves secrets in `_headers` |
| Before each provider call | single call per invocation; satisfied trivially |
| Before retry | no retry path exists (REV-002) |
| Before following redirects | **NOT satisfied** — `security/outbound.request` follows redirects in a loop, revalidating the address at each hop but never re-checking revocation |
| Before a side-effecting step | no multi-step adapter exists |

The redirect loop is the one genuine multi-hop path in the system and the one
real gap. Under standard revocation it is *correct* for it to continue: the
invocation is running and running work finishes. Under emergency revocation
it would need a checkpoint. Recorded as REV-003.

## Fields and events

**Status: NOT IMPLEMENTED.** The shape is fixed here so the migration that
adds them does not relitigate it.

Fields on `connectors`:

```
revocation_mode              standard | emergency
revoked_at                   (exists)
revoked_by                   (today: actor_id on the audit record)
revocation_reason            (today: state_reason + audit reason)
cancel_queued
cancel_running_requested
credentials_invalidated_at
```

Three of these already exist in another form. They are listed anyway so the
implementer reconciles rather than duplicates — a second `revoked_by`
alongside the audit record's `actor_id` would be precisely the
two-sources-of-truth failure ADR 0001 exists to prevent.

Events:

```
connector.revoked                                   (exists)
connector.invocation.cancellation_requested
connector.invocation.cancelled
connector.invocation.completed_after_revocation
connector.invocation.cancellation_failed
```

`completed_after_revocation` is not an error event. Under standard revocation
it is the *expected* outcome, and it is what makes the policy auditable rather
than merely stated: without it, "did anything run after we revoked?" is
answerable only by correlating timestamps across two tables.

Audit record must preserve:

```
authority evaluated at
connector version
credential version
revocation observed at
last permitted external action
```

`connector version` exists on `InvocationRecord`. The rest do not.

## Consequences

`tests/verify_lifecycle.sh` §3 now **asserts** that an in-flight invocation
completes, rather than reporting it. That assertion is the policy; if someone
later makes revocation terminate running work, this ADR is what they must
amend first, and the test is what will tell them.

The residual-risk row in ADR 0001 reading "an in-flight invocation completes
after its connector is revoked — open, needs a decision" is closed by this
document. It is replaced by a narrower and permanent one: the platform does
not guarantee provider-side cancellation, and never will, because it cannot.
