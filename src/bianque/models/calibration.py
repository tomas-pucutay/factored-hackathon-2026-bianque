"""Calibrators: map the bank's fraud_score (0-100, or missing) to a probability of fraud.

The signal gate (reports/label_signal.md, docs/adr/0001-fraud-signal-gate.md) showed that
fraud_score is the only fraud signal in the data and already ranks at the ceiling, so the
learned component is the map from score to probability. The expected-value contact rule needs
a real probability, and abstention needs to know how sure that probability is.

BayesianBlocksCalibrator (the proposed model):
  - Splits the score axis into contiguous blocks with a constant fraud rate, chosen by
    maximizing the Beta-Binomial marginal likelihood minus a penalty per block (optimal
    partitioning by dynamic programming, as in Bayesian Blocks). The data decides where the
    edges are; a fixed histogram cannot put an edge at, e.g., 30.00.
  - Each block's fraud rate has a Beta posterior (Jeffreys prior Beta(1/2, 1/2)): the
    prediction is its mean, and its credible interval drives abstention.
  - Transactions without a score are one more block.
The posterior is conjugate, so no sampling (MCMC) is needed.

IsotonicCalibrator is the frequentist comparator (monotone, no intervals). The two baselines
are the current histogram calibration in gold (baseline_fraud_score_v1) and the raw score read
as a probability (score / 100).

Every calibrator is fitted from aggregated counts: one row per distinct score with its
transactions (n) and frauds (k), plus the counts of unscored transactions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
from scipy.special import betaln
from scipy.stats import beta as beta_dist
from sklearn.isotonic import IsotonicRegression

JEFFREYS = 0.5


@dataclass(frozen=True)
class Block:
    low: float | None  # lowest score in the block (None for the unscored block)
    high: float | None
    n: int
    k: int


@dataclass(frozen=True)
class ScoreCounts:
    """Training data for a calibrator: counts per distinct score, and of unscored rows."""

    scores: np.ndarray  # sorted, distinct
    n: np.ndarray
    k: np.ndarray
    unscored_n: int
    unscored_k: int

    @property
    def total(self) -> int:
        return int(self.n.sum()) + self.unscored_n


def optimal_blocks(counts: ScoreCounts, penalty: float, a: float, b: float) -> list[Block]:
    """Partition the sorted scores into blocks maximizing sum(log evidence) - penalty * blocks.

    O(cells^2) dynamic program with cumulative counts; a few thousand distinct scores take
    under a second.
    """
    m = len(counts.scores)
    cum_n = np.concatenate([[0.0], np.cumsum(counts.n, dtype=float)])
    cum_k = np.concatenate([[0.0], np.cumsum(counts.k, dtype=float)])
    best = np.zeros(m + 1)
    start = np.zeros(m + 1, dtype=int)
    for j in range(1, m + 1):
        n = cum_n[j] - cum_n[:j]
        k = cum_k[j] - cum_k[:j]
        value = best[:j] + betaln(k + a, n - k + b) - betaln(a, b) - penalty
        start[j] = int(np.argmax(value))
        best[j] = value[start[j]]
    blocks, j = [], m
    while j > 0:
        i = start[j]
        blocks.append(
            Block(
                low=float(counts.scores[i]),
                high=float(counts.scores[j - 1]),
                n=int(cum_n[j] - cum_n[i]),
                k=int(cum_k[j] - cum_k[i]),
            )
        )
        j = i
    return blocks[::-1]


@dataclass(frozen=True)
class BayesianBlocksCalibrator:
    blocks: tuple[Block, ...]
    unscored: Block
    prior_a: float = JEFFREYS
    prior_b: float = JEFFREYS
    penalty: float = 0.0
    name: str = "bayes_blocks"

    @classmethod
    def fit(cls, counts: ScoreCounts, penalty: float | None = None) -> BayesianBlocksCalibrator:
        """penalty defaults to log(N), a BIC-like price for each extra block."""
        penalty = float(np.log(counts.total)) if penalty is None else penalty
        blocks = optimal_blocks(counts, penalty, JEFFREYS, JEFFREYS)
        unscored = Block(None, None, counts.unscored_n, counts.unscored_k)
        return cls(tuple(blocks), unscored, penalty=penalty)

    def _posterior(self, scores: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Beta posterior parameters for each score (NaN = unscored)."""
        n = np.array([b.n for b in self.blocks], dtype=float)
        k = np.array([b.k for b in self.blocks], dtype=float)
        # Boundaries halfway between one block's highest score and the next block's lowest.
        cuts = np.array(
            [(lo.high + hi.low) / 2 for lo, hi in zip(self.blocks, self.blocks[1:], strict=False)]
        )
        idx = np.searchsorted(cuts, np.nan_to_num(scores), side="right")
        alpha, beta = k[idx] + self.prior_a, n[idx] - k[idx] + self.prior_b
        missing = np.isnan(scores)
        alpha[missing] = self.unscored.k + self.prior_a
        beta[missing] = self.unscored.n - self.unscored.k + self.prior_b
        return alpha, beta

    def predict(self, scores: np.ndarray) -> np.ndarray:
        alpha, beta = self._posterior(np.asarray(scores, dtype=float))
        return alpha / (alpha + beta)

    def interval(self, scores: np.ndarray, level: float = 0.95) -> tuple[np.ndarray, np.ndarray]:
        """Equal-tailed credible interval of the fraud probability."""
        alpha, beta = self._posterior(np.asarray(scores, dtype=float))
        tail = (1 - level) / 2
        return beta_dist.ppf(tail, alpha, beta), beta_dist.ppf(1 - tail, alpha, beta)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "prior": {"a": self.prior_a, "b": self.prior_b},
            "penalty": self.penalty,
            "blocks": [asdict(b) for b in self.blocks],
            "unscored": asdict(self.unscored),
        }

    @classmethod
    def from_dict(cls, d: dict) -> BayesianBlocksCalibrator:
        return cls(
            blocks=tuple(Block(**b) for b in d["blocks"]),
            unscored=Block(**d["unscored"]),
            prior_a=d["prior"]["a"],
            prior_b=d["prior"]["b"],
            penalty=d["penalty"],
            name=d["name"],
        )


