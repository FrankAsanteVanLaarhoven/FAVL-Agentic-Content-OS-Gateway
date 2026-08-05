# ADR 0001 — Security invariants

Status: **Accepted** · 2026-08-05 · Supersedes nothing · Required reading
before M1.4

## Why this record exists

Every invariant below was violated at some point during M1.2–M1.3, and each
violation passed the test suite at the time it was introduced. Two were
written by the same hand that wrote the control they broke. The point of this
document is not to describe good intentions — it is to name the small number
of properties that must never regress, and to bind each one to a test that
has been *observed to fail* when the property is removed.

An invariant with no failing test is an aspiration. Where a test is listed
below, it has been validated by deliberately reintroducing the defect and
confirming the suite goes red. Where that validation has not been done, the
row says so.

## Threat model

**Trusted:** the operator, the deployment environment, and anything that can
set environment variables or apply Kubernetes manifests. An operator who
wants to reach an internal address can configure it; that is their
prerogative and not a vulnerability.

**Untrusted:** every API caller, including authenticated ones. A valid token
proves who someone is, never what they may reach. Connector configuration is
attacker-controlled data, because any authenticated principal can create a
connector.

**Semi-trusted:** in-cluster workloads. Network position is not
authentication. A compromised sidecar must not inherit the platform's
authority.

**Out of scope for this record:** denial of service against the gateway
itself, compromise of the identity provider, and physical or hypervisor-level
attack. Production identity hardening is M1.7 and this document does not
claim it.

---

## I1 — A destination may never authorise reaching itself

Anything that *widens* outbound reach is operator policy read from the
environment. Connector configuration may only narrow it.

Concretely: `allow_private_addresses`, `allowed_schemes` and
`allow_plaintext_acknowledged` are environment-only and rejected if present
in a connector record. Every numeric bound (`max_response_bytes`,
`max_redirects`, timeouts) is clamped with `min(requested, ceiling)` inside
`build_policy`, and every set is intersected.

**The clamp is the boundary, not the rejection list.** A denylist makes each
new field unsafe by default, which is exactly how three bounds stayed
caller-controlled after the first fix: nobody added them to the list. A new
`OutboundPolicy` field is safe only if it is clamped.

> Violated once: `allow_private_addresses` was read from the connector
> record, so any authenticated caller could point a hostname at RFC1918
> space and turn the gateway into a proxy into the internal network —
> Postgres, NATS, Keycloak and the APISIX control port.

Enforced by `security/policy.py::build_policy`.
Tested by `test_connector_config_cannot_widen_outbound_reach`,
`test_numeric_bounds_are_clamped_to_the_operator_ceiling`,
`test_operator_host_allowlist_is_an_upper_bound`. **Failure observed.**

## I2 — An address is classified after normalisation, against an explicit table

Every embedded-IPv4 form (v4-mapped, 6to4, Teredo, NAT64) is unwrapped before
classification, and forbidden ranges are declared as networks rather than
inferred from `is_private` / `is_link_local`.

Blocked unconditionally, even when a deployment permits private addressing:
loopback, link-local, multicast, unspecified, carrier-grade NAT, site-local,
IPv4-compatible and IPv4-translated embeddings, documentation and
discard-only ranges, and every cloud metadata endpoint. Blocked additionally
when private addressing is off: RFC1918 and the reserved ranges.

Non-HTTP schemes are refused, so `file://`, `gopher://` and a unix-socket
path never reach a transport. Every redirect hop is revalidated from scratch;
approval is never inherited.

> Violated once: `::ffff:169.254.169.254` reached cloud metadata because
> Python 3.11 — the container runtime — does not unwrap v4-mapped forms,
> while the test venv runs 3.13, which does. **The suite was green and
> production was exposed.** A guard whose correctness depends on the
> interpreter is not a guard.

Enforced by `security/ssrf.py`.
Tested by `test_embedded_ipv4_forms_are_unwrapped_before_classification`,
`test_embedded_forms_stay_blocked_when_private_is_allowed`,
`test_mixed_resolution_is_rejected_entirely`,
`test_non_http_schemes_are_rejected`. **Failure observed.**

## I3 — The connection goes to the address that was checked

