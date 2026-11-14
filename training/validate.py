"""Data validation stage (first task of the training pipeline).

Checks schema, nulls, value ranges, allowed categories, label values and minimum row count.
A failure here must stop the pipeline (DECISION D10): the CLI exits with code 1 and the Airflow task
raises ``AirflowFailException``.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

from training.make_dataset import COLUMNS, CONTRACT_TYPES, INTERNET_TYPES, PAYMENT_METHODS

MIN_ROWS = 1000
RANGES: dict[str, tuple[float, float]] = {
    "tenure_months": (0, 72),
    "monthly_charges": (0, 200),
    "total_charges": (0, 20000),
    "support_calls": (0, 20),
}
CATEGORIES: dict[str, list[str]] = {
    "contract_type": CONTRACT_TYPES,
    "internet_service": INTERNET_TYPES,
    "payment_method": PAYMENT_METHODS,
}
BINARY = ["paperless_billing", "senior_citizen", "churn"]


@dataclass
class ValidationReport:
    path: str
    rows: int = 0
    passed: bool = True
    errors: list[str] = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=dict)

    def fail(self, msg: str) -> None:
        self.passed = False
        self.errors.append(msg)


def validate_frame(df: pd.DataFrame, path: str = "<memory>") -> ValidationReport:
    report = ValidationReport(path=path, rows=len(df))

    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        report.fail(f"missing columns: {missing}")
        return report

    if len(df) < MIN_ROWS:
        report.fail(f"too few rows: {len(df)} < {MIN_ROWS}")

    nulls = df[COLUMNS].isna().sum()
    for col, cnt in nulls[nulls > 0].items():
        report.fail(f"column '{col}' has {int(cnt)} null values")

    for col, (lo, hi) in RANGES.items():
        values = pd.to_numeric(df[col], errors="coerce")
        out = int(((values < lo) | (values > hi)).sum())
        if out:
            report.fail(f"column '{col}' has {out} values outside [{lo}, {hi}]")

    for col, allowed in CATEGORIES.items():
        bad = sorted(set(df[col].dropna().unique()) - set(allowed))
        if bad:
            count = int(df[col].isin(bad).sum())
            report.fail(f"column '{col}' has {count} rows with unknown categories {bad}")

    for col in BINARY:
        bad_count = int((~df[col].isin([0, 1])).sum())
        if bad_count:
            report.fail(f"column '{col}' has {bad_count} non-binary values")

    if df["customer_id"].duplicated().any():
        report.fail(f"{int(df['customer_id'].duplicated().sum())} duplicate customer_id values")

    report.stats = {
        "churn_rate": round(float(df["churn"].mean()), 4) if "churn" in df else float("nan"),
        "mean_monthly_charges": round(float(pd.to_numeric(df["monthly_charges"], errors="coerce").mean()), 2),
    }
    return report


def validate_file(path: Path) -> ValidationReport:
    if not path.exists():
        report = ValidationReport(path=str(path))
        report.fail(f"dataset file not found: {path}")
        return report
    return validate_frame(pd.read_csv(path), str(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the raw churn dataset")
    parser.add_argument("--data", type=Path, default=Path("data/raw/churn.csv"))
    parser.add_argument("--report", type=Path, default=Path("reports/validation.json"))
    args = parser.parse_args(argv)

    report = validate_file(args.data)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(asdict(report), indent=2) + "\n", encoding="utf-8", newline="\n")
    status = "PASSED" if report.passed else "FAILED"
    print(f"validation {status}: rows={report.rows} errors={len(report.errors)}")
    for err in report.errors:
        print(f"  - {err}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
