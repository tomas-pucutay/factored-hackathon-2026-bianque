"""Label signal gate: is there anything a model can learn before we train one?

Run before the model step (make label-signal); the results and the decision they drive are
in reports/label_signal.md and docs/adr/0001-fraud-signal-gate.md.

Checks, all on the out-of-time splits of settings (train < train_end <= validation <
validation_end <= test < test_end):
  1. The bank's fraud_score as a ranking (is it a copy of the label?).
  2. LightGBM on the point-in-time behavioral features only, with a bootstrap interval and a
     permutation null for its PR-AUC (is there learnable signal beyond the score?).
  3. LightGBM on behavioral features + fraud_score (does behavior add to the score?).
  4. Isolation Forest on the numeric behavioral features (unsupervised baseline).
  5. One step of sequential forward selection from {fraud_score}: the gain of adding each
     feature alone (a Markov blanket check: does fraud_score shield the label from the rest?).
  6. Transcript text vs the interaction's reason (is there a label for an intent classifier?).

Train negatives are sampled at NEG_FRACTION (every fraud kept) and weighted back, so the
run fits in a few GB of RAM; validation and test are complete.
"""

from __future__ import annotations

import time

import duckdb
import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from sklearn.metrics import average_precision_score as pr_auc

from bianque.config import Settings, load_settings

NEG_FRACTION = 0.1
SEED = 0
N_RESAMPLES = 200
CATEGORICAL = [
    "currency", "channel", "transaction_type", "transaction_category", "transaction_country",
    "segment", "customer_country", "age_band", "product_type",
]  # fmt: skip
NOT_FEATURES = {
    "transaction_id", "customer_id", "product_id", "transaction_date", "process_date",
    "process_month", "source_fraud_score", "is_fraud", "score", "split", "weight",
}  # fmt: skip
LGBM = dict(
    n_estimators=300, learning_rate=0.05, num_leaves=31, min_child_samples=200, subsample=0.5,
    subsample_freq=1, colsample_bytree=0.8, verbose=-1, random_state=SEED, n_jobs=4,
)  # fmt: skip
LGBM_SMALL = dict(
    n_estimators=100, learning_rate=0.1, num_leaves=15, min_child_samples=200, verbose=-1,
    random_state=SEED, n_jobs=4,
)  # fmt: skip


def connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{settings.duckdb_memory_limit}'")
    con.execute(f"SET threads = {settings.duckdb_threads}")
    return con


def split_case(settings: Settings) -> str:
    return f"""CASE WHEN process_date < DATE '{settings.eval_train_end}' THEN 'train'
                    WHEN process_date < DATE '{settings.eval_validation_end}' THEN 'validation'
                    ELSE 'test' END"""


def load_features(settings: Settings) -> tuple[pd.DataFrame, list[str]]:
    con = connect(settings)
    keep = int(NEG_FRACTION * 1000)
    df = con.sql(f"""
        SELECT *, {split_case(settings)} AS split
        FROM read_parquet('{settings.gold_root}/transaction_features/**/*.parquet',
                          hive_partitioning = true)
        WHERE process_date < DATE '{settings.eval_test_end}'
          AND (process_date >= DATE '{settings.eval_train_end}'
               OR is_fraud OR hash(transaction_id) % 1000 < {keep})
    """).df()
    df["is_fraud"] = df["is_fraud"].astype(np.int8)
    df["score"] = df["source_fraud_score"].astype("float64").astype(np.float32)
    for col in df.columns:
        if col in CATEGORICAL:
            df[col] = df[col].astype("category")
        elif col not in NOT_FEATURES:
            df[col] = pd.to_numeric(df[col].astype("float64")).astype(np.float32)
    is_sampled_negative = (df["split"] == "train") & (df["is_fraud"] == 0)
    df["weight"] = np.where(is_sampled_negative, 1 / NEG_FRACTION, 1.0)
    features = [c for c in df.columns if c not in NOT_FEATURES]
    return df, features