The hostname is resolved once; every returned address must pass; the
connection is pinned to a validated address with `Host` and SNI preserved. A
name resolving to one public and one private address is rejected outright
rather than quietly using the public one.

Enforced by `security/ssrf.py::validate_url`, `security/outbound.py`.
Tested by `test_connection_is_pinned_to_the_validated_address` and
`test_ipv6_pinning_is_bracketed`. **Failure observed** — removing the pin
fails both. (`test_pinned_address_is_the_normalised_form` guards I2's
normalisation, not the pin, and is unaffected.)

## I4 — Identity comes from the verified token; the client cannot name itself

Tenant and actor derive from claims APISIX verified. The gateway strips
inbound `X-Tenant-ID` and `X-Actor-ID`. A request without verified claims is
refused — there is no fallback to a shared default, because a shared default
makes `WHERE tenant_id = …` match everything while looking like isolation.

> Violated once: the realm emitted no tenant claim, so every caller resolved
> to `default` and the scoping was structurally present but vacuous.

Enforced by `app/identity.py` (both services), the `proxy-rewrite` blocks in
`gateway/apisix.yaml`, and the realm's protocol mapper.
Tested by `tests/verify_identity.sh` — forged `X-Userinfo`, anonymous
request, claim presence. **Failure observed.**

## I5 — Public and internal paths share one authorisation pipeline

The internal surface carries a service credential proving the caller is
inside the mesh. That credential is **not** authorisation for a tenant. The
calling service forwards the tenant it verified, and the receiving service
applies the same `_tenant_connector` check as the public path.

> Violated once, and it was the worst defect found: the public path checked
> tenant and `/internal` did not. Tenant A created an agent naming tenant B's
> connector id; the orchestrator called `/internal` with the mesh token; B's
> connector was invoked with B's credentials and the response returned to A.
> A tenant-scoped public path plus an unscoped internal path is not
> isolation.

Enforced by `main.py::invoke_connector_internal`,
`orchestrator/main.py::internal_call_headers`.
Tested by `tests/verify_identity.sh` — plants a connector in another tenant
and asserts an agent fan-out cannot reach it. **Failure observed** (the
bypass was reintroduced and the suite went red).

## I6 — A connector names a logical secret, never a storage location

References take the form `secret://connector/<name>/<key>` or
`secret://tenant/<id>/<key>`. There is no generic environment lookup, so
there is no expression a connector can write that reaches an arbitrary
variable. Ownership comes from the record; a connector may read its own
secrets and its tenant's and nothing else. Legacy `env:` references are
refused rather than translated.

> Violated once: `env:` resolved out of `os.environ`, so
> `env:INTERNAL_SERVICE_TOKEN` in a header delivered the credential guarding
> the internal surface to a caller-chosen host — self-escalating straight
> back into I5.

Enforced by `security/secrets.py`.
Tested by `test_no_reference_can_address_arbitrary_storage`,
`test_a_connector_cannot_read_another_connectors_secret`,
`test_legacy_env_references_are_refused_not_translated`.
**Failure observed** — the tests were written against the previous `env:`
resolver and went red on migration, which is the same evidence.

## I7 — Nothing leaving the service republishes a credential

Redaction is an allowlist: a string value is published only if its key is
structural. Sensitivity is inherited into nested dicts and lists. Literal
header values are never published; names and references are.

> Violated twice. First a denylist missed lists entirely, so
> `{"profiles": [{"password": …}]}` went to the API and to NATS verbatim.
> Then it missed `{"auth": {"value": …}}`, where only the *parent* key looks
> sensitive, and any custom header name.

Enforced by `security/redaction.py`, applied at `main.py::_to_schema`, which
builds both the API response and the outbox payload.
Tested by the eight hostile-config cases. **Failure observed.**

## I7a — A partial index must reference its column, not a literal

`postgresql_where` must compile to a predicate over the column. SQLAlchemy
silently coerces a bare string into a bound parameter, so `func.lower("status")`
became `lower('status') = 'pending'` — a constant false. Postgres accepts such
an index, keeps it permanently empty, and the planner never matches it.

Verified three ways, because the DDL alone is not enough: the predicate text
must reference the column, must not contain `lower(`, and the claim query's
plan must use the index under a realistic pending backlog. Plan assertions on
an empty table are meaningless, so the probe inserts 2000 pending rows inside
a rolled-back transaction.

