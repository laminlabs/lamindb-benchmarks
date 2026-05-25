import lamindb as ln

source_db = ln.DB("laminlabs/lamindata")

project = source_db.Project.get(name="1000 Genomes")

artifacts = project.artifacts.filter(key__endswith=".parquet").all()

for artifact in artifacts:
    artifact.save()
    print(f"Synced: {artifact.path}")