@dataclass(frozen=True)
class IsotonicCalibrator:
    model: IsotonicRegression
    unscored_rate: float
    name: str = "isotonic"

    @classmethod
    def fit(cls, counts: ScoreCounts) -> IsotonicCalibrator:
        model = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        model.fit(counts.scores, counts.k / counts.n, sample_weight=counts.n)
        return cls(model, counts.unscored_k / max(counts.unscored_n, 1))

    def predict(self, scores: np.ndarray) -> np.ndarray:
        scores = np.asarray(scores, dtype=float)
        p = self.model.predict(np.nan_to_num(scores))
        return np.where(np.isnan(scores), self.unscored_rate, p)


@dataclass(frozen=True)
class HistogramCalibrator:
    """The current gold calibration (baseline_fraud_score_v1): fixed-width bins."""

    p_by_bin: dict[int | None, float]
    bin_width: int
    name: str = "histogram_baseline"

    def predict(self, scores: np.ndarray) -> np.ndarray:
        scores = np.asarray(scores, dtype=float)
        last = 100 // self.bin_width - 1
        bins = np.minimum(np.floor(np.nan_to_num(scores) / self.bin_width), last).astype(int)
        p = np.array([self.p_by_bin[int(b)] for b in bins])
        return np.where(np.isnan(scores), self.p_by_bin[None], p)


@dataclass(frozen=True)
class RawScoreCalibrator:
    """Naive baseline: read the score as a percentage; no score means 0."""

    name: str = "raw_score"

    def predict(self, scores: np.ndarray) -> np.ndarray:
        return np.nan_to_num(np.asarray(scores, dtype=float)) / 100.0