Enforced by `favl_outbox/models.py` and migration 0004/0005.
Tested by `verify_outbox.sh` section 0. **Failure observed.**

## I8 — A committed write has exactly one delivered event

Domain row and outbox row commit in one transaction. Delivery is
at-least-once with stream deduplication keyed on the outbox row id, which is
also the `Nats-Msg-Id`. This is **not** exactly-once, and the distinction is
deliberate: a database commit and a broker publish cannot be atomic without a
distributed transaction. Consumers must deduplicate on `event_id` and order
on `aggregate_version`.

The dead-letter limit and the duplicate-window gate must use the same number.

> Violated once: the gate validated `retry_policy.max_attempts` while the
> publisher dead-lettered on a database column defaulting to 8, so the gate
> could certify a 76-second horizon on a system that really retried for 766.

Enforced by `favl_outbox/publisher.py`, `favl_outbox/config.py`.
Tested by `tests/verify_outbox.sh` (17 assertions incl. four `SIGKILL`s) and
`test_dead_letter_limit_matches_the_gated_policy`. **Failure observed.**

## I9 — A check that has never failed is not known to work

Every gating check ships with a demonstrated failure. This applies to the
alert-metric checker, the image-pin checker, the tenant-isolation assertions
and the harness guards.

> Violated repeatedly. Six assertions were found that could not fail:
> comparisons against fields the schema does not have, counts of rows for a
> tenant nothing creates, swallowed `docker kill` failures, and
> `empty == empty` when a helper errored. Each reported green while testing
> nothing.

Enforced by review. See CONTRIBUTING.

---

## I10 — A health signal must not be produced by the component it describes

A signal for component X is computed by an independent observer, or derived
on pull from state X does not control, and never by X alone. A component that
has stopped cannot report that it has stopped.

| Component | Wrong | Correct |
|---|---|---|
| Outbox | publisher updates the backlog gauge | scrape derives backlog from the database |
| Connector | connector reports itself healthy | the runtime probes it and records the result |
| Agent | agent reports itself running | an observer watches a heartbeat it does not write |
| Workflow | workflow updates its own completion metric | the engine derives completion from persisted state |
| Gateway | service reports its own latency | APISIX and the collector measure it |

Only the first row is implemented; the rest are the standard this platform
holds itself to as those components are built, and a violation is a design
error rather than a bug to be found later.

> Violated once: outbox gauges were refreshed only inside the publisher's own
> run loop. When the publisher stopped — the precise incident
> `OutboxOldestPendingTooOld` exists to catch — the gauges froze at their
> last healthy values and the alert could not fire. It reported green
> throughout a deliberately injected stall.
>
> Found by writing the alert firing test, not by review. Every earlier check
> on that alert — promtool, the metric-name checker, the rewrite from `and`
> to `unless` — passed while it remained incapable of firing. None of them
> could observe that the input never moved.

Enforced by `main.py::prometheus_metrics` in both services.
Tested by `tests/verify_alerts.sh`. **Failure observed** — the test failed
before this change and passes after.

Residual: a scrape that cannot reach the database serves stale gauges. It
logs, and `up{}` covers the total-failure case, but a database that is slow
rather than down is not distinguished.

## I11 — A static check validates a file; only the running system is deployed

promtool parses the rule file. `check_alert_metrics.py` proves it names real
series. Neither says the running instance is evaluating it.

> OBS-001. `OutboxStalledWhileWriting` never fired through three controlled
> stalls. The expression was not at fault — the repository held the corrected
> rule and Prometheus held the previous one, because `/-/reload` was disabled
> and nothing had restarted it. Every static check was green against a rule
> that was not deployed.
>
> The alert was then removed anyway, on redundancy grounds: every way
> delivery can stall produces an ageing backlog, which
> `OutboxOldestPendingTooOld` detects in about ninety seconds. A second page
> for the same incident, ten minutes later, is noise.

Enforced by `--web.enable-lifecycle` and the drift assertion in
`tests/verify_alerts.sh`, which compares the deployed rule set to the file.
**Failure observed** — the drift check was written against the stale state
and reported it.

## I12 — Revocation prevents new use immediately, not eventually

