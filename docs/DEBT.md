# Engineering debt

Tracked work with an owner and a completion criterion. An item without both
is a wish, not debt. Items are closed only when the criterion is met, not when
the work feels done.

## DOC-001 — Restructure ADR 0001 into the eight-field format

**Owner:** Frank Asante Van Laarhoven
**Blocks:** the next security review (not M1.4)
**Raised:** 2026-08-05

The ADR carries all of the content but as prose per invariant, so a reviewer
must read a section to answer "what detects this?". The eight-field structure
makes each invariant independently auditable:

```
Invariant · Threat · Historical failure · Detection mechanism · Mitigation ·
Regression test · Operational alert · Residual risk
```

**Done when:** every invariant I1–I12 carries all eight fields, each field is
either populated or explicitly marked `none`, and no field is populated with a
claim that has no artefact behind it. A `Regression test` field naming a test
that has never been observed to fail must say so.

**Scope grew on 2026-08-05.** I12 (revocation immediacy) was added in the
existing prose style rather than the eight-field one, because restructuring
thirteen invariants was explicitly out of scope for M1.4 and half a
restructure is worse than none — a reader cannot tell whether an absent field
means "not applicable" or "not yet converted". I12 is the best-evidenced
invariant in the document (two observed failures, a live gate, eight
mutations), so it is the natural one to convert first and use as the
template.

## DOC-002 — Failure-mode coverage matrix

**Owner:** Frank Asante Van Laarhoven
**Blocks:** the next security review (not M1.4)
**Raised:** 2026-08-05

`OutboxStalledWhileWriting` was deleted on a redundancy argument that was
reasoned rather than tabulated. The argument was almost certainly right, but
"almost certainly" is the state the matrix exists to remove — and a matrix
would also expose failure modes with *no* detector, which is the more useful
direction.

**Done when:** every material failure mode has at least one row recording
detector, whether the failure has been injected, whether the detector fired,
and whether recovery was verified. A row with `Injected: No` is a gap, not a
pass. Current known-good rows, to be verified rather than assumed:

| Failure mode | Detector | Injected? | Fires? | Recovery verified? |
|---|---|---|---|---|
| Publisher stopped | oldest-pending age | yes | yes | yes |
| NATS unavailable | readiness + backlog age | yes | partial | yes |
| Database unavailable | readiness | no | — | — |
| Outbox index broken | verify_outbox section 0 | yes | yes | n/a |
| Metrics frozen | scrape-derived gauges | yes | yes | yes |
| Rules not reloaded | runtime rule comparison | yes | yes | yes |
| Tenant bypass | mutation suite + verify_identity | yes | yes | n/a |
| SSRF pin removed | mutation suite | yes | yes | n/a |
| Slow publisher (not stopped) | none identified | no | — | — |
| Locked transaction | none identified | no | — | — |
| Revoked connector keeps serving | verify_lifecycle.sh section 3 | yes | yes | yes |
| Audit record rewritten | database trigger | yes | yes | n/a |
| Connector deleted under its audit trail | FK RESTRICT | yes | yes | n/a |
| Migration 0008 rolled back | alembic downgrade/upgrade | yes | n/a — lossy by design | yes |

Two failure modes still have no identified detector — a publisher that is slow
rather than stopped, and a locked transaction — and a database outage has never
been injected. Those three rows are the reason to build the matrix; the rest is
bookkeeping.

The four M1.4 rows were added after the fact, which is itself the argument for
DOC-002: the revocation row was only fillable because the gate was
failure-validated, and three of the failure modes in it were not on anyone's
list until the state machine forced them to be enumerated.

## OBS-002 — Extend I11 beyond Prometheus rules

**Owner:** Frank Asante Van Laarhoven
**Blocks:** nothing; do opportunistically
**Raised:** 2026-08-05

I11 says a static check validates an artefact and only the running system
proves what is deployed. Rule drift is now checked. The same gap exists for
every other artefact:

| Artefact | Static check | Runtime check | Status |
|---|---|---|---|
| Prometheus rules | promtool | compare loaded rules | done |
| APISIX routes | compose config | query active routes, exercise them | open |
| Keycloak realm | JSON import | authenticate and assert claims | partial — `verify_identity.sh` asserts the tenant claim |
| Migrations | alembic | readiness compares head to expected | done |
| Network policies | manifest parse | attempt prohibited traffic | open |
| Secret policy | review | attempt unauthorised resolution | done — mutation suite |
| Image pinning | check_image_pins.py | verify running digest | open |

**Done when:** each open row either has a runtime check or a recorded reason
why the static check is sufficient.


