"""Validated query settings, independent of the generated population."""

from datetime import UTC, datetime

from benchmarks.config import require_str

PROTOCOLS = ["protocol_A", "protocol_B", "protocol_C", "protocol_D"]


def parse_timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Query date bounds must be ISO 8601 timestamps") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("Query date bounds must include a timezone")
    return timestamp.astimezone(UTC)


def query_parameters(params: dict) -> dict[str, str]:
    values = {
        "query_gene": require_str(params, "query_gene", default="gene_000"),
        "query_protocol": require_str(params, "query_protocol", default="protocol_B"),
        "query_treatment_start": require_str(
            params, "query_treatment_start", default="2025-01-01T00:00:00Z"
        ),
        "query_treatment_end": require_str(
            params, "query_treatment_end", default="2025-07-01T00:00:00Z"
        ),
    }
    if values["query_protocol"] not in PROTOCOLS:
        raise ValueError(f"query_protocol must be one of {PROTOCOLS}")
    start = parse_timestamp(values["query_treatment_start"])
    end = parse_timestamp(values["query_treatment_end"])
    if start >= end:
        raise ValueError("query_treatment_start must be before query_treatment_end")
    values["query_treatment_start"] = start.isoformat().replace("+00:00", "Z")
    values["query_treatment_end"] = end.isoformat().replace("+00:00", "Z")
    return values