Executability is a property of the connector row, read inside the transaction
that is about to use it. It is never cached, never derived from an event, and
never held in a worker's memory. `lifecycle.is_executable` is the only
function that answers "may this serve traffic", and `EXECUTABLE_STATES` is a
set of exactly one, so a state added to the enum is non-executable until
somebody puts it there deliberately.

The corollaries that make this hold rather than merely sound true:

- The executability guard runs **before** the refusal mapping. A state present
  in the enum but missing from `_REFUSAL` is refused with a generic code, not
  admitted. The reverse ordering — look up how to refuse, admit if unknown —
  reads almost identically and is a bypass.
- Events describe what happened; they do not decide what is permitted. A
  consumer that had not yet seen `connector.revoked` changes nothing.
- Revocation is one-way in the transition table. Restoring a revoked connector
  would return a compromised credential to service without rotating it.
- Creation is not a transition and so the machine cannot police it.
  `CREATABLE_STATES` polices it instead; without that list a caller could
  create a connector already `revoked`, skipping the reason that state
  requires.

Enforced by `services/connector-registry/app/lifecycle.py` and the guard in
`invocations.check_executable`. Guarded by `tests/test_lifecycle.py` (33
tests) and by eight mutations in `scripts/mutate.py`.

**Scope.** "New use" means exactly that. What happens to an invocation already
running is settled separately in ADR 0002: standard revocation lets it finish
under the authority snapshot pinned at acceptance; emergency revocation (not
yet implemented) requests cancellation. The platform does not claim to cancel
provider-side work already accepted remotely. Do not restate I12 as
"revocation stops all activity" — it does not, and the narrower claim is the
one that survives an incident review.

**Failure observed** — twice, and neither by the unit tests.

`tests/verify_lifecycle.sh` section 3 revokes a connector while an invocation
is in flight and asserts the next one is refused; with `REVOKED` added to
`EXECUTABLE_STATES` it returned 200. That is the failure the invariant exists
to prevent, and it was watched happening.

The second was subtler and is the reason the live test exists at all. Every
unit test passed while `transitions.apply` looked for an idempotent *self*
edge (`disabled -> disabled`) instead of an idempotent edge *into* the target
(`enabled -> disabled`), so every retried disable and every retried revoke
returned 409. An operator retrying a revocation after a timeout would have
been told the revocation failed. The unit tests exercised the predicate
directly and never the lookup order; only a real retry against the running
system found it.

## Consequences

Changes touching `security/`, `identity.py`, the `proxy-rewrite` blocks in
`gateway/apisix.yaml`, or `favl_outbox/` require an adversarial review whose
brief is to **defeat** the change by reading the source — not to confirm it
against the tests, which are the thing under suspicion.

A change that weakens an invariant is a breaking change to this record and
needs a superseding ADR, not a code comment.

## Known residual risk

| Risk | Compensating control | Closes in |
|---|---|---|
| Identity provider runs in development mode against H2 | Not internet-exposed; realm is repository-controlled | M1.7 |
| Secrets resolve from environment variables | Names are derived, never caller-supplied; scoped by owner | M1.7 |
| No tenant administration; tenant comes from one user attribute | Claim is IdP-issued and unforgeable through the gateway | M3 |
| Agents may reference a connector id from another tenant at creation | Invocation is refused at fan-out; only the reference is permitted | M1.5 |
| Provider-side work already accepted remotely cannot be cancelled | None possible. The guarantee is scoped to authorisation, not to remote execution; ADR 0002 states it explicitly rather than implying more | permanent — accepted |
| Emergency revocation is specified but not built | Standard revocation is implemented and gated; the emergency path is designed in ADR 0002, tracked as REV-001..003 | M1.5 |
| The redirect loop re-checks addresses but not revocation | Correct under standard revocation (running work finishes); a gap only for the unbuilt emergency mode | with REV-003 |
| Physical deletion (`archived -> deleted`) has no endpoint | The transition is defined and privileged; nothing can reach it yet | M1.5 |
| Rolling back migration 0008 destroys the audit trail | Documented in the migration; dump `connector_audit` before downgrading | accepted |
| A config change can sit undeployed | `verify_alerts.sh` compares the running rule set against the file; `--web.enable-lifecycle` makes reload possible | closed |
