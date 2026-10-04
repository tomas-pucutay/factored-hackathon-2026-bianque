"""Compare contact policies on the frozen sets (make evaluate): reports/policy_comparison.md.

Runs the real policy engine (bianque.policy.engine.decide, the code the API uses) over every
transaction of the frozen validation and test sets, scored by the trained calibrator, in time
order (so the per-customer contact cap applies). Policy thresholds are compared on validation;
test is reported, never used to choose.

OFFLINE SIMULATION. Assumptions, stated in the report:
  - A contacted fraud's loss is fully avoided; a contacted legitimate customer costs the
    synthetic friction.
  - Alerts are automated. A legitimate customer answers "it's mine" and the case closes
    without a human. A human costs one outbound agent call (measured handle time x synthetic
    cost per minute, gold.service_cost_baseline, Outbound Call, Transaccional) for every human
    review and for every dispute ("not mine", i.e. a fraud) the policy escalates.
  - Customer context (app user, complaints in 365 days) comes from gold.customer_360, a
    snapshot at the end of the data: it is used for routing and channel, not for detection.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from bianque.config import Settings, load_settings
from bianque.evaluation.frozen_sets import sha256
from bianque.evaluation.label_signal import connect
from bianque.models.calibration import BayesianBlocksCalibrator
from bianque.policy.engine import (
    ChannelStats,
    Charge,
    CustomerContext,
    Policy,
    decide,
    load_policy,
)

REPORT = Path("reports/policy_comparison.md")
SETS = ("fraud_validation", "fraud_test")
MIN_P_GRID = (0.0, 0.0005, 0.001, 0.002, 0.01, 0.1, 0.5)
FRICTIONS = (1.5, 2.0, 2.5)  # "near USD 2": the chosen minimum must hold across this range
GROUPS = ("segment", "customer_country", "age_band")
N_BOOT = 1000
SEED = 0


def load_frozen(settings: Settings) -> dict[str, pd.DataFrame]:
    """Frozen sets (hash checked) joined with the customer context the policy needs."""
    manifest = json.loads(settings.eval_manifest.read_text())
    con = connect(settings)
    customers = settings.gold_root / "customer_360" / "*.parquet"
    out = {}
    for name in SETS:
        info = manifest["sets"][name]
        path = settings.eval_manifest.parent / info["file"]
        if sha256(path) != info["sha256"]:
            raise SystemExit(f"{path} does not match its manifest hash; run make gold first")
        out[name] = con.sql(f"""
            SELECT f.transaction_id, f.customer_id, f.transaction_date,
                   f.amount_usd::DOUBLE AS amount_usd, f.source_fraud_score::DOUBLE AS score,
                   f.is_fraud::INTEGER AS is_fraud, f.segment, f.customer_country, f.age_band,
                   c.main_digital_channel, coalesce(c.n_complaints_365d, 0) AS complaints_365d
            FROM read_parquet('{path}') AS f
            LEFT JOIN read_parquet('{customers}') AS c USING (customer_id)
            ORDER BY f.transaction_date, f.transaction_id
        """).df()
    return out


def load_channels(settings: Settings) -> dict[str, ChannelStats]:
    rows = (
        connect(settings)
        .sql(f"""
        SELECT channel, cost_per_delivered, open_rate
        FROM read_parquet('{settings.gold_root}/channel_costs/*.parquet')
    """)
        .fetchall()
    )
    return {
        c: ChannelStats(c, float(cost), None if rate is None else float(rate))
        for c, cost, rate in rows
    }


def human_case_cost(settings: Settings) -> float:
    return float(
        connect(settings)
        .sql(f"""
        SELECT cost_per_contact_usd
        FROM read_parquet('{settings.gold_root}/service_cost_baseline/*.parquet')
        WHERE interaction_type = 'Outbound Call' AND reason_category = 'Transaccional'
    """)
        .fetchone()[0]
    )


def simulate(
    policy: Policy,
    df: pd.DataFrame,
    model: BayesianBlocksCalibrator,
    channels: dict[str, ChannelStats],
    human_cost: float,
) -> pd.DataFrame:
    """One row per transaction: the decision and its net benefit contribution (USD)."""
    scores = df["score"].to_numpy()
    p = model.predict(scores)
    lo, hi = model.interval(scores)
    amount = df["amount_usd"].to_numpy()
    action = np.full(len(df), "no_contact", dtype=object)
    handled = np.full(len(df), "", dtype=object)
    channel_cost = np.zeros(len(df))
    # Rows that cannot clear the friction even at the top of their interval never contact.
    candidates = np.flatnonzero(np.maximum(p, hi) * amount > policy.friction_usd)
    last_contact: dict[str, pd.Timestamp] = {}
    rows = df.iloc[candidates][["transaction_id", "customer_id", "transaction_date",
                                "main_digital_channel", "complaints_365d"]]  # fmt: skip
    for i, row in zip(candidates, rows.itertuples(index=False), strict=True):  # time order
        prev = last_contact.get(row.customer_id)
        since = None if prev is None else (row.transaction_date - prev).total_seconds() / 3600
        customer = CustomerContext(row.main_digital_channel, int(row.complaints_365d), since)
        charge = Charge(row.transaction_id, amount[i], p[i], lo[i], hi[i])
        d = decide(policy, charge, customer, channels)
        action[i], handled[i] = d.action, d.handled_by or ""
        if d.action == "contact":
            channel_cost[i] = channels[d.channel].cost_per_delivered
            last_contact[row.customer_id] = row.transaction_date
        elif d.action == "human_review":
            channel_cost[i] = channels["SMS"].cost_per_delivered  # the agent contacts by phone
    touched = action != "no_contact"
    fraud = df["is_fraud"].to_numpy() == 1
    # A human works the case: every review, and every escalated dispute (frauds say "not mine").
    human_case = (action == "human_review") | ((handled == "human") & fraud)
    value = (
        np.where(touched & fraud, amount, 0.0)
        - channel_cost
        - np.where(touched & ~fraud, policy.friction_usd, 0.0)
        - np.where(human_case, human_cost, 0.0)
    )
    return pd.DataFrame(
        {
            "action": action,
            "human_case": human_case,
            "fraud": fraud,
            "value": value,
            "amount": amount,
        }
    )


def summarize(sim: pd.DataFrame, oracle: float) -> dict:
    touched = sim.action != "no_contact"
    return {
        "alerts": int((sim.action == "contact").sum()),
        "cases_human": int(sim.human_case.sum()),
        "frauds": int(sim.fraud.sum()),
        "frauds_caught": int((touched & sim.fraud).sum()),
        "legit_contacted": int((touched & ~sim.fraud).sum()),
        "net_benefit": float(sim.value.sum()),
        "share_of_oracle": float(sim.value.sum() / oracle),
        "automated_share": float(1 - sim.human_case.sum() / max(touched.sum(), 1)),
    }


def paired_bootstrap(
    a: np.ndarray, b: np.ndarray, rng: np.random.Generator
) -> tuple[float, float, float]:
    delta = a - b
    boot = [delta[rng.integers(0, len(delta), len(delta))].sum() for _ in range(N_BOOT)]
    return float(delta.sum()), *np.quantile(boot, [0.025, 0.975])


def main() -> None:
    start = time.monotonic()
    settings = load_settings()
    v1 = load_policy(settings.contact_policy, settings.cost_assumptions)
    model = BayesianBlocksCalibrator.from_dict(json.loads(settings.fraud_model.read_text()))
    channels = load_channels(settings)
    human_cost = human_case_cost(settings)
    frozen = load_frozen(settings)
    never = float("inf")
    policies = {
        "EV rule only (no minimum, no abstention, no escalation, no cap)": replace(
            v1, min_p_fraud=0.0, abstain_when_interval_straddles=False, high_amount_usd=never,
            repeat_complainer_365d=10**9, max_contacts_per_customer_hours=0.0,
        ),
        f"{v1.version} with a 0.01 floor (no false alerts)": replace(v1, min_p_fraud=0.01),
        f"{v1.version}": v1,
    }  # fmt: skip
    rng = np.random.default_rng(SEED)
    lines = [
        "# Contact policy comparison",
        "",
        "Generated by `make evaluate` (`src/bianque/evaluation/policy_compare.py`), running the",
        "real policy engine over every transaction of the frozen sets, scored by",
        f"`{json.loads(settings.fraud_model.read_text())['model_version']}`. **Offline simulation**,",
        "not a production result. Thresholds are compared on validation; test is reported.",
        "",
        "## Assumptions",
        "",
        "| Item | Value |",
        "|------|-------|",
        f"| Policy | [`{settings.contact_policy}`](../{settings.contact_policy}) (SYNTHETIC) |",
        f"| Friction per legitimate customer contacted | USD {v1.friction_usd:.2f} (`{v1.cost_assumptions}`, SYNTHETIC) |",
        "| Channel cost | Per delivered message, gold.channel_costs (Push for app users, SMS otherwise) |",
        f"| Human case | One outbound agent call, USD {human_cost:.2f} (measured minutes x synthetic rate) |",
        "| A contacted fraud | Its loss is fully avoided |",
        "| Customer context | gold.customer_360 snapshot (routing and channel only, not detection) |",
        "",
    ]  # fmt: skip
    sims: dict[str, dict[str, pd.DataFrame]] = {split: {} for split in SETS}
    for split, df in frozen.items():
        fraud_amount = df.loc[df.is_fraud == 1, "amount_usd"].to_numpy()
        oracle = float(fraud_amount.sum() - len(fraud_amount) * channels["Push"].cost_per_delivered)
        lines += [
            f"## {split}: {len(df):,} transactions, {int(df.is_fraud.sum())} frauds",
            "",
            "| Policy | Automated alerts | Cases for a human | Frauds caught | Legit contacted "
            "| Net benefit (USD) | Share of oracle | Automated share |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
            "| No proactive contact (status quo) | 0 | 0 | 0 | 0 | 0 | 0.000 | - |",
        ]
        for name, policy in policies.items():
            sim = simulate(policy, df, model, channels, human_cost)
            sims[split][name] = sim
            m = summarize(sim, oracle)
            lines.append(
                f"| {name} | {m['alerts']:,} | {m['cases_human']:,} "
                f"| {m['frauds_caught']} / {m['frauds']} | {m['legit_contacted']:,} "
                f"| {m['net_benefit']:,.0f} | {m['share_of_oracle']:.3f} | {m['automated_share']:.3f} |"
            )
            print(f"{split} {name}: {m}", flush=True)
        names = list(policies)
        lines += ["", "| Difference | USD | Bootstrap 95% |", "|---|---:|---|"]
        for other in names[:-1]:
            diff, lo, hi = paired_bootstrap(
                sims[split][names[-1]].value.to_numpy(), sims[split][other].value.to_numpy(), rng
            )
            lines.append(f"| {names[-1]} - {other} | {diff:+,.0f} | [{lo:+,.0f}, {hi:+,.0f}] |")
        lines.append("")

    # Minimum probability: chosen on validation at the assumed friction, checked near it.
    val = frozen["fraud_validation"]
    grid = {
        (mp, f): summarize(
            simulate(replace(v1, min_p_fraud=mp, friction_usd=f), val, model, channels, human_cost),
            1.0,
        )
        for mp in MIN_P_GRID
        for f in FRICTIONS
    }
    best = {f: max(MIN_P_GRID, key=lambda mp, f=f: grid[(mp, f)]["net_benefit"]) for f in FRICTIONS}
    lines += [
        "## Choosing the minimum probability (validation)",
        "",
        "The expected-value rule already sets a threshold per charge: (channel cost + friction) /",
        "amount. The minimum is a floor on top of it. Every other rule as in the policy; net",
        "benefit at each friction near the assumed USD 2.",
        "",
        "| min_p_fraud | Frauds caught | Legit contacted | "
        + " | ".join(f"Net benefit at USD {f:.2f}" for f in FRICTIONS)
        + " |",
        "|---:|---:|---:|" + "---:|" * len(FRICTIONS),
    ]
    for mp in MIN_P_GRID:
        g = grid[(mp, v1.friction_usd)]
        cells = " | ".join(
            f"**{grid[(mp, f)]['net_benefit']:,.0f}**" if best[f] == mp
            else f"{grid[(mp, f)]['net_benefit']:,.0f}"
            for f in FRICTIONS
        )  # fmt: skip
        lines.append(
            f"| {mp:g} | {g['frauds_caught']} / {g['frauds']} | {g['legit_contacted']:,} | {cells} |"
        )
    chosen = best[v1.friction_usd]
    stable = all(
        grid[(chosen, f)]["net_benefit"] == grid[(best[f], f)]["net_benefit"] for f in FRICTIONS
    )
    lines += [
        "",
        f"Best minimum at USD {v1.friction_usd:.2f}: **{chosen:g}**"
        + (" (also the best, or tied, at every friction checked)." if stable else
           f" (not the best at every friction checked: {best})."),
        f"The policy uses **{v1.min_p_fraud:g}**"
        + (" (the chosen minimum)." if grid[(v1.min_p_fraud, v1.friction_usd)]["net_benefit"]
           == grid[(chosen, v1.friction_usd)]["net_benefit"] else
           ": it differs from the best on validation; update the policy."),
    ]  # fmt: skip

    # Fairness: outcomes of the chosen policy by group (test).
    test = frozen["fraud_test"]
    sim = sims["fraud_test"][names[-1]]
    d = test.assign(
        touched=(sim.action != "no_contact").to_numpy(),
        human=sim.human_case.to_numpy(),
    )
    lines += [
        "",
        f"## By group (test, `{v1.version}`)",
        "",
        "Fraud recall, share of legitimate transactions contacted, and share of handled cases",
        "that go to a human. Groups with few frauds are noisy (20 frauds: recall ±0.2). All",
        "customers in the data are in Spanish-speaking countries; Portuguese is evaluated with",
        "team-generated conversations (agent evaluation).",
        "",
        "| Group | Value | Transactions | Frauds | Fraud recall | Legit contact rate | Human share |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]  # fmt: skip
    for group in GROUPS:
        for value, g in d.groupby(group, dropna=False):
            frauds = g[g.is_fraud == 1]
            recall = f"{frauds.touched.mean():.3f}" if len(frauds) else "-"
            human = f"{g[g.touched].human.mean():.3f}" if g.touched.any() else "-"
            lines.append(
                f"| {group} | {'NULL' if pd.isna(value) else value} | {len(g):,} | {len(frauds)} "
                f"| {recall} | {g[g.is_fraud == 0].touched.mean():.6f} | {human} |"
            )
    lines += ["", f"_Run time: {time.monotonic() - start:.0f} s_"]
    REPORT.write_text("\n".join(lines) + "\n")
    print(f"wrote {REPORT}")


if __name__ == "__main__":
    main()
