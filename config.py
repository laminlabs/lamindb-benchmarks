import os

TRACKING_INSTANCE = "laminlabs/lamindb-benchmarks"
LOAD_INSTANCE = "laminlabs/lamindb-load-test"
LOAD_DB_URI = "postgresql://6798ad81719449e4a943fe4992409fa1_jwt.rdprthiizfwqjvgppqvs:Nvifw8IHqzs6bIrTcR6ZsqcQPXoUhQ8X2XAQSSv5@aws-0-eu-central-1.pooler.supabase.com:6543/6798ad81719449e4a943fe4992409fa1"

RLS_ON = True

SCALE_STEPS = [1_000, 2_000, 5_000, 10_000]
BATCH_SIZE = 1_000
N_REPETITIONS = 11
UID_POOL_SIZE = 2_000

KEY_TABLES = ["lamindb_record", "lamindb_recordulabel", "lamindb_recordjson",
              "lamindb_recordrecord", "hubmodule_dbwrite"]

SUPPLIERS = [f"supplier_{i:02d}" for i in range(10)]
GENES = [f"gene_{i:03d}" for i in range(100)]
SMALL_MOLECULES = [f"mol_{i:02d}" for i in range(20)]
PROTOCOLS = ["protocol_A", "protocol_B", "protocol_C", "protocol_D"]
GENES_PER_RECORD = (3, 5)
MOLECULES_PER_RECORD = (1, 3)

QUERY_LADDER = [
    {"query_type": "uid_lookup", "n_uids": 1},
    {"query_type": "uid_with_features", "n_uids": 1},
    {"query_type": "uid_batch20_with_features", "n_uids": 20},
]