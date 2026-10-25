"""Shared query order and styling for record scalability plots."""

LABELS = {
    "uid_lookup": "lookup",
    "uid_with_features": "with features",
    "uid_batch_with_features": "batch with features",
    "recent_by_treatment_started_at": "latest 100 by treatment time",
    "recent_by_created_at": "latest 100 by creation time",
    "interventions_by_gene": "latest 100 linked to gene",
    "interventions_by_gene_protocol_time": "latest 100 by gene, protocol and time",
}

COLORS = {
    "uid_lookup": "#1f77b4",
    "uid_with_features": "#ff7f0e",
    "uid_batch_with_features": "#2ca02c",
    "recent_by_treatment_started_at": "#9467bd",
    "recent_by_created_at": "#d62728",
    "interventions_by_gene": "#17becf",
    "interventions_by_gene_protocol_time": "#8c564b",
}
