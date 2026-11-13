"""Deterministic synthetic customer-churn dataset generator (DECISION D4).

Versions
--------
- ``v1``      : 5,000 customers, baseline distribution (released as git tag ``data-v1.0``).
- ``v2``      : 8,000 customers, drifted distribution — more fiber customers, higher charges, more
                support calls (released as git tag ``data-v2.0``).
- ``corrupt`` : v1 with injected quality problems (nulls, negative charges, unknown categories).
                Used only for failure injection F2; never released as a dataset tag.

The output is byte-for-byte reproducible for a given version (fixed seed, fixed float formatting,
LF line endings), so anybody can regenerate the data and compare the DVC md5.

Usage::

    uv run python -m training.make_dataset --version v1 --out data/raw/churn.csv
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

CONTRACT_TYPES = ["month-to-month", "one-year", "two-year"]
INTERNET_TYPES = ["dsl", "fiber", "none"]
PAYMENT_METHODS = ["electronic-check", "mailed-check", "bank-transfer", "credit-card"]

COLUMNS = [
    "customer_id",
    "tenure_months",
    "monthly_charges",
    "total_charges",
    "contract_type",
    "internet_service",
    "payment_method",
    "paperless_billing",
    "senior_citizen",
    "support_calls",
    "churn",
]


@dataclass(frozen=True)
class VersionSpec:
    rows: int
    seed: int
    contract_p: tuple[float, float, float]
    internet_p: tuple[float, float, float]
    charge_shift: float
    support_lambda: float


SPECS: dict[str, VersionSpec] = {
    "v1": VersionSpec(5000, 101, (0.50, 0.28, 0.22), (0.40, 0.42, 0.18), 0.0, 1.4),
    "v2": VersionSpec(8000, 202, (0.58, 0.25, 0.17), (0.30, 0.55, 0.15), 9.0, 2.0),
}


def _generate(spec: VersionSpec) -> pd.DataFrame:
    rng = np.random.default_rng(spec.seed)
    n = spec.rows

    contract = rng.choice(CONTRACT_TYPES, size=n, p=spec.contract_p)
    internet = rng.choice(INTERNET_TYPES, size=n, p=spec.internet_p)
    payment = rng.choice(PAYMENT_METHODS, size=n, p=[0.35, 0.20, 0.23, 0.22])
    senior = rng.binomial(1, 0.16, size=n)
    paperless = rng.binomial(1, 0.6, size=n)

    tenure_base = np.select(
        [contract == "month-to-month", contract == "one-year"],
        [rng.gamma(2.0, 7.0, n), rng.gamma(4.0, 8.0, n)],
        default=rng.gamma(6.0, 8.0, n),
    )
    tenure = np.clip(np.round(tenure_base), 0, 72).astype(int)

    base_charge = np.select([internet == "fiber", internet == "dsl"], [80.0, 55.0], default=22.0)
    monthly = np.clip(base_charge + rng.normal(spec.charge_shift, 9.0, n), 18.0, 125.0).round(2)
    total = (monthly * np.maximum(tenure, 1) * rng.uniform(0.95, 1.02, n)).round(2)
    support = np.clip(rng.poisson(spec.support_lambda, n), 0, 10)

    # Ground-truth churn model (logistic). Coefficients chosen so classes are learnable but not trivial.
    logit = (
        -1.15
        + 1.6 * (contract == "month-to-month")
        - 1.2 * (contract == "two-year")
        - 0.055 * tenure
        + 0.028 * (monthly - 60.0)
        + 0.42 * support
        + 0.55 * (internet == "fiber")
        + 0.45 * (payment == "electronic-check")
        + 0.30 * senior
        + 0.20 * paperless
    )
    churn = rng.binomial(1, 1.0 / (1.0 + np.exp(-logit)))

    return pd.DataFrame(
        {
            "customer_id": [f"C{spec.seed:03d}-{i:05d}" for i in range(n)],
            "tenure_months": tenure,
            "monthly_charges": monthly,
            "total_charges": total,
            "contract_type": contract,
            "internet_service": internet,
            "payment_method": payment,
            "paperless_billing": paperless,
            "senior_citizen": senior,
            "support_calls": support,
            "churn": churn,
        },
        columns=COLUMNS,
    )


def _corrupt(df: pd.DataFrame, seed: int = 303) -> pd.DataFrame:
    """Inject realistic data-quality defects so validation must fail (failure injection F2)."""
    rng = np.random.default_rng(seed)
    bad = df.copy()
    bad["tenure_months"] = bad["tenure_months"].astype("float")
    n = len(bad)
    bad.loc[rng.choice(n, 400, replace=False), "tenure_months"] = np.nan
    bad.loc[rng.choice(n, 120, replace=False), "monthly_charges"] = -1.0 * rng.uniform(5, 50, 120).round(2)
    bad.loc[rng.choice(n, 60, replace=False), "contract_type"] = "unknown"
    return bad


def generate(version: str) -> pd.DataFrame:
    if version == "corrupt":
        return _corrupt(_generate(SPECS["v1"]))
    if version not in SPECS:
        raise ValueError(f"unknown dataset version {version!r}; expected one of {[*SPECS, 'corrupt']}")
    return _generate(SPECS[version])


def write_csv(df: pd.DataFrame, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    # Fixed formatting + LF endings => identical bytes on every OS => identical DVC md5.
    df.to_csv(out, index=False, lineterminator="\n", float_format="%.2f")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", required=True, choices=[*SPECS, "corrupt"])
    parser.add_argument("--out", type=Path, default=Path("data/raw/churn.csv"))
    args = parser.parse_args(argv)
    df = generate(args.version)
    write_csv(df, args.out)
    print(f"wrote {len(df)} rows ({args.version}) -> {args.out}  churn_rate={df['churn'].mean():.3f}")


if __name__ == "__main__":
    main()
