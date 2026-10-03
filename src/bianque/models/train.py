"""Train the fraud calibrator and compare it with the baselines on the frozen sets.

  make train      # uv run --group ml python -m bianque.models.train

What a run does:
  1. Fits every calibrator on train only (process_date < settings.eval_train_end), from score
     counts aggregated in DuckDB.
  2. Evaluates each one on the frozen validation and test sets, after checking their SHA-256
     against eval/frozen/manifest.json (a changed set would make runs incomparable).
  3. Measures probability quality (Brier, log loss, ECE, ROC-AUC, PR-AUC) and the outcome of
     the expected-value contact rule, an OFFLINE SIMULATION with the synthetic costs of
     policies/: contact if p x amount_usd > channel cost + friction.
  4. Logs one MLflow run per calibrator (mlruns/, git-ignored).
  5. Selects the calibrator with the highest validation net benefit (then lowest log loss),
     writes it to settings.fraud_model (committed, aggregated counts only) and writes
     reports/model_evaluation.md. Test numbers are reported, never used to choose.

Simulation assumptions (stated in the report): a contacted fraud's loss is fully avoided; the
contact uses one SMS (cost per delivered message from gold.channel_costs); a contacted
legitimate customer costs the friction of policies/cost_assumptions_v*.yaml.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml
from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

from bianque.config import Settings, load_settings
from bianque.evaluation.frozen_sets import sha256
from bianque.models.baselines import BIN_WIDTH
from bianque.models.calibration import (
    BayesianBlocksCalibrator,
    HistogramCalibrator,
    IsotonicCalibrator,
    RawScoreCalibrator,
    ScoreCounts,
)
from bianque.pipeline.silver import lit

log = logging.getLogger("bianque.train")

MODEL_VERSION = "bayes_blocks_v1"
CONTACT_CHANNEL = "SMS"
CREDIBLE_LEVEL = 0.95
EXPERIMENT = "fraud_calibration"
REPORT = Path("reports/model_evaluation.md")
SETS = ("fraud_validation", "fraud_test")
GROUPS = ("segment", "customer_country", "age_band")


@dataclass(frozen=True)
class Costs:
    contact: float  # USD per contact
    friction: float  # USD per legitimate customer contacted
    assumptions_version: str


def features_relation(settings: Settings) -> str:
    path = settings.gold_root / "transaction_features" / "**" / "*.parquet"
    return f"read_parquet({lit(str(path))}, hive_partitioning = true)"


def train_counts(con: duckdb.DuckDBPyConnection, settings: Settings) -> ScoreCounts:
    rows = con.execute(f"""
        SELECT source_fraud_score::DOUBLE AS score, count(*) AS n,
               count(*) FILTER (WHERE is_fraud) AS k
        FROM {features_relation(settings)}
        WHERE process_date < DATE {lit(settings.eval_train_end.isoformat())}
        GROUP BY 1 ORDER BY 1 NULLS FIRST
    """).fetchnumpy()
    score = np.ma.filled(rows["score"], np.nan)  # NULL arrives as a masked element
    n, k = np.asarray(rows["n"], dtype=np.int64), np.asarray(rows["k"], dtype=np.int64)
    scored = ~np.isnan(score)
    return ScoreCounts(
        scores=score[scored],
        n=n[scored],
        k=k[scored],
        unscored_n=int(n[~scored].sum()),
        unscored_k=int(k[~scored].sum()),
    )


def histogram_baseline(con: duckdb.DuckDBPyConnection, settings: Settings) -> HistogramCalibrator:
    path = settings.gold_root / "score_calibration" / "*.parquet"
    rows = con.execute(f"SELECT score_bin, p_fraud FROM read_parquet({lit(str(path))})").fetchall()
    return HistogramCalibrator({b: p for b, p in rows}, BIN_WIDTH)


def load_costs(con: duckdb.DuckDBPyConnection, settings: Settings) -> Costs:
    assumptions = yaml.safe_load(settings.cost_assumptions.read_text())
    path = settings.gold_root / "channel_costs" / "*.parquet"
    contact = con.execute(
        f"SELECT cost_per_delivered FROM read_parquet({lit(str(path))}) WHERE channel = ?",
        [CONTACT_CHANNEL],
    ).fetchone()[0]
    return Costs(
        float(contact), float(assumptions["friction_cost_legit_usd"]), assumptions["version"]
    )


def load_frozen(settings: Settings) -> dict[str, pd.DataFrame]:
    """The frozen sets, refusing any file whose hash differs from the committed manifest."""
    manifest = json.loads(settings.eval_manifest.read_text())
    frozen = {}
    for name in SETS:
        info = manifest["sets"][name]
        path = settings.eval_manifest.parent / info["file"]
        if sha256(path) != info["sha256"]:
            raise SystemExit(f"{path} does not match its manifest hash; run make gold first")
        df = duckdb.sql(f"""
            SELECT transaction_id, source_fraud_score::DOUBLE AS score,
                   amount_usd::DOUBLE AS amount_usd, is_fraud::INTEGER AS is_fraud,
                   {", ".join(GROUPS)}
            FROM read_parquet({lit(str(path))})
        """).df()
        frozen[name] = df
    return frozen


def probability_metrics(y: np.ndarray, p: np.ndarray, bins: int = 10) -> dict[str, float]:
    clipped = np.clip(p, 1e-6, 1 - 1e-6)
    edges = np.minimum((p * bins).astype(int), bins - 1)
    ece = sum(
        abs(y[edges == b].mean() - p[edges == b].mean()) * (edges == b).mean()
        for b in range(bins)
        if (edges == b).any()
    )
    return {
        "brier": brier_score_loss(y, p),
        "log_loss": log_loss(y, clipped, labels=[0, 1]),
        "ece": float(ece),
        "roc_auc": roc_auc_score(y, p),
        "pr_auc": average_precision_score(y, p),
    }


def decision_metrics(
    y: np.ndarray,
    amount: np.ndarray,
    p: np.ndarray,
    costs: Costs,
    interval: tuple[np.ndarray, np.ndarray] | None = None,
) -> dict[str, float]:
    """Outcome of the expected-value rule. With an interval, transactions whose credible
    interval straddles the break-even probability are counted as abstentions (to a human);
    the rest are decided automatically."""
    hurdle = costs.contact + costs.friction
    contact = p * amount > hurdle
    fraud = y == 1
    caught = contact & fraud
    out = {
        "contacts": int(contact.sum()),
        "frauds": int(fraud.sum()),
        "frauds_contacted": int(caught.sum()),
        "legit_contacted": int((contact & ~fraud).sum()),
        "fraud_loss_usd": float(amount[fraud].sum()),
        "loss_avoided_usd": float(amount[caught].sum()),
        "contact_cost_usd": float(contact.sum() * costs.contact),
        "friction_usd": float((contact & ~fraud).sum() * costs.friction),
    }
    out["net_benefit_usd"] = out["loss_avoided_usd"] - out["contact_cost_usd"] - out["friction_usd"]
    out["recall"] = out["frauds_contacted"] / max(out["frauds"], 1)
    out["precision"] = out["frauds_contacted"] / max(out["contacts"], 1)
    if interval is not None:
        lo, hi = interval
        abstain = (lo * amount <= hurdle) & (hi * amount > hurdle)
        out["abstained"] = int(abstain.sum())
        out["frauds_abstained"] = int((abstain & fraud).sum())
    return out


def oracle_metrics(y: np.ndarray, amount: np.ndarray, costs: Costs) -> dict[str, float]:
    """Contact exactly the frauds: the upper bound of the net benefit."""
    return decision_metrics(y, amount, y.astype(float), Costs(costs.contact, 0.0, ""))


def group_table(df: pd.DataFrame, p: np.ndarray, costs: Costs) -> pd.DataFrame:
    contact = p * df["amount_usd"].to_numpy() > costs.contact + costs.friction
    d = df.assign(contact=contact, fraud=df["is_fraud"] == 1)
    rows = []
    for group in GROUPS:
        for value, g in d.groupby(group, dropna=False):
            rows.append(
                {
                    "group": group,
                    "value": "NULL" if pd.isna(value) else value,
                    "transactions": len(g),
                    "frauds": int(g.fraud.sum()),
                    "fraud_recall": g[g.fraud].contact.mean() if g.fraud.any() else np.nan,
                    "legit_contact_rate": g[~g.fraud].contact.mean(),
                }
            )
    return pd.DataFrame(rows)


def evaluate(models: dict, frozen: dict[str, pd.DataFrame], costs: Costs) -> dict:
    results = {}
    for name, model in models.items():
        results[name] = {}
        for split, df in frozen.items():
            y, amount = df["is_fraud"].to_numpy(), df["amount_usd"].to_numpy()
            p = model.predict(df["score"].to_numpy())
            interval = (
                model.interval(df["score"].to_numpy(), CREDIBLE_LEVEL)
                if hasattr(model, "interval")
                else None
            )
            results[name][split] = {
                **probability_metrics(y, p),
                **decision_metrics(y, amount, p, costs, interval),
            }
    return results


def log_mlflow(results: dict, models: dict, selected: str, context: dict) -> None:
    import mlflow  # training-only dependency (uv group "ml")

    root = Path("mlruns").resolve()
    root.mkdir(exist_ok=True)
    mlflow.set_tracking_uri(f"sqlite:///{root / 'mlflow.db'}")
    if mlflow.get_experiment_by_name(EXPERIMENT) is None:
        mlflow.create_experiment(EXPERIMENT, artifact_location=(root / "artifacts").as_uri())
    mlflow.set_experiment(EXPERIMENT)
    for name, model in models.items():
        with mlflow.start_run(run_name=name):
            mlflow.set_tags({"selected": str(name == selected), **context["tags"]})
            mlflow.log_params({"calibrator": name, **context["params"]})
            if isinstance(model, BayesianBlocksCalibrator):
                mlflow.log_params({"penalty": model.penalty, "n_blocks": len(model.blocks)})
                mlflow.log_dict(model.to_dict(), "calibrator.json")
            for split, metrics in results[name].items():
                short = split.removeprefix("fraud_")
                mlflow.log_metrics({f"{short}_{k}": float(v) for k, v in metrics.items()})


def select(results: dict) -> str:
    """Highest validation net benefit, then lowest validation log loss. Test is not used."""
    return max(
        results,
        key=lambda n: (
            round(results[n]["fraud_validation"]["net_benefit_usd"], 2),
            -results[n]["fraud_validation"]["log_loss"],
        ),
    )


def write_model(model: BayesianBlocksCalibrator, counts: ScoreCounts, settings: Settings) -> None:
    """Deterministic JSON (no timestamps), so an unchanged retrain leaves git clean."""
    payload = {
        "model_version": MODEL_VERSION,
        "predicts": "probability that a transaction is fraud, from source fraud_score",
        "trained_on": "gold.transaction_features",
        "trained_before": settings.eval_train_end.isoformat(),
        "train_rows": counts.total,
        "train_frauds": int(counts.k.sum()) + counts.unscored_k,
        "credible_level": CREDIBLE_LEVEL,
        **model.to_dict(),
    }
    settings.fraud_model.parent.mkdir(parents=True, exist_ok=True)
    settings.fraud_model.write_text(json.dumps(payload, indent=2) + "\n")


def fmt(v: float, digits: int = 4) -> str:
    return f"{v:,.{digits}f}" if isinstance(v, float) else f"{v:,}"


def write_report(
    results: dict,
    oracle: dict,
    selected: str,
    blocks: BayesianBlocksCalibrator,
    groups: pd.DataFrame,
    costs: Costs,
    frozen: dict[str, pd.DataFrame],
) -> None:
    lines = [
        "# Fraud calibrator evaluation",
        "",
        "Generated by `make train` (`src/bianque/models/train.py`). All numbers are **offline",
        "measurements on the frozen evaluation sets**; the decision outcomes are an **offline",
        "simulation**, not a production result. Why the learned component is a calibrator:",
        "[`docs/adr/0001-fraud-signal-gate.md`](../docs/adr/0001-fraud-signal-gate.md).",
        "",
        "## Setup",
        "",
        "| Item | Value |",
        "|------|-------|",
        "| Train | `gold.transaction_features`, process_date before the train end (2025-07-01) |",
    ]
    for split, df in frozen.items():
        lines.append(
            f"| {split} | {len(df):,} transactions, {int(df.is_fraud.sum())} frauds (frozen, "
            "hash verified) |"
        )
    lines += [
        "| Selection | Highest validation net benefit, then lowest validation log loss |",
        f"| Contact rule | contact if p x amount_usd > {costs.contact:.4f} "
        f"({CONTACT_CHANNEL} cost per delivered message, gold.channel_costs) + "
        f"{costs.friction:.2f} (friction, `{costs.assumptions_version}`, SYNTHETIC) |",
        "| Simulation assumption | A contacted fraud's loss is fully avoided |",
        f"| Abstention | {CREDIBLE_LEVEL:.0%} credible interval of p straddles the break-even "
        "probability (Bayesian blocks only) |",
        "",
        f"**Selected: `{selected}`.**",
        "",
        "## Learned blocks",
        "",
        "| Score range | Train transactions | Train frauds | p(fraud) | 95% credible interval |",
        "|---|---:|---:|---:|---|",
    ]
    for b in (*blocks.blocks, blocks.unscored):
        rng = "no score" if b.low is None else f"{b.low:.2f} - {b.high:.2f}"
        score = np.array([np.nan if b.low is None else b.low])
        lo, hi = blocks.interval(score, CREDIBLE_LEVEL)
        lines.append(
            f"| {rng} | {b.n:,} | {b.k:,} | {blocks.predict(score)[0]:.5f} "
            f"| [{lo[0]:.5f}, {hi[0]:.5f}] |"
        )
    lines += [
        "",
        f"Penalty per block: log(N) = {blocks.penalty:.2f} nats.",
    ]
    for split in SETS:
        lines += [
            "",
            f"## {split}",
            "",
            "| Calibrator | Brier | Log loss | ECE | ROC-AUC | PR-AUC |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name in results:
            m = results[name][split]
            lines.append(
                f"| {name} | {m['brier']:.6f} | {m['log_loss']:.5f} | {m['ece']:.5f} "
                f"| {m['roc_auc']:.4f} | {m['pr_auc']:.4f} |"
            )
        lines += [
            "",
            "| Calibrator | Contacts | Frauds contacted | Legit contacted | Recall | "
            "Loss avoided (USD) | Contact + friction (USD) | Net benefit (USD) | Abstained |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for name, m in [*results.items(), ("oracle (contact only frauds)", oracle)]:
            m = m[split]
            abstained = (
                f"{m['abstained']:,} ({m['frauds_abstained']} fraud)" if "abstained" in m else "-"
            )
            lines.append(
                f"| {name} | {m['contacts']:,} | {m['frauds_contacted']} / {m['frauds']} "
                f"| {m['legit_contacted']:,} | {m['recall']:.3f} | {m['loss_avoided_usd']:,.0f} "
                f"| {m['contact_cost_usd'] + m['friction_usd']:,.0f} "
                f"| {m['net_benefit_usd']:,.0f} | {abstained} |"
            )
    lines += [
        "",
        f"## By group (test, `{selected}`)",
        "",
        "Fraud recall and the share of legitimate transactions contacted, per group. Small",
        "groups are noisy: a group with 20 frauds has a recall interval of about ±0.2.",
        "",
        "| Group | Value | Transactions | Frauds | Fraud recall | Legit contact rate |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in groups.itertuples():
        recall = "-" if np.isnan(r.fraud_recall) else f"{r.fraud_recall:.3f}"
        lines.append(
            f"| {r.group} | {r.value} | {r.transactions:,} | {r.frauds} | {recall} "
            f"| {r.legit_contact_rate:.5f} |"
        )
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{settings.duckdb_memory_limit}'")
    counts = train_counts(con, settings)
    costs = load_costs(con, settings)
    blocks = BayesianBlocksCalibrator.fit(counts)
    models = {
        "raw_score": RawScoreCalibrator(),
        "histogram_baseline": histogram_baseline(con, settings),
        "isotonic": IsotonicCalibrator.fit(counts),
        "bayes_blocks": blocks,
    }
    frozen = load_frozen(settings)
    results = evaluate(models, frozen, costs)
    oracle = {
        split: oracle_metrics(df["is_fraud"].to_numpy(), df["amount_usd"].to_numpy(), costs)
        for split, df in frozen.items()
    }
    selected = select(results)
    for name, r in results.items():
        log.info(
            "%-20s valid net=%10.0f logloss=%.5f | test net=%10.0f logloss=%.5f",
            name,
            r["fraud_validation"]["net_benefit_usd"],
            r["fraud_validation"]["log_loss"],
            r["fraud_test"]["net_benefit_usd"],
            r["fraud_test"]["log_loss"],
        )
    log.info("selected: %s", selected)

    manifest = json.loads(settings.eval_manifest.read_text())
    context = {
        "tags": {"model_version": MODEL_VERSION, "simulation": "offline"},
        "params": {
            "train_end": settings.eval_train_end.isoformat(),
            "contact_channel": CONTACT_CHANNEL,
            "contact_cost_usd": costs.contact,
            "friction_usd": costs.friction,
            "cost_assumptions": costs.assumptions_version,
            **{f"{n}_sha256": manifest["sets"][n]["sha256"][:12] for n in SETS},
        },
    }
    log_mlflow(results, models, selected, context)
    if selected == "bayes_blocks":
        write_model(blocks, counts, settings)
        log.info("wrote %s", settings.fraud_model)
    else:
        log.warning("%s beat bayes_blocks on validation; model file not updated", selected)
    test = frozen["fraud_test"]
    groups = group_table(test, models[selected].predict(test["score"].to_numpy()), costs)
    write_report(results, oracle, selected, blocks, groups, costs, frozen)
    log.info("wrote %s", REPORT)


if __name__ == "__main__":
    main()
