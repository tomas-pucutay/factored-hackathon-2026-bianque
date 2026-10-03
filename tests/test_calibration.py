import numpy as np
import pytest

from bianque.models.calibration import (
    BayesianBlocksCalibrator,
    HistogramCalibrator,
    IsotonicCalibrator,
    RawScoreCalibrator,
    ScoreCounts,
)


def step_counts() -> ScoreCounts:
    """Rate 0.001 for scores 0-30.00, 1.0 above: the shape of the real fraud_score."""
    low = np.round(np.arange(0, 30.01, 0.5), 2)
    high = np.round(np.arange(30.5, 100, 0.5), 2)
    scores = np.concatenate([low, high])
    n = np.concatenate([np.full(len(low), 10_000), np.full(len(high), 5)])
    k = np.concatenate([np.full(len(low), 10), np.full(len(high), 5)])
    return ScoreCounts(scores, n, k, unscored_n=50_000, unscored_k=50)


def test_bayes_blocks_finds_the_step_edge():
    model = BayesianBlocksCalibrator.fit(step_counts())

    assert [(b.low, b.high) for b in model.blocks] == [(0.0, 30.0), (30.5, 99.5)]
    p = model.predict(np.array([10.0, 30.0, 30.4, 31.0, np.nan]))
    assert p[0] == pytest.approx(0.001, rel=0.05)
    assert p[1] == p[0]  # 30.00 belongs to the low block
    assert p[2] > 0.95  # past the midpoint between 30.0 and 30.5: high block
    assert p[3] > 0.95
    assert p[4] == pytest.approx(50.5 / 50_001)  # unscored block, Jeffreys prior


def test_bayes_blocks_merges_a_flat_rate():
    scores = np.arange(0, 100.0)
    counts = ScoreCounts(scores, np.full(100, 1_000), np.full(100, 1), 0, 0)

    model = BayesianBlocksCalibrator.fit(counts)

    assert len(model.blocks) == 1


def test_bayes_blocks_interval_contains_mean_and_narrows_with_data():
    model = BayesianBlocksCalibrator.fit(step_counts())
    scores = np.array([10.0, 50.0])

    lo, hi = model.interval(scores)
    p = model.predict(scores)

    assert np.all(lo <= p) and np.all(p <= hi)
    # 610k rows in the low block vs 695 in the high block
    assert (hi - lo)[0] < (hi - lo)[1]


def test_bayes_blocks_round_trip():
    model = BayesianBlocksCalibrator.fit(step_counts())
    scores = np.array([0.0, 29.9, 30.0, 75.0, np.nan])

    restored = BayesianBlocksCalibrator.from_dict(model.to_dict())

    assert np.array_equal(restored.predict(scores), model.predict(scores))


def test_isotonic_is_monotone_and_uses_unscored_rate():
    model = IsotonicCalibrator.fit(step_counts())

    p = model.predict(np.array([0.0, 20.0, 40.0, 99.0, np.nan]))

    assert np.all(np.diff(p[:4]) >= 0)
    assert p[4] == pytest.approx(0.001)


def test_histogram_and_raw_baselines():
    hist = HistogramCalibrator({0: 0.1, 1: 0.9, None: 0.05}, bin_width=50)
    raw = RawScoreCalibrator()
    scores = np.array([10.0, 99.99, 100.0, np.nan])

    assert hist.predict(scores).tolist() == [0.1, 0.9, 0.9, 0.05]
    assert raw.predict(scores).tolist() == pytest.approx([0.1, 0.9999, 1.0, 0.0])
