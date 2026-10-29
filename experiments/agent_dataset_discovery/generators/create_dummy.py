"""Create the shared placeholder once; setup only copies the saved file."""

from pathlib import Path

import anndata as ad
import numpy as np
import pandas as pd


def main() -> None:
    destination = Path(__file__).resolve().parents[1] / "raw" / "dummy.h5ad"
    if destination.exists():
        raise SystemExit(f"Dummy already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    data = ad.AnnData(
        X=np.zeros((3, 4), dtype=np.float32),
        obs=pd.DataFrame(index=[f"cell_{i}" for i in range(3)]),
        var=pd.DataFrame(index=[f"gene_{i}" for i in range(4)]),
    )
    data.uns["placeholder"] = True
    data.uns["description"] = (
        "Shared dummy payload. Dataset properties and hypothetical counts locations "
        "are defined only by the accompanying metadata tables."
    )
    data.write_h5ad(destination, compression="gzip")
    print(f"Created {destination}")


if __name__ == "__main__":
    main()
