import numpy as np
import pytest

from bianque.models.train import Costs, decision_metrics, probability_metrics, select

COSTS = Costs(contact=0.10, friction=2.00, assumptions_version="test")


def test_decision_metrics_applies_the_expected_value_rule():
    y = np.array([1, 1, 0, 0])
    amount = np.array([100.0, 100.0, 100.0, 1.0])
    p = np.array([0.9, 0.01, 0.5, 0.9])  # hurdle 2.10: contact rows 0 and 2 only

    m = decision_metrics(y, amount, p, COSTS)

    assert (m["contacts"], m["frauds_contacted"], m["legit_contacted"]) == (2, 1, 1)
    assert m["loss_avoided_usd"] == 100.0
    assert m["net_benefit_usd"] == pytest.approx(100.0 - 2 * 0.10 - 2.00)
    assert m["recall"] == 0.5
    assert "abstained" not in m


def test_decision_metrics_counts_intervals_straddling_break_even():
    y = np.array([1, 0, 0])
    amount = np.array([100.0, 100.0, 100.0])  # break-even p = 0.021
    p = np.array([0.5, 0.02, 0.001])
    lo, hi = np.array([0.4, 0.01, 0.0005]), np.array([0.6, 0.03, 0.002])

    m = decision_metrics(y, amount, p, COSTS, (lo, hi))

    assert (m["abstained"], m["frauds_abstained"]) == (1, 0)


def test_probability_metrics_perfect_calibration_has_zero_ece():
    y = np.array([0] * 9 + [1] * 1 + [1] * 5)
    p = np.array([0.1] * 10 + [1.0] * 5)

    m = probability_metrics(y, p)

    assert m["ece"] == pytest.approx(0.0)
    assert m["roc_auc"] > 0.5


def test_select_uses_validation_net_benefit_then_log_loss():
    def r(net, ll, test_net):
        return {
            "fraud_validation": {"net_benefit_usd": net, "log_loss": ll},
            "fraud_test": {"net_benefit_usd": test_net, "log_loss": 0.0},
        }

    results = {"a": r(100.0, 0.2, 999.0), "b": r(100.0, 0.1, 0.0), "c": r(50.0, 0.0, 0.0)}

    assert select(results) == "b"  # tie on net benefit; lower log loss; test ignored


def test_decision_metrics_weights_sampled_rows():
    y = np.array([1, 0])
    amount = np.array([100.0, 100.0])
    p = np.array([0.5, 0.5])  # both contacted

    m = decision_metrics(y, amount, p, COSTS, weight=np.array([1.0, 10.0]))

    assert (m["contacts"], m["legit_contacted"]) == (11, 10)
    assert m["net_benefit_usd"] == pytest.approx(100.0 - 11 * 0.10 - 10 * 2.00)
