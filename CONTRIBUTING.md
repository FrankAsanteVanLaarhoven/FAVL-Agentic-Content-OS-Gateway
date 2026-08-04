# Contributing

## The gate

```bash
make check          # ruff + ruff format --check + mypy --strict + pytest
make test-outbox    # delivery guarantee against a running stack
make test-identity  # tenant isolation against a running stack
```

`make check` must be clean before a commit. CI runs the same commands in the
same order, so a green local gate and a red CI is a bug in one of them, not a
difference of opinion.

## Standards that are enforced, not suggested

| Rule | Enforced by |
|---|---|
| No lint or formatting drift | `ruff check` + `ruff format --check` |
| Full static typing | `mypy --strict`, no per-file opt-outs beyond two documented ORM factories |
| Alert rules reference real metrics | `scripts/check_alert_metrics.py` |
| Container images pinned by digest | `scripts/check_image_pins.py` |
| No secrets in history | gitleaks, full-depth |
| No known-vulnerable runtime deps | `npm audit --omit=dev`, Trivy on every image |
| Delivery guarantee holds under crashes | `tests/verify_outbox.sh` |
| Tenant isolation holds | `tests/verify_identity.sh` |

Each of the two custom checkers was validated against a deliberately
introduced fault. A checker nobody has seen fail is not known to work.

## Writing changes

**Comments explain why, never what.** The code says what it does. A comment
earns its place by recording the reason a non-obvious choice was made — a
constraint, a failure mode, a rejected alternative. Several comments in this
repository exist because the obvious implementation was wrong; see
`security/ssrf.py` on interpreter-dependent address classification and
`favl_outbox/publisher.py` on why the guarantee is at-least-once.

**Match the surrounding code.** Naming, structure and comment density are
consistent throughout; a change that reads differently is harder to review
even when it is correct.

**Honesty markers.** Anything unmeasured stays marked as such. The README's
"Not yet implemented" section and the console's disabled navigation entries
are load-bearing: a system that looks more complete than it is causes worse
decisions than one that admits its gaps.

## Security-sensitive areas

Changes to these require an adversarial review — someone trying to defeat the
change, reading the source rather than the tests:

- `services/connector-registry/app/security/` — SSRF classification, outbound
  policy, secret handling. Two criticals have already been found here: a
  caller-controlled privilege escalation, and an address-classification
  bypass that passed tests because the test interpreter differed from the
  runtime interpreter.
- `services/connector-registry/app/identity.py` and the `proxy-rewrite`
  blocks in `gateway/apisix.yaml` — together these decide who a caller is.
- `packages/favl-outbox/` — the delivery guarantee. Any change here needs
  `make test-outbox`, which kills containers under load.

A test that passes is not evidence that a security control works; a control
that has never been attacked is untested. Prefer a check that has been shown
to fail on a real fault.

## Migrations

- One migration per change, with a working `downgrade`.
- The ORM and the migration must produce identical DDL. They diverged once —
  a partial index predicate compiled to a constant false — and the divergence
  was invisible because production happened to have the correct index from a
  different migration.
- Bump `EXPECTED_MIGRATION` in the service's `main.py` so readiness fails
  when a replica runs against a schema it does not expect.
- Migrations run from a Kubernetes Job, not from replica startup: several
  replicas racing the Alembic version lock can deadlock a rollout.

## Authorship

Commits carry Frank Asante Van Laarhoven <frankleroyvan@gmail.com> only.
