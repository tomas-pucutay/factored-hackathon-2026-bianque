"""Model search: can tuned machine learning beat the calibrated fraud_score?

  make model-search   # about 10 minutes; writes reports/model_search.md

Run after make train. It is the evidence behind docs/adr/0002-model-search.md: before settling
on the calibrated fraud_score (bayes_blocks), two tuned alternatives were tried honestly.

  A. No fraud_score at all. fraud_score is treated as the challenge's baseline to beat, so
     neither it nor anything derived from it (e.g. "has no score") is a feature. All
     transactions; behavioral features only.
  B. Hybrid. Above the trained calibrator's first block edge (30.00) fraud is certain, so the
     calibrator keeps that range; a tuned model replaces it where it is weak: scores at or
     below the edge and unscored transactions. fraud_score and an unscored flag are features.

Protocol (the same for both, so the numbers are comparable):
  - Train is split again in time: fit (before TUNE_START) and tune (TUNE_START to the train
    end). Hyperparameters are chosen on tune only; validation and test are touched once, at
    the end, after refitting the chosen configuration on all of train.
  - Negatives of train are sampled at NEG_FRACTION and weighted back (every fraud kept), so
    the search fits in a few GB of RAM. Validation and test are complete.
  - Objective: the combined metric below. Two samplers with the same budget: Optuna TPE
    (Bayesian optimization) and random search. Logistic regression is tuned over C.
  - Final comparison: net benefit in USD with a paired bootstrap of the difference.

Metrics (offline simulation, synthetic costs from policies/ and gold.channel_costs):
  net benefit (USD) = fraud amount on contacted frauds - contact cost x contacts
                      - friction x legitimate customers contacted,
  with the contact rule p x amount_usd > contact cost + friction (train.decision_metrics).
  share of oracle  = net benefit / net benefit of contacting exactly the frauds.
  dollar-weighted AP = average precision with each transaction weighted by its amount
                     (ranking the expensive frauds first), normalized to 0 = random, 1 = perfect.
  combined = share of oracle + 0.5 x normalized dollar-weighted AP.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import lightgbm as lgb
import numpy as np
import optuna
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, log_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

from bianque.config import Settings, load_settings
from bianque.evaluation.label_signal import CATEGORICAL, connect
from bianque.models.calibration import BayesianBlocksCalibrator, ScoreCounts
from bianque.models.train import Costs, decision_metrics, load_costs, oracle_metrics

REPORT = Path("reports/model_search.md")
SEED = 0
NEG_FRACTION = 0.1
N_TRIALS = 30
N_BOOT = 200
TUNE_DAYS = 181  # last ~6 months of train
LOGREG_C = (0.001, 0.01, 0.1, 1.0, 10.0)
APW_WEIGHT = 0.5
NOT_FEATURES = {
    "transaction_id", "customer_id", "product_id", "transaction_date", "process_date",
    "process_month", "source_fraud_score", "is_fraud", "split", "w", "fs",
}  # fmt: skip


def load(settings: Settings) -> pd.DataFrame:
    tune_start = settings.eval_train_end - timedelta(days=TUNE_DAYS)
    keep = int(NEG_FRACTION * 1000)
    df = (
        connect(settings)
        .sql(f"""
        SELECT *,
            CASE WHEN process_date < DATE '{tune_start}' THEN 'fit'
                 WHEN process_date < DATE '{settings.eval_train_end}' THEN 'tune'
                 WHEN process_date < DATE '{settings.eval_validation_end}' THEN 'validation'
                 ELSE 'test' END AS split
        FROM read_parquet('{settings.gold_root}/transaction_features/**/*.parquet',
                          hive_partitioning = true)
        WHERE process_date < DATE '{settings.eval_test_end}'
          AND (process_date >= DATE '{settings.eval_train_end}'
               OR is_fraud OR hash(transaction_id) % 1000 < {keep})
    """)
        .df()
    )
    df["is_fraud"] = df["is_fraud"].astype(np.int8)
    df["fs"] = df["source_fraud_score"].astype("float64")  # the baseline; a feature only in B
    for col in df.columns:
        if col in CATEGORICAL:
            df[col] = df[col].astype("category")
        elif col not in NOT_FEATURES:
            df[col] = pd.to_numeric(df[col].astype("float64")).astype(np.float32)
    df["amount_usd"] = df["amount_usd"].astype(np.float64)
    df["score"] = df["fs"].fillna(-1).astype(np.float32)
    df["unscored"] = df["fs"].isna().astype(np.float32)
    sampled = df["split"].isin(["fit", "tune"]) & (df["is_fraud"] == 0)
    df["w"] = np.where(sampled, 1 / NEG_FRACTION, 1.0)
    return df


@dataclass(frozen=True)
class Experiment:
    key: str
    title: str
    features: list[str]
    references: dict  # name -> f(train_df, eval_df) -> p


def evaluate(d: pd.DataFrame, p: np.ndarray, costs: Costs) -> dict[str, float]:
    y, a, w = d["is_fraud"].to_numpy(), d["amount_usd"].to_numpy(), d["w"].to_numpy()
    rule = decision_metrics(y, a, p, costs, weight=w)
    oracle = oracle_metrics(y, a, costs)["net_benefit_usd"]
    apw = average_precision_score(y, p, sample_weight=w * a)
    apw_random = float((w * a)[y == 1].sum() / (w * a).sum())
    apw_norm = (apw - apw_random) / (1 - apw_random)
    share = rule["net_benefit_usd"] / oracle
    return {
        **rule,
        "share_of_oracle": share,
        "apw_norm": apw_norm,
        "combined": share + APW_WEIGHT * apw_norm,
        "roc_auc": roc_auc_score(y, p, sample_weight=w),
        "ap": average_precision_score(y, p, sample_weight=w),
        "log_loss": log_loss(y, np.clip(p, 1e-7, 1 - 1e-7), sample_weight=w),
    }


def fit_lgbm(params: dict, tr: pd.DataFrame, feats: list[str]) -> lgb.LGBMClassifier:
    model = lgb.LGBMClassifier(**params, verbose=-1, random_state=SEED, n_jobs=4)
    return model.fit(tr[feats], tr["is_fraud"], sample_weight=tr["w"])


def lgbm_space(trial: optuna.Trial) -> dict:
    return {
        "n_estimators": trial.suggest_int("n_estimators", 50, 600, log=True),
        "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
        "num_leaves": trial.suggest_int("num_leaves", 4, 128, log=True),
        "min_child_samples": trial.suggest_int("min_child_samples", 50, 3000, log=True),
        "subsample": trial.suggest_float("subsample", 0.4, 1.0),
        "subsample_freq": 1,
        "colsample_bytree": trial.suggest_float("colsample_bytree", 0.3, 1.0),
        "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 100, log=True),
    }


def logreg(c: float, tr: pd.DataFrame, d: pd.DataFrame, feats: list[str]) -> np.ndarray:
    num = [f for f in feats if f not in CATEGORICAL]
    scaler = StandardScaler().fit(tr[num].fillna(0))
    model = LogisticRegression(C=c, max_iter=500)
    model.fit(scaler.transform(tr[num].fillna(0)), tr["is_fraud"], sample_weight=tr["w"])
    return model.predict_proba(scaler.transform(d[num].fillna(0)))[:, 1]


def constant(tr: pd.DataFrame, d: pd.DataFrame, by: str | None = None) -> np.ndarray:
    """Train fraud rate, overall or per group: a model that knows nothing about the row."""
    if by is None:
        return np.full(len(d), (tr.is_fraud * tr.w).sum() / tr.w.sum())
    rates = (tr.is_fraud * tr.w).groupby(tr[by]).sum() / tr.w.groupby(tr[by]).sum()
    return d[by].map(rates).to_numpy(dtype=float)


def blocks_from(tr: pd.DataFrame) -> BayesianBlocksCalibrator:
    """bayes_blocks fitted on a (weighted) slice of train: the Factored reference on tune."""
    scored = tr[tr.fs.notna()]
    g = scored.assign(k=scored.is_fraud * scored.w).groupby("fs")[["w", "k"]].sum()
    unscored = tr[tr.fs.isna()]
    counts = ScoreCounts(
        g.index.to_numpy(float),
        g["w"].to_numpy(float),
        g["k"].to_numpy(float),
        unscored_n=float(unscored.w.sum()),
        unscored_k=float((unscored.is_fraud * unscored.w).sum()),
    )
    return BayesianBlocksCalibrator.fit(counts)


def paired_bootstrap(
    d: pd.DataFrame, p_a: np.ndarray, p_b: np.ndarray, costs: Costs, rng: np.random.Generator
) -> tuple[float, float, float]:
    """Net benefit of A minus B on the same rows, with a 95% bootstrap interval."""
    y, a = d["is_fraud"].to_numpy(), d["amount_usd"].to_numpy()

    def net(p: np.ndarray, i: np.ndarray) -> float:
        return decision_metrics(y[i], a[i], p[i], costs)["net_benefit_usd"]

    full = np.arange(len(d))
    diffs = [
        net(p_a, i) - net(p_b, i) for i in (rng.integers(0, len(d), len(d)) for _ in range(N_BOOT))
    ]
    return net(p_a, full) - net(p_b, full), *np.quantile(diffs, [0.025, 0.975])


def run(exp: Experiment, data: dict[str, pd.DataFrame], costs: Costs, lines: list[str]) -> None:
    fit, tune, val, test = data["fit"], data["tune"], data["validation"], data["test"]
    train = pd.concat([fit, tune])
    feats = exp.features
    rng = np.random.default_rng(SEED)
    log = lambda s: print(f"[{exp.key}] {s}", flush=True)  # noqa: E731

    lines += [f"## {exp.title}", "", f"Features: {len(feats)}. Rows (frauds): "
              + ", ".join(f"{s} {len(d):,} ({int(d.is_fraud.sum())})" for s, d in data.items()),
              "", "### Search on the tune split", "",
              "| Candidate | Best combined | Trials to reach it |", "|---|---:|---:|"]  # fmt: skip
    for name, ref in exp.references.items():
        m = evaluate(tune, ref(fit, tune), costs)
        lines.append(f"| reference: {name} | {m['combined']:.4f} | - |")
        log(f"tune reference {name}: combined={m['combined']:.4f}")
    studies = {}
    for name, sampler in (
        ("LightGBM, TPE (Bayesian optimization)", optuna.samplers.TPESampler(seed=SEED)),
        ("LightGBM, random search", optuna.samplers.RandomSampler(seed=SEED)),
    ):
        study = optuna.create_study(direction="maximize", sampler=sampler)
        study.optimize(
            lambda t: evaluate(
                tune, fit_lgbm(lgbm_space(t), fit, feats).predict_proba(tune[feats])[:, 1], costs
            )["combined"],
            n_trials=N_TRIALS,
        )
        studies[name] = study
        lines.append(
            f"| {name} | {study.best_value:.4f} | {study.best_trial.number + 1} of {N_TRIALS} |"
        )
        log(f"{name}: best combined={study.best_value:.4f}")
    lr = {c: evaluate(tune, logreg(c, fit, tune, feats), costs)["combined"] for c in LOGREG_C}
    best_c = max(lr, key=lr.get)
    lines.append(
        f"| Logistic regression, best C = {best_c} | {lr[best_c]:.4f} | {len(LOGREG_C)} values |"
    )
    best = max(studies, key=lambda n: studies[n].best_value)
    params = {**studies[best].best_params, "subsample_freq": 1}
    shown = ", ".join(
        f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in params.items()
    )
    lines += ["", f"Chosen LightGBM ({best}): {shown}.", ""]

    lgbm_model = fit_lgbm(params, train, feats)
    for split, d in (("validation", val), ("test", test)):
        preds = {name: ref(train, d) for name, ref in exp.references.items()}
        preds["LightGBM tuned"] = lgbm_model.predict_proba(d[feats])[:, 1]
        preds[f"Logistic regression (C = {best_c})"] = logreg(best_c, train, d, feats)
        lines += [f"### {split} (touched once)", "",
                  "| Model | Net benefit (USD) | Share of oracle | Frauds contacted | Contacts "
                  "| ROC-AUC | AP | Dollar-weighted AP (norm.) | Log loss |",
                  "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]  # fmt: skip
        for name, p in preds.items():
            m = evaluate(d, p, costs)
            lines.append(
                f"| {name} | {m['net_benefit_usd']:,.0f} | {m['share_of_oracle']:.3f} "
                f"| {m['frauds_contacted']} / {m['frauds']} | {m['contacts']:,} | {m['roc_auc']:.4f} "
                f"| {m['ap']:.5f} | {m['apw_norm']:.4f} | {m['log_loss']:.6f} |"
            )
        lines += ["", "| Net benefit difference | USD | Bootstrap 95% |", "|---|---:|---|"]
        for name in exp.references:
            diff, lo, hi = paired_bootstrap(d, preds["LightGBM tuned"], preds[name], costs, rng)
            verdict = "better" if lo > 0 else "worse" if hi < 0 else "no difference"
            lines.append(
                f"| LightGBM tuned - {name} | {diff:+,.0f} | [{lo:+,.0f}, {hi:+,.0f}] ({verdict}) |"
            )
            log(f"{split}: LightGBM - {name} = {diff:+,.0f} [{lo:+,.0f}, {hi:+,.0f}]")
        lines.append("")


def main() -> None:
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    settings = load_settings()
    start = time.monotonic()
    costs = load_costs(connect(settings), settings)
    model = BayesianBlocksCalibrator.from_dict(json.loads(settings.fraud_model.read_text()))
    edge = model.blocks[0].high  # learned on train: above it, fraud is certain
    df = load(settings)
    behavioral = [c for c in df.columns if c not in NOT_FEATURES | {"score", "unscored"}]
    splits = ("fit", "tune", "validation", "test")

    def committed(tr: pd.DataFrame, d: pd.DataFrame) -> np.ndarray:
        # On tune, a calibrator fitted on fit only; at the end, the committed model.
        is_final = d["split"].iloc[0] in ("validation", "test")
        return (model if is_final else blocks_from(tr)).predict(d["fs"].to_numpy())

    experiments = [
        (
            Experiment(
                "A",
                "A. Without fraud_score: behavioral features only, all transactions",
                behavioral,
                {
                    "no skill (train fraud rate)": constant,
                    "Factored fraud_score, calibrated (bayes_blocks)": committed,
                },
            ),
            df,
        ),
        (
            Experiment(
                "B",
                f"B. Hybrid: tuned model only where fraud_score <= {edge:.2f} or missing",
                [*behavioral, "score", "unscored"],
                {"bayes_blocks in this range": lambda tr, d: constant(tr, d, by="unscored")},
            ),
            df[df.fs.isna() | (df.fs <= edge)],
        ),
    ]
    lines = [
        "# Model search: can tuned ML beat the calibrated fraud_score?",
        "",
        "Generated by `make model-search` (`src/bianque/evaluation/model_search.py`); the",
        "protocol and the metric definitions are in the module docstring and in",
        "[`docs/model_design.md`](../docs/model_design.md) §4. Offline measurements; net",
        "benefit is an **offline simulation** with synthetic costs: contact if p x amount_usd >",
        f"{costs.contact:.4f} (SMS) + {costs.friction:.2f} (friction, `{costs.assumptions_version}`).",
        "",
        f"Tuning: {N_TRIALS} trials per sampler on the tune split (last {TUNE_DAYS} days of train);",
        f"train negatives sampled at {NEG_FRACTION:.0%} and weighted back; validation and test",
        f"complete and used once. Paired bootstrap: {N_BOOT} resamples. Seed {SEED}.",
        "",
    ]
    for exp, frame in experiments:
        run(exp, {s: frame[frame.split == s] for s in splits}, costs, lines)
    lines.append(f"_Run time: {time.monotonic() - start:.0f} s_")
    REPORT.write_text("\n".join(lines) + "\n")
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
