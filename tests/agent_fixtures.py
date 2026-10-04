"""A tiny serving slice and policy for agent tests: two customers, a few charges."""

from pathlib import Path

import duckdb

from bianque.agent.store import CaseStore
from bianque.agent.tools import Tools
from bianque.policy.engine import load_policy

POLICY = load_policy(
    Path("policies/contact_policy_v1.yaml"), Path("policies/cost_assumptions_v1.yaml")
)


def build_slice(path: Path) -> Path:
    if path.exists():
        return path
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE customers AS SELECT * FROM (VALUES
            ('CLI-A', 'Android App', 0, 'Mexico'),
            ('CLI-B', 'Desktop Web', 3, 'Colombia')
        ) AS t(customer_id, main_digital_channel, n_complaints_365d, country);
        CREATE TABLE products AS SELECT * FROM (VALUES
            ('PRD-A1', 'CLI-A', 'Tarjeta Crédito'),
            ('PRD-B1', 'CLI-B', 'Tarjeta Débito')
        ) AS t(product_id, customer_id, product_type);
        CREATE TABLE transactions AS SELECT * FROM (VALUES
            ('TX-A-FRAUD', 'CLI-A', 'PRD-A1', TIMESTAMP '2026-06-15 21:04:00', 120.0,
             'Gasolinera Sol', 0.9997, 0.9985, 1.0),
            ('TX-A-BIG', 'CLI-A', 'PRD-A1', TIMESTAMP '2026-06-14 10:00:00', 6500.0,
             'Electro Max', 0.9997, 0.9985, 1.0),
            ('TX-A-NORMAL', 'CLI-A', 'PRD-A1', TIMESTAMP '2026-06-10 12:00:00', 45.5,
             'Cafe Luna', 0.0003, 0.00028, 0.00033),
            ('TX-B-FRAUD', 'CLI-B', 'PRD-B1', TIMESTAMP '2026-06-16 09:30:00', 300.0,
             'Tienda Norte', 0.9997, 0.9985, 1.0)
        ) AS t(transaction_id, customer_id, product_id, transaction_date, amount_usd,
               merchant_name, p_fraud, p_fraud_low, p_fraud_high);
        CREATE TABLE channel_costs AS SELECT * FROM (VALUES
            ('Push', 0.0006, 0.40), ('SMS', 0.1063, 0.50), ('Email', 0.0059, 0.30)
        ) AS t(channel, cost_per_delivered, open_rate);
        CREATE TABLE agent_routing AS SELECT * FROM (VALUES
            ('AGT-ES', true, true, false, false, true, 0.9),
            ('AGT-PT', true, false, true, false, true, 0.8),
            ('AGT-PT2', true, false, true, false, false, 0.95)
        ) AS t(agent_id, is_available, speaks_es, speaks_pt, speaks_en, is_fraud_specialist,
               first_contact_resolution_rate_90d);
    """)
    con.close()
    return path


def make_tools(tmp_path: Path, fail: frozenset[str] = frozenset()) -> Tools:
    return Tools(
        serving_db=build_slice(tmp_path / "slice.duckdb"),
        store=CaseStore(tmp_path / "cases.sqlite"),
        policy=POLICY,
        fail=fail,
    )
