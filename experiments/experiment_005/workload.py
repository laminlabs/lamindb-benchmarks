"""The record scalability synthetic treatment, represented as Notion properties."""

from __future__ import annotations

from datetime import date, timedelta

NAME = "experiment_005"
TABLES = ("suppliers", "genes", "small_molecules", "interventions")
DATASET_TITLE = "Notion experiment_005 dataset"


def uid(index: int) -> str:
    return f"intervention_{index:09d}"


def text(value: str) -> list[dict]:
    return [{"type": "text", "text": {"content": value}}]


def plain(property: dict) -> str:
    return "".join(
        item.get("plain_text", item.get("text", {}).get("content", ""))
        for item in property[property["type"]]
    )


def treatment(index: int, rng, params: dict) -> dict:
    """Match record scalability’s random call order and scalar values exactly."""
    treated = date(2024, 1, 1) + timedelta(days=rng.randint(0, 700))
    return {
        "benchmark_uid": uid(index),
        "benchmark_index": index,
        "dose": f"{round(rng.uniform(0.1, 100.0), 2)} uM",
        "protocol": rng.choice(
            ["protocol_A", "protocol_B", "protocol_C", "protocol_D"]
        ),
        "treatment_date": treated.isoformat(),
        "treatment_time_min": rng.randint(5, 1440),
        "supplier": rng.choice([f"supplier_{i:02d}" for i in range(10)]),
        "genes": rng.sample(
            [f"gene_{i:03d}" for i in range(params["n_genes"])],
            rng.randint(params["genes_per_record_min"], params["genes_per_record_max"]),
        ),
        "small_molecules": rng.sample(
            [f"mol_{i:02d}" for i in range(params["n_molecules"])],
            rng.randint(
                params["small_molecules_per_record_min"],
                params["small_molecules_per_record_max"],
            ),
        ),
    }


def schema(table: str, sources: dict) -> dict:
    properties = {"name": {"title": {}}}
    if table != "interventions":
        return properties
    properties.update(
        {
            "benchmark_uid": {"rich_text": {}},
            "benchmark_index": {"number": {}},
            "dose": {"rich_text": {}},
            "protocol": {
                "select": {
                    "options": [{"name": f"protocol_{letter}"} for letter in "ABCD"]
                }
            },
            "treatment_date": {"date": {}},
            "treatment_time_min": {"number": {}},
        }
    )
    for prop, target in (
        ("supplier", "suppliers"),
        ("genes", "genes"),
        ("small_molecules", "small_molecules"),
    ):
        properties[prop] = {
            "relation": {
                "data_source_id": sources[target]["data_source_id"],
                "type": "single_property",
                "single_property": {},
            }
        }
    return properties


def page_properties(row: dict, catalogs: dict) -> dict:
    props = {
        "name": {"title": text(row["benchmark_uid"])},
        "benchmark_uid": {"rich_text": text(row["benchmark_uid"])},
        "benchmark_index": {"number": row["benchmark_index"]},
        "dose": {"rich_text": text(row["dose"])},
        "protocol": {"select": {"name": row["protocol"]}},
        "treatment_date": {"date": {"start": row["treatment_date"]}},
        "treatment_time_min": {"number": row["treatment_time_min"]},
    }
    for prop, table in (
        ("supplier", "suppliers"),
        ("genes", "genes"),
        ("small_molecules", "small_molecules"),
    ):
        names = [row[prop]] if prop == "supplier" else row[prop]
        props[prop] = {"relation": [{"id": catalogs[table][name]} for name in names]}
    return props


def catalog_names(params: dict) -> dict[str, list[str]]:
    return {
        "suppliers": [f"supplier_{i:02d}" for i in range(10)],
        "genes": [f"gene_{i:03d}" for i in range(params["n_genes"])],
        "small_molecules": [f"mol_{i:02d}" for i in range(params["n_molecules"])],
    }


def uid_filter(values: list[str]) -> dict:
    clauses = [
        {"property": "benchmark_uid", "rich_text": {"equals": value}}
        for value in values
    ]
    return clauses[0] if len(clauses) == 1 else {"or": clauses}


def inventory(api, source: str, expected: int, *, full=False):
    """Partition by synthetic index: each query matches <=1,000, never 10,000.

    An additional query detects foreign/missing-index/out-of-range pages. Refuse
    duplicate UIDs, gaps, incomplete results, or edited benchmark identities.
    """
    for start in range(0, expected, 1000):
        end = min(start + 1000, expected)
        seen = set()
        filter = {
            "and": [
                {
                    "property": "benchmark_index",
                    "number": {"greater_than_or_equal_to": start},
                },
                {"property": "benchmark_index", "number": {"less_than": end}},
            ]
        }
        for page in api.query(
            source,
            filter=filter,
            properties=None if full else ["benchmark_index", "benchmark_uid"],
        ):
            props = page["properties"]
            number = props["benchmark_index"]["number"]
            if (
                isinstance(number, bool)
                or not isinstance(number, (int, float))
                or not float(number).is_integer()
            ):
                raise ValueError("Dataset has an invalid benchmark index")
            index = int(number)
            if (
                index in seen
                or index not in range(start, end)
                or plain(props["benchmark_uid"]) != uid(index)
            ):
                raise ValueError(
                    "Dataset has duplicate, missing, or edited benchmark identities"
                )
            seen.add(index)
            yield page
        if len(seen) != end - start:
            raise ValueError(
                f"Expected {end - start} interventions in index range [{start}, {end}), found {len(seen)}"
            )
    outside = {
        "or": [
            {"property": "benchmark_index", "number": {"is_empty": True}},
            {"property": "benchmark_index", "number": {"less_than": 0}},
            {
                "property": "benchmark_index",
                "number": {"greater_than_or_equal_to": expected},
            },
        ]
    }
    if (
        next(api.query(source, filter=outside, properties=["benchmark_index"]), None)
        is not None
    ):
        raise ValueError(
            "Dataset contains interventions outside the expected population"
        )
