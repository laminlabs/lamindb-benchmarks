"""Probe: after creating features on the LOAD instance, do they actually exist
there? This isolates the Schema.save() FK failure:

  - if features are NOT found on load  -> the gap is "features aren't saved to X"
  - if features ARE found on load       -> the gap is "the schemafeature link
                                            uses the wrong-instance feature id"

Runs the same setup path but STOPS before ln.Schema(...).save() (the line that
crashes), so we can inspect state at the point of failure.
"""
import lamindb as ln
from lamindb.models.sqlrecord import _ensure_lamindb_router

import config

_ensure_lamindb_router()

USING = config.LOAD_INSTANCE

# recreate just the pieces setup_load_sheets makes, up to (not including) the schema
supplier_t = ln.Record(name="Supplier", is_type=True).save(using=USING)
gene_t = ln.Record(name="Gene", is_type=True).save(using=USING)
molecule_t = ln.Record(name="SmallMolecule", is_type=True).save(using=USING)

feats = {
    "dose": ln.Feature(name="dose", dtype=str).save(using=USING),
    "supplier": ln.Feature(name="supplier", dtype=supplier_t).save(using=USING),
    "genes": ln.Feature(name="genes", dtype=gene_t).save(using=USING),
}

print("=== where did each feature object think it saved? (_state.db) ===")
for name, f in feats.items():
    print(f"  {name:10s} uid={f.uid}  _state.db={f._state.db}")

print("\n=== is each feature actually present ON THE LOAD INSTANCE? ===")
db_load = ln.DB(USING)
for name, f in feats.items():
    found = db_load.Feature.filter(uid=f.uid).one_or_none()
    print(f"  {name:10s} on load? {'YES' if found else 'NO'}")

print("\n=== is it (wrongly) present on the DEFAULT/current instance instead? ===")
for name, f in feats.items():
    found_here = ln.Feature.filter(uid=f.uid).one_or_none()
    print(f"  {name:10s} on default? {'YES' if found_here else 'NO'}")