## DOC-003 — In-flight invocations survive revocation — **CLOSED 2026-08-05**

Resolved by `docs/adr/0002-connector-revocation-semantics.md`. Standard
revocation blocks new and queued work and lets running invocations finish
under the authority snapshot pinned at acceptance; emergency revocation is a
separate operation. `verify_lifecycle.sh` §3 now asserts the chosen behaviour
instead of reporting it, which was this item's completion criterion.

The three implementation items the decision created are below, and they are
**executable, not just written**: `scripts/check_dormant_obligations.py` runs
in `make check` and fails when any of their activation triggers appears in the
source without the corresponding control. Each has been observed both firing
and clearing.

None may be closed because the control exists. The only permitted lifecycle:

```
capability appears
→ tripwire fails
→ control is implemented
→ targeted failure is injected
→ regression test observes the failure
→ obligation may close
```

A regex match alone must never close an obligation. The script can report that
a control is present; only an injected failure reports that it works — and an
obligation closed on the strength of code being present is the same mistake in
a new place.

## REV-001 — "Cancel queued invocations" is vacuous until execution is async

**Owner:** Frank Asante Van Laarhoven
**Blocks:** the first asynchronous execution path — not before
**Raised:** 2026-08-05

ADR 0002 requires standard revocation to cancel queued invocations. Today
`accept()` commits `ACCEPTED` and `execute()` sets `RUNNING` inside the same
request, so there is no queue and no actor positioned to cancel anything. The
clause is not satisfied — there is nothing for it to be satisfied *by*.

This is the dangerous kind of debt: the requirement reads as met because
nothing violates it, and it becomes load-bearing at precisely the moment
someone is busy building a worker pool.

**Carries the connector half of the authority reconciliation.** ADR 0002 pins
*connector* authority at acceptance; `docs/M1.5-PLAN.md` requires *credential*
authority to be revalidated at every resolution. Synchronously the two are
indistinguishable, because `accepted_at ≈ execution_started_at`. The dequeue
checkpoint is where they separate, and where the rule must be stated:

```
accepted under connector v4 → connector later standard-revoked
                            → queued work cancelled here
```

Do not let async dispatch inherit acceptance-time credential authority by
default. That would decide the reconciliation accidentally, which is the
specific failure this item exists to prevent.

**Done when:** either an asynchronous path exists and cancels `ACCEPTED`
invocations on revocation, with a live test that revokes between accept and
start and asserts the invocation never runs; or the ADR is amended to say the
clause is deliberately deferred, with the date.

## REV-002 — Automatic retry would silently falsify a stated guarantee

**Owner:** Frank Asante Van Laarhoven
**Blocks:** any automatic retry of invocations
**Raised:** 2026-08-05

ADR 0002 states that credentials are unavailable for subsequent retries. That
holds today only because no retry path exists: `attempt` is always 1, and a
caller retrying issues a new invocation that passes through
`check_executable`. The property rests on an architectural absence, not a
control — add an internal retry loop and the sentence becomes false with no
test going red.

**Carries the credential half of the authority reconciliation.** A retry is
the clearest case of an execution that must re-earn authority rather than
inherit it:

```
accepted under credential v7 → credential v7 revoked before the retry
                             → the retry must not resolve or use v7
```

`reauthorise_attempt()` must re-evaluate all six, and they are decomposed
this way deliberately:

```
connector status
connector tenant ownership
credential status
credential tenant ownership
credential version binding
invocation deadline / authority context
```

Connector and credential tenancy are separate lines because checking one and
assuming the other follows is the likely omission — the connector check
already exists to be copied, and copying it feels like completing the job.
Re-checking only the connector leaves a revoked *credential* usable, which is
I13a.

**Done when:** any retry implementation re-authorises before resolving
secrets, and a mutation removing that check kills a named test. The M1.5
mutation target "reuse prior credential on retry" is that test; write it early
as a skipped case with its reason rather than remembering it later. Until
then, this item is the tripwire.

## REV-003 — The redirect loop has no revocation checkpoint

**Owner:** Frank Asante Van Laarhoven
**Blocks:** emergency revocation
**Raised:** 2026-08-05

`security/outbound.request` follows redirects in a loop, revalidating the
destination address at every hop (I3) but never re-checking whether the
connector is still authorised. It is the only genuine multi-hop path in the
system.

Under standard revocation this is **correct** — the invocation is running, and
running work finishes. It is a gap only for the emergency mode.

**Done when:** emergency revocation exists and the redirect loop consults it
before each hop, or the loop is documented as out of scope with the reason.
Do not "fix" this before emergency revocation exists: adding a check that
nothing can trigger is untested code guarding an unreachable state.