def metrics_row(name: str, y: pd.Series, p: np.ndarray) -> str:
    return (
        f"| {name} | {len(y):,} | {int(y.sum())} | {y.mean():.5f} "
        f"| {roc_auc_score(y, p):.4f} | {pr_auc(y, p):.4f} |"
    )


def fit(df: pd.DataFrame, cols: list[str], params: dict) -> lgb.LGBMClassifier:
    return lgb.LGBMClassifier(**params).fit(df[cols], df["is_fraud"], sample_weight=df["weight"])


def fraud_checks(settings: Settings) -> None:
    df, features = load_features(settings)
    train, valid, test = (df[df["split"] == s] for s in ("train", "validation", "test"))
    rng = np.random.default_rng(SEED)

    print("## Splits\n\n| Split | Rows | Fraud | Rate |\n|---|---:|---:|---:|")
    print(f"| train (negatives sampled at {NEG_FRACTION:.0%}) | {len(train):,} "
          f"| {train.is_fraud.sum()} | n/a |")  # fmt: skip
    for name, d in (("validation", valid), ("test", test)):
        print(f"| {name} | {len(d):,} | {d.is_fraud.sum()} | {d.is_fraud.mean():.5f} |")

    print("\n## Rankers\n\n| Ranker | Rows | Fraud | Base rate | ROC-AUC | PR-AUC |")
    print("|---|---:|---:|---:|---:|---:|")
    behavioral = fit(train, features, LGBM)
    combined = fit(train, [*features, "score"], LGBM)
    numeric = [c for c in features if c not in CATEGORICAL]
    negatives = train[train.is_fraud == 0].sample(200_000, random_state=SEED)
    iso = IsolationForest(n_estimators=200, max_samples=4096, random_state=SEED, n_jobs=4)
    iso.fit(negatives[numeric].fillna(-1).to_numpy(np.float32))
    for name, d in (("validation", valid), ("test", test)):
        scored = d["score"].notna()
        print(metrics_row(f"[{name}] fraud_score, NULL as 0", d.is_fraud, d.score.fillna(0)))
        print(metrics_row(f"[{name}] fraud_score, scored rows only", d.is_fraud[scored],
                          d.score[scored]))  # fmt: skip
        print(metrics_row(f"[{name}] LightGBM, behavioral features", d.is_fraud,
                          behavioral.predict_proba(d[features])[:, 1]))  # fmt: skip
        print(metrics_row(f"[{name}] LightGBM, behavioral + fraud_score", d.is_fraud,
                          combined.predict_proba(d[[*features, "score"]])[:, 1]))  # fmt: skip
        iso_score = -iso.score_samples(d[numeric].fillna(-1).to_numpy(np.float32))
        print(metrics_row(f"[{name}] Isolation Forest", d.is_fraud, iso_score))

    unscored_train, unscored_valid = train[train.score.isna()], valid[valid.score.isna()]
    unscored = fit(unscored_train, features, LGBM)
    print(metrics_row("[validation] LightGBM, behavioral, rows without fraud_score",
                      unscored_valid.is_fraud,
                      unscored.predict_proba(unscored_valid[features])[:, 1]))  # fmt: skip
    band_train = train[train.score.between(25, 40)]
    band_valid = valid[valid.score.between(25, 40)]
    band = fit(band_train, [*features, "score"], {**LGBM, "n_estimators": 150})
    print(metrics_row("[validation] fraud_score, ambiguous band 25-40", band_valid.is_fraud,
                      band_valid.score))  # fmt: skip
    print(metrics_row("[validation] LightGBM, behavioral + fraud_score, band 25-40",
                      band_valid.is_fraud,
                      band.predict_proba(band_valid[[*features, "score"]])[:, 1]))  # fmt: skip

    y = valid["is_fraud"].to_numpy()
    p = behavioral.predict_proba(valid[features])[:, 1]
    observed = pr_auc(y, p)
    boot = [
        pr_auc(y[i], p[i]) for i in (rng.integers(0, len(y), len(y)) for _ in range(N_RESAMPLES))
    ]
    perm = np.array([pr_auc(rng.permutation(y), p) for _ in range(N_RESAMPLES)])
    print(f"\n## Behavioral model vs chance (validation, {N_RESAMPLES} resamples)\n")
    print("| PR-AUC | Bootstrap 95% | Permutation null 95% | p-value |\n|---:|---|---|---:|")
    print(f"| {observed:.5f} | [{np.quantile(boot, 0.025):.5f}, {np.quantile(boot, 0.975):.5f}] "
          f"| [{np.quantile(perm, 0.025):.5f}, {np.quantile(perm, 0.975):.5f}] "
          f"| {np.mean(perm >= observed):.2f} |")  # fmt: skip

    base = pr_auc(y, fit(train, ["score"], LGBM_SMALL).predict_proba(valid[["score"]])[:, 1])
    gains = {
        f: pr_auc(y, fit(train, ["score", f], LGBM_SMALL).predict_proba(valid[["score", f]])[:, 1])
        - base
        for f in features
    }
    score_boot = [
        pr_auc(y[i], valid["score"].fillna(0).to_numpy()[i])
        for i in (rng.integers(0, len(y), len(y)) for _ in range(N_RESAMPLES))
    ]
    ranked = pd.Series(gains).sort_values(ascending=False)
    print(f"\n## Forward selection from {{fraud_score}} (validation PR-AUC {base:.4f})\n")
    print(f"Bootstrap 95% width of the fraud_score PR-AUC: "
          f"{np.quantile(score_boot, 0.975) - np.quantile(score_boot, 0.025):.4f}\n")  # fmt: skip
    print("| Added feature | PR-AUC gain |\n|---|---:|")
    for f, g in pd.concat([ranked.head(5), ranked.tail(3)]).items():
        print(f"| {f} | {g:+.4f} |")


