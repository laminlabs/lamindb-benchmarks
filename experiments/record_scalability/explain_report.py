"""Render captured SQL and matching plans as a browser-readable HTML artifact."""

from __future__ import annotations

import json
from html import escape

import sqlparse

LOOKUP_CALLS = {
    "interventions_by_gene_protocol_time": (
        "cohort = (ln.Record.filter(type=intervention, is_type=False)\n"
        '    .filter(gene_feature == gene, protocol=query_settings["query_protocol"],\n'
        "            treatment_started_at__gte=start, treatment_started_at__lt=end)\n"
        '    .order_by(-timestamp_feature, "-uid"))\n'
        'selected_uids = list(cohort.values_list("uid", flat=True)[:100])\n'
        "(ln.Record.filter(uid__in=selected_uids)\n"
        '    .order_by(-timestamp_feature, "-uid").to_dataframe(include="features"))'
    ),
    "uid_lookup": "ln.Record.filter(uid=uid).one_or_none()",
    "uid_with_features": 'ln.Record.filter(uid=uid).to_dataframe(include="features")',
    "uid_batch_with_features": 'ln.Record.filter(uid__in=uids).to_dataframe(include="features")',
    "recent_by_treatment_started_at": (
        "list(ln.Record.filter(type=intervention, is_type=False)\n"
        '    .order_by(-timestamp_feature, "-uid")[:100])\n'
        '# timestamp_feature = ln.Feature.get(name="treatment_started_at")'
    ),
    "recent_by_created_at": (
        "list(ln.Record.filter(type=intervention, is_type=False)\n"
        '    .order_by("-created_at", "-uid")[:100])'
    ),
    "interventions_by_gene": (
        "list(ln.Record.filter(type=intervention, is_type=False).filter(gene_feature == gene)\n"
        '    .order_by("-created_at", "-uid")[:100])'
    ),
}


def _code(value: str, language: str) -> str:
    return f'<pre><code class="language-{language}">{escape(value)}</code></pre>'


def render_explain_report(queries: list[dict], n_rows: int) -> str:
    parts = [
        (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            "<title>SQL queries and execution plans</title><style>"
            "body{font:15px/1.6 system-ui,sans-serif;color:#172b45;max-width:1100px;margin:32px auto;padding:0 24px}"
            "h1,h2,h3{line-height:1.25}section{border-top:1px solid #dbe3ed;margin-top:32px;padding-top:16px}"
            "pre{background:#f2f5f9;border:1px solid #dbe3ed;border-radius:8px;padding:16px;overflow:auto}"
            "code{font:13px/1.55 ui-monospace,monospace}summary{cursor:pointer;font-weight:600}"
            "nav a{margin-right:20px}a{color:#3266a8}</style></head><body>"
        ),
        "<h1>SQL queries and execution plans</h1>",
        f"<p>Population: <strong>{n_rows:,} intervention records</strong>.</p>",
        (
            "<p>These data SELECT statements were captured by replaying "
            "each lookup after its timed repetitions. Each statement is paired with "
            "its PostgreSQL EXPLAIN ANALYZE plan, including buffer statistics. "
            "Diagnostic replays are outside the lookup latency measurements.</p>"
        ),
        (
            "<p>The benchmark queries are Python API operations, not necessarily "
            "single SQL statements. Feature inclusion can issue additional SELECTs "
            "to discover feature annotations and fetch values and linked records. "
            "Each section below shows one diagnostic replay of that operation; "
            "the timed repetitions are not repeated in this report. Statements run "
            "separately. The EXPLAIN table sums their plan execution times; the "
            "lookup latency tables time the complete Python operation. Authentication "
            "token calls and transaction-control statements are excluded.</p>"
        ),
        "<nav>"
        + " ".join(
            f'<a href="#query-{index}">{escape(query["query_name"])}</a>'
            for index, query in enumerate(queries)
        )
        + "</nav>",
    ]
    for query_index, query in enumerate(queries):
        parts.append(
            f'<section id="query-{query_index}"><h2>{escape(query["query_name"])}</h2>'
        )
        if query["query_name"] in LOOKUP_CALLS:
            parts.extend(
                [
                    "<h3>Python operation</h3>",
                    _code(LOOKUP_CALLS[query["query_name"]], "python"),
                ]
            )
        if query.get("parameters"):
            parts.extend(
                [
                    "<p>Query parameters (resolved before timing):</p>",
                    _code(json.dumps(query["parameters"], indent=2), "json"),
                ]
            )
        if query.get("error"):
            parts.extend(
                [
                    "<p>Diagnostic collection failed:</p>",
                    _code(query["error"], "text"),
                    "</section>",
                ]
            )
            continue
        captured = sum(
            statement["source"] == "lookup" for statement in query["statements"]
        )
        parts.append(
            f"<p>Captured SQL statements for this logical lookup: <strong>{captured}</strong>.</p>"
        )
        parts.append("<h3>Complete SQL query list</h3>")
        statements = [
            statement["sql"]
            for statement in query["statements"]
            if statement["sql"] is not None
        ]
        if statements:
            sql = "\n\n".join(
                f"-- Statement {index}\n{sqlparse.format(statement, reindent=True, keyword_case='upper', wrap_after=100)};"
                for index, statement in enumerate(statements, start=1)
            )
            parts.append(_code(sql, "sql"))
        else:
            parts.append("<p>No SQL statements could be captured.</p>")
        parts.append(
            '<details class="execution-plans"><summary>Execution plans</summary>'
        )
        for index, statement in enumerate(query["statements"], start=1):
            parts.append(f"<h4>Statement {index}</h4>")
            if statement["source"] == "record_filter":
                parts.append(
                    "<p>Fallback record-filter plan; this is not the complete lookup. "
                    "The corresponding SQL could not be captured.</p>"
                )
            if statement["plan"] is None:
                parts.append(
                    "<p>EXPLAIN was unavailable for this captured statement.</p>"
                )
            else:
                parts.extend(
                    [
                        "<details open><summary>Execution plan</summary>",
                        _code(json.dumps(statement["plan"], indent=2), "json"),
                        "</details>",
                    ]
                )
        parts.append("</details></section>")
    return "\n".join([*parts, "</body></html>"]) + "\n"
