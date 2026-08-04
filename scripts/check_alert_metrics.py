#!/usr/bin/env python3
"""Fail if an alert rule references a metric no code path emits.

An alert on a misspelled or renamed metric never fires. It looks like
coverage, reports green forever, and is discovered only during the incident
it was meant to catch. This check makes that failure mode a build error.

It parses metric names out of the Prometheus rule expressions and compares
them against the names declared in the Python sources, allowing for the
suffixes the client library appends (`_total`, `_bucket`, `_sum`, `_count`,
`_created`).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "deploy" / "prometheus-alerts.yml"

# Every module that constructs prometheus_client metrics.
SOURCES = [
    ROOT / "packages" / "favl-outbox" / "favl_outbox" / "metrics.py",
    ROOT / "packages" / "favl-outbox" / "favl_outbox" / "consumer.py",
    ROOT / "services" / "connector-registry" / "app" / "invocations.py",
]

# Names Prometheus itself provides, so they need no declaration in our code.
BUILTIN = {"up", "vector", "time", "scrape_duration_seconds"}

# Suffixes prometheus_client derives from a declared base name.
DERIVED_SUFFIXES = ("_total", "_bucket", "_sum", "_count", "_created", "_info")

DECLARATION = re.compile(
    r'(?:Counter|Gauge|Histogram|Summary)\(\s*["\']([a-zA-Z_:][a-zA-Z0-9_:]*)["\']'
)
IDENTIFIER = re.compile(r"\b([a-z_][a-z0-9_]*(?::[a-z0-9_]+)*)\b")

# Quoted strings hold label values and regex alternations, never metric names.
QUOTED = re.compile(r"""(["']).*?\1""", re.DOTALL)
# Label matchers: everything inside {} is label names and values.
LABEL_MATCHER = re.compile(r"\{[^}]*\}")
# Grouping clauses: `by (service)` names labels, not metrics.
GROUPING = re.compile(
    r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)"
)

PROMQL_FUNCTIONS = {
    "abs",
    "absent",
    "absent_over_time",
    "avg",
    "avg_over_time",
    "bottomk",
    "ceil",
    "changes",
    "clamp",
    "clamp_max",
    "clamp_min",
    "count",
    "count_over_time",
    "count_values",
    "day_of_month",
    "day_of_week",
    "days_in_month",
    "delta",
    "deriv",
    "exp",
    "floor",
    "group",
    "histogram_quantile",
    "hour",
    "idelta",
    "increase",
    "irate",
    "label_join",
    "label_replace",
    "last_over_time",
    "ln",
    "log2",
    "log10",
    "max",
    "max_over_time",
    "min",
    "min_over_time",
    "minute",
    "month",
    "predict_linear",
    "present_over_time",
    "quantile",
    "quantile_over_time",
    "rate",
    "resets",
    "round",
    "scalar",
    "sgn",
    "sort",
    "sort_desc",
    "sqrt",
    "stddev",
    "stddev_over_time",
    "stdvar",
    "sum",
    "sum_over_time",
    "time",
    "timestamp",
    "topk",
    "vector",
    "year",
}

PROMQL_KEYWORDS = {
    "and",
    "or",
    "unless",
    "by",
    "without",
    "on",
    "ignoring",
    "group_left",
    "group_right",
    "offset",
    "bool",
    "start",
    "end",
    "inf",
    "nan",
} | PROMQL_FUNCTIONS


def declared_metrics() -> set[str]:
    names: set[str] = set()
    for source in SOURCES:
        if not source.exists():
            print(f"warning: declared-metric source missing: {source}", file=sys.stderr)
            continue
        for match in DECLARATION.finditer(source.read_text(encoding="utf-8")):
            base = match.group(1)
            names.add(base)
            # A Counter("x_total") is emitted verbatim; a Counter("x") becomes
            # x_total. Accept both spellings for either declaration.
            names.update(base + suffix for suffix in DERIVED_SUFFIXES)
            if base.endswith("_total"):
                names.add(base.removesuffix("_total"))
    return names


def _strip_non_metric_syntax(expression: str) -> str:
    """Remove everything in an expression that cannot be a metric name."""
    expression = QUOTED.sub(" ", expression)
    expression = LABEL_MATCHER.sub(" ", expression)
    return GROUPING.sub(" ", expression)


def referenced_metrics() -> dict[str, int]:
    """Metric names appearing in `expr:` values, mapped to line number."""
    found: dict[str, int] = {}
    in_expr = False
    expr_indent = 0

    for number, raw in enumerate(RULES.read_text(encoding="utf-8").splitlines(), 1):
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())

        if stripped.startswith("expr:"):
            in_expr = True
            expr_indent = indent
            body = stripped[len("expr:") :].lstrip("|>-").strip()
        elif in_expr and stripped and indent <= expr_indent:
            # Dedent back to sibling key level ends the block scalar.
            in_expr = False
            continue
        elif in_expr:
            body = stripped
        else:
            continue

        for match in IDENTIFIER.finditer(_strip_non_metric_syntax(body)):
            token = match.group(1)
            if token in PROMQL_KEYWORDS or token in BUILTIN:
                continue
            found.setdefault(token, number)
    return found


def main() -> int:
    if not RULES.exists():
        print(f"error: {RULES} not found", file=sys.stderr)
        return 1

    declared = declared_metrics()
    referenced = referenced_metrics()

    unknown = {
        name: line
        for name, line in referenced.items()
        if name not in declared and name not in BUILTIN
    }

    print(f"declared metric names: {len(declared)}")
    print(f"metrics referenced by alert rules: {len(referenced)}")

    if unknown:
        print("\nAlert rules reference metrics that no code path emits:\n")
        for name, line in sorted(unknown.items(), key=lambda kv: kv[1]):
            print(f"  {RULES.name}:{line}: {name}")
        print(
            "\nAn alert on a metric that is never emitted cannot fire. Either "
            "correct the name or add the instrumentation."
        )
        return 1

    print("\nEvery alert rule references a metric the code emits.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