def intent_label_check(settings: Settings) -> None:
    con = connect(settings)
    silver = settings.silver_root
    df = con.sql(f"""
        SELECT t.customer_text, t.detected_intents, i.reason_category,
               {split_case(settings).replace("process_date", "i.process_date")} AS split
        FROM read_parquet('{silver}/call_transcripts/**/*.parquet') AS t
        JOIN read_parquet('{silver}/call_center_interactions/**/*.parquet') AS i
            USING (interaction_id)
        WHERE t.customer_text IS NOT NULL AND i.reason_category IS NOT NULL
    """).df()
    train, test = df[df["split"] == "train"], df[df["split"] == "test"]
    vec = TfidfVectorizer(min_df=5, ngram_range=(1, 2))
    model = LogisticRegression(max_iter=300, C=4.0)
    model.fit(vec.fit_transform(train["customer_text"]), train["reason_category"])
    pred = model.predict(vec.transform(test["customer_text"]))
    majority = train["reason_category"].mode()[0]
    print("\n## Transcript text vs interaction reason (test)\n")
    print(f"- Transcripts: {len(df):,}; distinct customer texts: {df.customer_text.nunique()}")
    print(f"- detected_intents values: {df.detected_intents.value_counts(dropna=False).to_dict()}")
    print(f"- TF-IDF + logistic regression: accuracy {accuracy_score(test.reason_category, pred):.3f}, "
          f"macro F1 {f1_score(test.reason_category, pred, average='macro'):.3f}; "
          f"majority class accuracy {(test.reason_category == majority).mean():.3f} "
          f"({train.reason_category.nunique()} classes)")  # fmt: skip


def main() -> None:
    settings = load_settings()
    start = time.monotonic()
    fraud_checks(settings)
    intent_label_check(settings)
    print(f"\n_Run time: {time.monotonic() - start:.0f} s_")


if __name__ == "__main__":
    main()
