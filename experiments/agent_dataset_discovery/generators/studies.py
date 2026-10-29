"""Study descriptions derived entirely from the per-dataset metadata."""

from collections import Counter


def study_readme(study_id: str, rows: list[dict[str, str]]) -> str:
    def values(field: str) -> str:
        return ", ".join(sorted({row[field] for row in rows}))

    groups = Counter(
        (
            row["organism"],
            row["source_tissue"],
            row["assay"],
            row["counts_availability"],
        )
        for row in rows
    )
    lines = [
        f"# {study_id}: {values('organism')} — {values('source_tissue')}",
        "",
        (
            f"This synthetic study contains {len(rows)} datasets. "
            f"The represented organisms are {values('organism')}; "
            f"the source tissues are {values('source_tissue')}. "
            f"The assays are {values('assay')}."
        ),
        "",
        "## Dataset composition",
        "",
        "The table shows the combinations actually present in this study.",
        "",
        "| Organism | Source tissue | Assay | Counts availability | Datasets |",
        "| --- | --- | --- | --- | ---: |",
    ]
    for group, count in sorted(groups.items()):
        lines.append("| " + " | ".join((*group, str(count))) + " |")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            (
                "This description is derived from the study's datasets table. "
                "Use that table for individual dataset IDs, paths, counts locations, and evidence. "
                "Counts marked unknown must not be treated as normalized-only expression."
            ),
            "",
            (
                "These are synthetic benchmark fixtures, not a real biological study. "
                "The shared dummy expression files do not represent experimental observations. "
                "No donors, treatments, institutions, or publications are implied."
            ),
            "",
        ]
    )
    return "\n".join(lines)
