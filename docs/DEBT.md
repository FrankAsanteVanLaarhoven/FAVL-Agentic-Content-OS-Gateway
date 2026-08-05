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


## DOC-003 — In-flight invocations survive revocation

**Owner:** Frank Asante Van Laarhoven
**Blocks:** nothing yet; needs a decision before a customer asks
**Raised:** 2026-08-05

Revoking a connector prevents new use immediately (I12, gated by
`verify_lifecycle.sh` section 3). An invocation already accepted runs to its
deadline — up to 300 s. `verify_lifecycle.sh` reports that outcome rather than
asserting it, because asserting either way would imply a decision nobody has
made.

The two defensible answers are different products:

- **Accepted work completes.** Simple, and matches how most credentials
  behave — a revoked API key does not kill sessions already authenticated.
- **Revocation cancels in flight.** Correct if revocation means "this
  credential is compromised *now*", which is the reason the reason field
  exists.

**Done when:** the choice is recorded in ADR 0001 with its rationale, and
`verify_lifecycle.sh` asserts the chosen behaviour instead of reporting it.
Until then the residual-risk row stays open, and nobody should claim
revocation is instantaneous without the qualifier.
