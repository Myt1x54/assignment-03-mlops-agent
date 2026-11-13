"""Preprocessing stage: deterministic stratified train/test split.

Feature encoding (scaling, one-hot) lives inside the sklearn Pipeline built in ``training.train`` so the
exact same transformation is shipped with the model to the inference service.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from sklearn.model_selection import train_test_split

from training.params import load_params

NUMERIC_FEATURES = ["tenure_months", "monthly_charges", "total_charges", "support_calls"]
BINARY_FEATURES = ["paperless_billing", "senior_citizen"]
CATEGORICAL_FEATURES = ["contract_type", "internet_service", "payment_method"]
FEATURES = NUMERIC_FEATURES + BINARY_FEATURES + CATEGORICAL_FEATURES
TARGET = "churn"


def split(df: pd.DataFrame, test_size: float, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    train, test = train_test_split(df, test_size=test_size, random_state=seed, stratify=df[TARGET])
    return train.reset_index(drop=True), test.reset_index(drop=True)


def run(data: Path, out_dir: Path, test_size: float, seed: int) -> tuple[Path, Path]:
    df = pd.read_csv(data)
    train, test = split(df, test_size, seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    train_path, test_path = out_dir / "train.csv", out_dir / "test.csv"
    train.to_csv(train_path, index=False, lineterminator="\n")
    test.to_csv(test_path, index=False, lineterminator="\n")
    print(f"preprocess: train={len(train)} test={len(test)} -> {out_dir}")
    return train_path, test_path


def main(argv: list[str] | None = None) -> None:
    params = load_params()
    parser = argparse.ArgumentParser(description="Split raw data into train/test")
    parser.add_argument("--data", type=Path, default=Path("data/raw/churn.csv"))
    parser.add_argument("--out-dir", type=Path, default=Path("data/processed"))
    args = parser.parse_args(argv)
    run(args.data, args.out_dir, params["split"]["test_size"], params["split"]["seed"])


if __name__ == "__main__":
    main()
