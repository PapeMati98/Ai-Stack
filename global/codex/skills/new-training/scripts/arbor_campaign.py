#!/usr/bin/env python3
"""Arbor ML Campaign and Experience Ledger Helper.

Provides a deterministic, stdlib-only CPU synthetic regression campaign engine,
phase and lockbox fencing, single-use final evaluation, and append-only
experience learning. Local CPU records are distinct from Agent Graph envelopes;
the explicit export validates caller-authored native AG inputs.

Hard contracts:
- Candidate algorithms are strictly declarative configurations (no arbitrary code execution).
- Selection (development/validation) is strictly separated from final holdout evaluation.
- Evaluating final holdout is single-use and fail-closed: repeated evaluation or overwrite is blocked.
- Non-finite metrics (NaN, Inf) are strictly rejected.
- Experience separates approach failures from infrastructure interruptions.
- Lesson transfer is gated by domain, project, trust level, and transfer conditions.
- Safe output handling: symlinks and caller data mutation are rejected.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import random
import re
import shlex
import sys
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

# Defensive bytecode setting
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
try:
    sys.dont_write_bytecode = True
except Exception:
    pass

VERSION = "1.1.0"
SCHEMA_CAMPAIGN = "arbor-cpu-campaign/v1"
SCHEMA_UNIT_RESULT = "arbor-cpu-unit-result/v1"
SCHEMA_EXPERIENCE = "arbor-experience/v1"
SCHEMA_LOCKED_CANDIDATE = "arbor-locked-candidate/v1"
SCHEMA_FINAL_EVALUATION = "arbor-final-evaluation/v1"
SCHEMA_FROZEN_MANIFEST = "arbor-frozen-manifest/v1"
SCHEMA_FINAL_CLAIM = "arbor-final-claim/v1"

EVALUATOR_SPEC = {
    "name": "arbor_builtin_regression_metrics",
    "version": VERSION,
    "metrics": ["mse", "rmse", "mae", "r2"],
}


# ==============================================================================
# Exceptions
# ==============================================================================

class ArborError(Exception):
    """Base exception for all Arbor campaign operations."""
    pass


class NonFiniteMetricError(ArborError):
    """Raised when an evaluation metric is NaN or Infinite."""
    pass


class NonFinitePredictionError(ArborError):
    """Raised when a model produces NaN or Infinite predictions."""
    pass


class NumericalInstabilityError(ArborError):
    """Raised when an algorithm encounters singular matrices or numerical divergence."""
    pass


class UnsupportedAlgorithmError(ArborError):
    """Raised when an unknown or disallowed algorithm name is requested."""
    pass


class InvalidPhaseError(ArborError):
    """Raised when an operation is executed in an invalid campaign lifecycle phase."""
    pass


class FinalAlreadyEvaluatedError(ArborError):
    """Raised when an already-evaluated final holdout is invoked again."""
    pass


class SecurityError(ArborError):
    """Raised when symlinks or unauthorized path traversal is detected."""
    pass


class BudgetExceededError(ArborError):
    """Raised when a candidate unit exceeds its allowed training budget."""
    pass


class DataIntegrityError(ArborError):
    """Raised when data hashes do not match expected provenance."""
    pass


# ==============================================================================
# Enums and Timestamps
# ==============================================================================

class CampaignPhase(str, Enum):
    INITIALIZED = "initialized"
    DEVELOPMENT = "development"
    SELECTION = "selection"
    FINAL_LOCKED = "final_locked"
    FINAL_EVALUATED = "final_evaluated"


class TrustLevel(str, Enum):
    VERIFIED = "verified"
    TRUSTED = "verified"  # Legacy alias.
    PROVISIONAL = "provisional"
    REJECTED = "rejected"
    UNTRUSTED = "rejected"  # Legacy alias.


class FailureCategory(str, Enum):
    APPROACH = "approach"
    INFRASTRUCTURE = "infrastructure"


def utc_now() -> str:
    """Return an ISO-8601 UTC timestamp ending in Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compute_hash(data: Any) -> str:
    """Compute deterministic SHA-256 of arbitrary serializable data."""
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def normalize_trust(value: Union[str, TrustLevel]) -> TrustLevel:
    """Normalize old lesson aliases to the canonical AG trust vocabulary."""
    raw = value.value if isinstance(value, TrustLevel) else str(value).strip().lower()
    aliases = {"trusted": "verified", "untrusted": "rejected"}
    return TrustLevel(aliases.get(raw, raw))


def current_compatibility() -> Dict[str, str]:
    return {
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "arbor_version": VERSION,
    }


def _iso_date(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


# ==============================================================================
# Security & Filesystem Utilities
# ==============================================================================

def check_no_symlinks(path: Union[str, Path]) -> Path:
    """Verify that path does not traverse or point to any symlinks."""
    p = Path(path)
    if p.is_symlink():
        raise SecurityError(f"Target path is a symlink: {p}")
    # Check parent ancestry up to root
    curr = p.parent
    while curr != curr.parent:
        if curr.is_symlink():
            raise SecurityError(f"Parent directory contains a symlink: {curr}")
        curr = curr.parent
    return p


def safe_write_json(path: Union[str, Path], data: Any) -> None:
    """Safely write JSON to disk, checking symlinks and writing via temporary atomic replace."""
    p = check_no_symlinks(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp_path = p.with_suffix(p.suffix + f".tmp_{os.getpid()}_{int(time.time()*1000)}")
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(temp_path, p)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def write_exclusive_json(path: Union[str, Path], data: Any) -> None:
    """Create durable JSON once. Existing files are never replaced or cleared."""
    p = check_no_symlinks(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    check_no_symlinks(p)
    with open(p, "x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def safe_read_json(path: Union[str, Path]) -> Any:
    """Safely read JSON file without traversing symlinks."""
    p = check_no_symlinks(path)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {p}")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def safe_append_jsonl(path: Union[str, Path], record: Dict[str, Any]) -> None:
    """Append a single JSON line record safely to an append-only file."""
    p = check_no_symlinks(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(serialized + "\n")


def safe_read_jsonl(path: Union[str, Path]) -> List[Dict[str, Any]]:
    """Safely read all JSON lines from a file."""
    p = check_no_symlinks(path)
    if not p.is_file():
        return []
    records = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


# ==============================================================================
# Pure Python Linear Algebra & Math Helpers
# ==============================================================================

def mean(vals: Sequence[float]) -> float:
    if not vals:
        return 0.0
    return sum(vals) / float(len(vals))


def median(vals: Sequence[float]) -> float:
    if not vals:
        return 0.0
    sorted_vals = sorted(vals)
    n = len(sorted_vals)
    mid = n // 2
    if n % 2 == 1:
        return float(sorted_vals[mid])
    return (float(sorted_vals[mid - 1]) + float(sorted_vals[mid])) / 2.0


def solve_linear_system(A: List[List[float]], b: List[float], min_pivot: float = 1e-11) -> List[float]:
    """Solve A x = b using Gauss-Jordan elimination with partial pivoting.
    
    Raises NumericalInstabilityError if matrix is singular or near-singular.
    """
    n = len(A)
    if n == 0 or len(b) != n:
        raise ValueError("Invalid matrix dimensions")
    
    # Augmented matrix
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    
    for i in range(n):
        # Find pivot
        pivot_row = i
        max_val = abs(M[i][i])
        for r in range(i + 1, n):
            if abs(M[r][i]) > max_val:
                max_val = abs(M[r][i])
                pivot_row = r
                
        if max_val < min_pivot:
            raise NumericalInstabilityError(
                f"Matrix is singular or near-singular (max pivot {max_val:.2e} < {min_pivot:.2e})."
            )
            
        if pivot_row != i:
            M[i], M[pivot_row] = M[pivot_row], M[i]
            
        pivot = M[i][i]
        for c in range(i, n + 1):
            M[i][c] /= pivot
            
        for r in range(n):
            if r != i:
                factor = M[r][i]
                if factor != 0.0:
                    for c in range(i, n + 1):
                        M[r][c] -= factor * M[i][c]
                        
    return [M[i][n] for i in range(n)]


# ==============================================================================
# Metric Calculations & Non-Finite Verification
# ==============================================================================

def assert_finite_series(series: Sequence[float], name: str = "series") -> None:
    for idx, val in enumerate(series):
        if not math.isfinite(val):
            raise NonFiniteMetricError(f"Non-finite value encountered in {name} at index {idx}: {val}")


def mse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    if len(y_true) != len(y_pred):
        raise ValueError("Length mismatch between y_true and y_pred")
    if not y_true:
        return 0.0
    assert_finite_series(y_true, "y_true")
    assert_finite_series(y_pred, "y_pred")
    val = sum((yt - yp) ** 2 for yt, yp in zip(y_true, y_pred)) / float(len(y_true))
    if not math.isfinite(val):
        raise NonFiniteMetricError(f"Computed MSE is non-finite: {val}")
    return val


def rmse(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    m = mse(y_true, y_pred)
    val = math.sqrt(m)
    if not math.isfinite(val):
        raise NonFiniteMetricError(f"Computed RMSE is non-finite: {val}")
    return val


def mae(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    if len(y_true) != len(y_pred):
        raise ValueError("Length mismatch between y_true and y_pred")
    if not y_true:
        return 0.0
    assert_finite_series(y_true, "y_true")
    assert_finite_series(y_pred, "y_pred")
    val = sum(abs(yt - yp) for yt, yp in zip(y_true, y_pred)) / float(len(y_true))
    if not math.isfinite(val):
        raise NonFiniteMetricError(f"Computed MAE is non-finite: {val}")
    return val


def r2_score(y_true: Sequence[float], y_pred: Sequence[float]) -> float:
    if len(y_true) != len(y_pred):
        raise ValueError("Length mismatch between y_true and y_pred")
    if not y_true:
        return 0.0
    assert_finite_series(y_true, "y_true")
    assert_finite_series(y_pred, "y_pred")
    y_mean = mean(y_true)
    ss_tot = sum((yt - y_mean) ** 2 for yt in y_true)
    ss_res = sum((yt - yp) ** 2 for yt, yp in zip(y_true, y_pred))
    if ss_tot == 0.0:
        return 1.0 if ss_res == 0.0 else 0.0
    val = 1.0 - (ss_res / ss_tot)
    if not math.isfinite(val):
        raise NonFiniteMetricError(f"Computed R2 is non-finite: {val}")
    return val


def compute_metrics(y_true: Sequence[float], y_pred: Sequence[float], prefix: str = "") -> Dict[str, float]:
    p = f"{prefix}_" if prefix else ""
    return {
        f"{p}mse": mse(y_true, y_pred),
        f"{p}rmse": rmse(y_true, y_pred),
        f"{p}mae": mae(y_true, y_pred),
        f"{p}r2": r2_score(y_true, y_pred),
    }


# ==============================================================================
# Declarative Machine Learning Algorithms (No Arbitrary Code Execution)
# ==============================================================================

class DeclarativeModel:
    """Base class for purely declarative models."""
    def fit(self, X: List[List[float]], y: List[float], budget: Dict[str, Any]) -> int:
        raise NotImplementedError

    def predict(self, X: List[List[float]]) -> List[float]:
        raise NotImplementedError

    def serialize(self) -> Dict[str, Any]:
        raise NotImplementedError


class MeanBaselineModel(DeclarativeModel):
    """Rung 1 baseline: predicts constant mean of training targets."""
    def __init__(self, config: Dict[str, Any]):
        self.config = copy.deepcopy(config)
        self.mean_val: float = 0.0

    def fit(self, X: List[List[float]], y: List[float], budget: Dict[str, Any]) -> int:
        self.mean_val = mean(y)
        return 1

    def predict(self, X: List[List[float]]) -> List[float]:
        return [self.mean_val] * len(X)

    def serialize(self) -> Dict[str, Any]:
        return {"algorithm": "mean_baseline", "mean_val": self.mean_val}


class MedianBaselineModel(DeclarativeModel):
    """Rung 1 baseline alternative: predicts constant median."""
    def __init__(self, config: Dict[str, Any]):
        self.config = copy.deepcopy(config)
        self.median_val: float = 0.0

    def fit(self, X: List[List[float]], y: List[float], budget: Dict[str, Any]) -> int:
        self.median_val = median(y)
        return 1

    def predict(self, X: List[List[float]]) -> List[float]:
        return [self.median_val] * len(X)

    def serialize(self) -> Dict[str, Any]:
        return {"algorithm": "median_baseline", "median_val": self.median_val}


class LinearRegressionModel(DeclarativeModel):
    """Rung 2 Ordinary Least Squares regression via normal equation."""
    def __init__(self, config: Dict[str, Any]):
        self.config = copy.deepcopy(config)
        self.fit_intercept = self.config.get("fit_intercept", True)
        self.weights: List[float] = []

    def fit(self, X: List[List[float]], y: List[float], budget: Dict[str, Any]) -> int:
        n = len(X)
        if n == 0:
            raise ValueError("Empty training data")
        p = len(X[0])
        
        # Build design matrix
        X_mat: List[List[float]] = []
        for row in X:
            r = [1.0] + list(row) if self.fit_intercept else list(row)
            X_mat.append(r)
            
        dim = p + 1 if self.fit_intercept else p
        
        # Compute X^T X and X^T y
        XtX = [[0.0] * dim for _ in range(dim)]
        Xty = [0.0] * dim
        
        for row, y_val in zip(X_mat, y):
            for i in range(dim):
                Xty[i] += row[i] * y_val
                for j in range(dim):
                    XtX[i][j] += row[i] * row[j]
                    
        self.weights = solve_linear_system(XtX, Xty)
        return 1

    def predict(self, X: List[List[float]]) -> List[float]:
        preds = []
        for row in X:
            r = [1.0] + list(row) if self.fit_intercept else list(row)
            y_hat = sum(w * x for w, x in zip(self.weights, r))
            if not math.isfinite(y_hat):
                raise NonFinitePredictionError(f"LinearRegression produced non-finite prediction: {y_hat}")
            preds.append(y_hat)
        return preds

    def serialize(self) -> Dict[str, Any]:
        return {
            "algorithm": "linear_regression",
            "fit_intercept": self.fit_intercept,
            "weights": self.weights,
        }


class RidgeRegressionModel(DeclarativeModel):
    """Rung 2 regularized linear model with L2 regularization."""
    def __init__(self, config: Dict[str, Any]):
        self.config = copy.deepcopy(config)
        self.alpha = float(self.config.get("alpha", 1.0))
        self.fit_intercept = self.config.get("fit_intercept", True)
        self.weights: List[float] = []

    def fit(self, X: List[List[float]], y: List[float], budget: Dict[str, Any]) -> int:
        n = len(X)
        if n == 0:
            raise ValueError("Empty training data")
        p = len(X[0])
        
        X_mat: List[List[float]] = []
        for row in X:
            r = [1.0] + list(row) if self.fit_intercept else list(row)
            X_mat.append(r)
            
        dim = p + 1 if self.fit_intercept else p
        
        XtX = [[0.0] * dim for _ in range(dim)]
        Xty = [0.0] * dim
        
        for row, y_val in zip(X_mat, y):
            for i in range(dim):
                Xty[i] += row[i] * y_val
                for j in range(dim):
                    XtX[i][j] += row[i] * row[j]
                    
        # Apply L2 penalty to weights (leave intercept unpenalized if fit_intercept is True)
        start_idx = 1 if self.fit_intercept else 0
        for i in range(start_idx, dim):
            XtX[i][i] += self.alpha
            
        self.weights = solve_linear_system(XtX, Xty)
        return 1

    def predict(self, X: List[List[float]]) -> List[float]:
        preds = []
        for row in X:
            r = [1.0] + list(row) if self.fit_intercept else list(row)
            y_hat = sum(w * x for w, x in zip(self.weights, r))
            if not math.isfinite(y_hat):
                raise NonFinitePredictionError(f"RidgeRegression produced non-finite prediction: {y_hat}")
            preds.append(y_hat)
        return preds

    def serialize(self) -> Dict[str, Any]:
        return {
            "algorithm": "ridge_regression",
            "alpha": self.alpha,
            "fit_intercept": self.fit_intercept,
            "weights": self.weights,
        }


class DecisionStumpEnsembleModel(DeclarativeModel):
    """Rung 2 non-linear gradient-boosted decision stumps."""
    def __init__(self, config: Dict[str, Any]):
        self.config = copy.deepcopy(config)
        self.n_estimators = int(self.config.get("n_estimators", 15))
        self.learning_rate = float(self.config.get("learning_rate", 0.1))
        self.base_pred: float = 0.0
        self.stumps: List[Dict[str, Any]] = []

    def fit(self, X: List[List[float]], y: List[float], budget: Dict[str, Any]) -> int:
        n = len(X)
        if n == 0:
            raise ValueError("Empty training data")
        p = len(X[0])
        
        max_budget_steps = budget.get("max_steps", self.n_estimators)
        total_steps = min(self.n_estimators, max_budget_steps)
        
        self.base_pred = mean(y)
        current_preds = [self.base_pred] * n
        self.stumps = []
        
        for step in range(total_steps):
            residuals = [y_i - cp for y_i, cp in zip(y, current_preds)]
            best_feat = 0
            best_thresh = 0.0
            best_left_val = 0.0
            best_right_val = 0.0
            best_loss = float("inf")
            
            # Find best feature split
            for feat_idx in range(p):
                feat_vals = sorted(set(X[i][feat_idx] for i in range(n)))
                if len(feat_vals) <= 1:
                    continue
                # Test midpoints
                thresholds = [(feat_vals[k] + feat_vals[k+1]) / 2.0 for k in range(len(feat_vals) - 1)]
                # Subsample thresholds if too many to preserve budget
                if len(thresholds) > 10:
                    step_size = len(thresholds) // 10
                    thresholds = thresholds[::step_size]
                    
                for thresh in thresholds:
                    left_res = [residuals[i] for i in range(n) if X[i][feat_idx] <= thresh]
                    right_res = [residuals[i] for i in range(n) if X[i][feat_idx] > thresh]
                    if not left_res or not right_res:
                        continue
                    left_val = mean(left_res)
                    right_val = mean(right_res)
                    loss = sum((r - left_val)**2 for r in left_res) + sum((r - right_val)**2 for r in right_res)
                    if loss < best_loss:
                        best_loss = loss
                        best_feat = feat_idx
                        best_thresh = thresh
                        best_left_val = left_val
                        best_right_val = right_val
                        
            if math.isinf(best_loss):
                # Cannot split further
                break
                
            stump = {
                "feat_idx": best_feat,
                "threshold": best_thresh,
                "left_val": best_left_val,
                "right_val": best_right_val,
            }
            self.stumps.append(stump)
            
            # Update predictions
            for i in range(n):
                val = best_left_val if X[i][best_feat] <= best_thresh else best_right_val
                current_preds[i] += self.learning_rate * val
                
        return len(self.stumps)

    def predict(self, X: List[List[float]]) -> List[float]:
        preds = [self.base_pred] * len(X)
        for stump in self.stumps:
            feat = stump["feat_idx"]
            thresh = stump["threshold"]
            l_val = stump["left_val"]
            r_val = stump["right_val"]
            for i, row in enumerate(X):
                val = l_val if row[feat] <= thresh else r_val
                preds[i] += self.learning_rate * val
        for p in preds:
            if not math.isfinite(p):
                raise NonFinitePredictionError(f"DecisionStumpEnsemble produced non-finite prediction: {p}")
        return preds

    def serialize(self) -> Dict[str, Any]:
        return {
            "algorithm": "decision_stump_ensemble",
            "base_pred": self.base_pred,
            "stumps": self.stumps,
            "learning_rate": self.learning_rate,
        }


class KNNRegressionModel(DeclarativeModel):
    """Rung 2 instance-based K-Nearest Neighbors regression."""
    def __init__(self, config: Dict[str, Any]):
        self.config = copy.deepcopy(config)
        self.k = int(self.config.get("n_neighbors", 5))
        self.train_X: List[List[float]] = []
        self.train_y: List[float] = []

    def fit(self, X: List[List[float]], y: List[float], budget: Dict[str, Any]) -> int:
        self.train_X = copy.deepcopy(X)
        self.train_y = copy.deepcopy(y)
        return 1

    def predict(self, X: List[List[float]]) -> List[float]:
        preds = []
        for row in X:
            dists = []
            for tr_row, tr_y in zip(self.train_X, self.train_y):
                d = math.sqrt(sum((a - b)**2 for a, b in zip(row, tr_row)))
                dists.append((d, tr_y))
            dists.sort(key=lambda t: t[0])
            neighbors_y = [t[1] for t in dists[:self.k]]
            pred = mean(neighbors_y)
            if not math.isfinite(pred):
                raise NonFinitePredictionError(f"KNNRegression produced non-finite prediction: {pred}")
            preds.append(pred)
        return preds

    def serialize(self) -> Dict[str, Any]:
        return {
            "algorithm": "knn_regression",
            "k": self.k,
            "sample_count": len(self.train_X),
        }


class PolynomialRidgeModel(DeclarativeModel):
    """Rung 2 polynomial expansion followed by Ridge regression."""
    def __init__(self, config: Dict[str, Any]):
        self.config = copy.deepcopy(config)
        self.degree = int(self.config.get("degree", 2))
        self.alpha = float(self.config.get("alpha", 1.0))
        self.ridge = RidgeRegressionModel({"alpha": self.alpha, "fit_intercept": True})

    def _expand(self, X: List[List[float]]) -> List[List[float]]:
        expanded = []
        for row in X:
            new_row = list(row)
            if self.degree >= 2:
                # Add quadratic terms
                for i in range(len(row)):
                    new_row.append(row[i] ** 2)
                    for j in range(i + 1, len(row)):
                        new_row.append(row[i] * row[j])
            expanded.append(new_row)
        return expanded

    def fit(self, X: List[List[float]], y: List[float], budget: Dict[str, Any]) -> int:
        X_poly = self._expand(X)
        return self.ridge.fit(X_poly, y, budget)

    def predict(self, X: List[List[float]]) -> List[float]:
        X_poly = self._expand(X)
        return self.ridge.predict(X_poly)

    def serialize(self) -> Dict[str, Any]:
        return {
            "algorithm": "polynomial_ridge",
            "degree": self.degree,
            "alpha": self.alpha,
            "weights": self.ridge.weights,
        }


SUPPORTED_ALGORITHMS: Dict[str, Any] = {
    "mean_baseline": MeanBaselineModel,
    "median_baseline": MedianBaselineModel,
    "linear_regression": LinearRegressionModel,
    "ridge_regression": RidgeRegressionModel,
    "decision_stump_ensemble": DecisionStumpEnsembleModel,
    "knn_regression": KNNRegressionModel,
    "polynomial_ridge": PolynomialRidgeModel,
}


def build_model(algorithm_config: Dict[str, Any]) -> DeclarativeModel:
    algo_name = algorithm_config.get("algorithm")
    if not algo_name or algo_name not in SUPPORTED_ALGORITHMS:
        raise UnsupportedAlgorithmError(
            f"Algorithm '{algo_name}' is not supported or not allowed. "
            f"Supported algorithms: {sorted(SUPPORTED_ALGORITHMS.keys())}"
        )
    return SUPPORTED_ALGORITHMS[algo_name](algorithm_config)


# ==============================================================================
# Synthetic Dataset Generation
# ==============================================================================

def generate_synthetic_regression_data(
    seed: int = 42,
    n_samples: int = 200,
    n_features: int = 4,
    noise: float = 0.1,
    collinear: bool = False,
    dev_ratio: float = 0.2,
    holdout_ratio: float = 0.2,
) -> Dict[str, Any]:
    """Generate deterministic synthetic tabular regression data with clean splits.
    
    Returns train, dev, and final_holdout splits with independent hashing.
    """
    rng = random.Random(seed)
    
    # Ground truth weights
    weights = [1.5, -2.0, 0.8, -0.5]
    while len(weights) < n_features:
        weights.append(rng.uniform(-1.0, 1.0))
    weights = weights[:n_features]
    
    X_all: List[List[float]] = []
    y_all: List[float] = []
    
    for _ in range(n_samples):
        row = [rng.gauss(0.0, 1.0) for _ in range(n_features)]
        if collinear and n_features >= 2:
            # Create extreme collinearity: feature 1 is nearly identical to feature 0
            row[1] = row[0] + rng.gauss(0.0, 1e-6)
            
        signal = sum(w * x for w, x in zip(weights, row))
        err = rng.gauss(0.0, noise) if noise > 0.0 else 0.0
        target = signal + err
        X_all.append(row)
        y_all.append(target)
        
    n_holdout = int(n_samples * holdout_ratio)
    n_dev = int(n_samples * dev_ratio)
    n_train = n_samples - n_dev - n_holdout
    
    train_data = {"X": X_all[:n_train], "y": y_all[:n_train]}
    dev_data = {"X": X_all[n_train:n_train + n_dev], "y": y_all[n_train:n_train + n_dev]}
    holdout_data = {"X": X_all[n_train + n_dev:], "y": y_all[n_train + n_dev:]}
    
    return {
        "metadata": {
            "seed": seed,
            "n_samples": n_samples,
            "n_features": n_features,
            "noise": noise,
            "collinear": collinear,
            "train_samples": n_train,
            "dev_samples": n_dev,
            "holdout_samples": n_holdout,
        },
        "hashes": {
            "train_hash": compute_hash(train_data),
            "dev_hash": compute_hash(dev_data),
            "holdout_hash": compute_hash(holdout_data),
        },
        "train": train_data,
        "dev": dev_data,
        "final_holdout": holdout_data,
    }


# ==============================================================================
# Arbor Campaign Engine & Lifecycle Fencing
# ==============================================================================

def _campaign_workspace(campaign: Dict[str, Any]) -> Path:
    campaign_id = str(campaign.get("campaign_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", campaign_id):
        raise DataIntegrityError("Campaign ID is not a safe stable identifier.")
    workspace_text = campaign.get("workspace")
    if not isinstance(workspace_text, str) or not workspace_text.strip():
        raise DataIntegrityError("Campaign workspace is missing.")
    workspace = Path(workspace_text)
    return check_no_symlinks(workspace)


def _frozen_manifest_path(campaign: Dict[str, Any]) -> Path:
    return _campaign_workspace(campaign) / f"arbor-{campaign['campaign_id']}-frozen.json"


def _claim_path(campaign: Dict[str, Any]) -> Path:
    return _campaign_workspace(campaign) / f"arbor-{campaign['campaign_id']}-final-claim.json"


def _campaign_config_hash(campaign: Dict[str, Any]) -> str:
    return compute_hash({
        "campaign_id": campaign.get("campaign_id"),
        "goal": campaign.get("goal"),
        "workspace": campaign.get("workspace"),
        "project_slug": campaign.get("project_slug"),
        "domain": campaign.get("domain"),
        "run_metadata": campaign.get("run_metadata", {}),
        "demo_only": campaign.get("demo_only", False),
        "evaluation_mode": campaign.get("evaluation_mode"),
        "units": [
            {"unit_id": unit.get("unit_id"), "algorithm_config": unit.get("algorithm_config")}
            for unit in campaign.get("units", [])
        ],
    })


def _validate_regression_split(data: Dict[str, Any], name: str) -> None:
    if not isinstance(data, dict) or set(data) != {"X", "y"}:
        raise DataIntegrityError(f"{name} must contain exactly X and y.")
    X, y = data["X"], data["y"]
    if not isinstance(X, list) or not isinstance(y, list) or not X or len(X) != len(y):
        raise DataIntegrityError(f"{name} must contain non-empty, equally sized X and y lists.")
    width = None
    for row in X:
        if not isinstance(row, list) or not row:
            raise DataIntegrityError(f"{name} contains an invalid feature row.")
        if width is None:
            width = len(row)
        if len(row) != width:
            raise DataIntegrityError(f"{name} feature rows have inconsistent widths.")
        assert_finite_series(row, f"{name}.X")
    assert_finite_series(y, f"{name}.y")


def _evaluator_hash() -> str:
    return compute_hash(EVALUATOR_SPEC)


def _restore_model(locked_candidate: Dict[str, Any]) -> DeclarativeModel:
    algo_cfg = locked_candidate["algorithm_config"]
    model = build_model(algo_cfg)
    state = locked_candidate["model_state"]
    if isinstance(model, MeanBaselineModel):
        model.mean_val = state["mean_val"]
    elif isinstance(model, MedianBaselineModel):
        model.median_val = state["median_val"]
    elif isinstance(model, LinearRegressionModel):
        model.weights = state["weights"]
    elif isinstance(model, RidgeRegressionModel):
        model.weights = state["weights"]
    elif isinstance(model, DecisionStumpEnsembleModel):
        model.base_pred = state["base_pred"]
        model.stumps = state["stumps"]
        model.learning_rate = state["learning_rate"]
    elif isinstance(model, KNNRegressionModel):
        raise ArborError("KNN direct state restoration requires lazy evaluation")
    elif isinstance(model, PolynomialRidgeModel):
        model.ridge.weights = state["weights"]
    return model

def create_campaign(
    campaign_id: str,
    goal: str,
    workspace: str,
    project_slug: str,
    baseline_config: Dict[str, Any],
    alternative_configs: List[Dict[str, Any]],
    tree_budget: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a local CPU campaign record (not an Agent Graph envelope).
    
    Requires 1 baseline and 2 to 4 alternative candidates.
    """
    if len(alternative_configs) < 2 or len(alternative_configs) > 4:
        raise ValueError(
            f"Campaign requires 2 to 4 alternative candidates; received {len(alternative_configs)}."
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", campaign_id):
        raise ValueError("campaign_id must be a stable path-safe identifier.")
        
    budget = tree_budget or {
        "max_parallel": 1,
        "max_total_units": len(alternative_configs) + 1,
        "max_depth": 1,
        "allowed_pools": ["local-cpu"],
        "allowed_write_globs": ["*.json", "*.jsonl"],
    }
    
    baseline_id = baseline_config.get("name", "baseline-mean")
    units = []
    
    # 1. Baseline Unit
    units.append({
        "unit_id": f"unit-{baseline_id}",
        "campaign_id": campaign_id,
        "parent_job_id": None,
        "hypothesis": "Baseline naive estimator provides primary reference performance.",
        "text": f"Evaluate baseline algorithm '{baseline_config.get('algorithm')}' under declared budget.",
        "relative_scope": ["models/baseline"],
        "inputs": ["train", "dev"],
        "checks": [{"name": "finite_metrics", "status": "required"}],
        "done_when": ["metrics_computed", "reproducible_hash"],
        "maker": "arbor-maker",
        "reviewer": "arbor-reviewer",
        "dedupe_key": compute_hash({"id": baseline_id, "config": baseline_config}),
        "algorithm_config": copy.deepcopy(baseline_config),
    })
    
    # 2. Alternative Candidate Units
    for alt in alternative_configs:
        alt_id = alt.get("name", alt.get("algorithm", "alt"))
        units.append({
            "unit_id": f"unit-{alt_id}",
            "campaign_id": campaign_id,
            "parent_job_id": f"unit-{baseline_id}",
            "hypothesis": alt.get("hypothesis", f"Candidate '{alt_id}' satisfies goal within budget."),
            "text": f"Evaluate candidate algorithm '{alt.get('algorithm')}' under declared budget.",
            "relative_scope": [f"models/{alt_id}"],
            "inputs": ["train", "dev"],
            "checks": [{"name": "finite_metrics", "status": "required"}],
            "done_when": ["metrics_computed", "reproducible_hash"],
            "maker": "arbor-maker",
            "reviewer": "arbor-reviewer",
            "dedupe_key": compute_hash({"id": alt_id, "config": alt}),
            "algorithm_config": copy.deepcopy(alt),
        })
        
    campaign = {
        "schema_version": SCHEMA_CAMPAIGN,
        "record_kind": "local_numeric_campaign",
        "campaign_id": campaign_id,
        "goal": goal,
        "workspace": workspace,
        "project_slug": project_slug,
        "baseline_ref": f"unit-{baseline_id}",
        "lockbox_ref": None,
        "tree_budget": budget,
        "units": units,
        "phase": CampaignPhase.INITIALIZED.value,
        "created_at": utc_now(),
    }
    return campaign


def run_campaign_units(
    campaign: Dict[str, Any],
    train_data: Dict[str, Any],
    dev_data: Dict[str, Any],
    budget: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Execute baseline and candidate units against train and dev splits.
    
    Produces local CPU numeric records, never AG review results.
    Transitions campaign phase to SELECTION.
    Does NOT mutate caller's train_data, dev_data, or campaign dicts.
    """
    phase = campaign.get("phase")
    if phase not in (CampaignPhase.INITIALIZED.value, CampaignPhase.DEVELOPMENT.value):
        raise InvalidPhaseError(f"Cannot run units in campaign phase '{phase}'.")
        
    camp = copy.deepcopy(campaign)
    camp["phase"] = CampaignPhase.DEVELOPMENT.value
    
    _validate_regression_split(train_data, "train")
    _validate_regression_split(dev_data, "selection")
    X_train = [row[:] for row in train_data["X"]]
    y_train = train_data["y"][:]
    X_dev = [row[:] for row in dev_data["X"]]
    y_dev = dev_data["y"][:]
    
    train_hash = compute_hash(train_data)
    dev_hash = compute_hash(dev_data)
    
    unit_results: List[Dict[str, Any]] = []
    
    for unit in camp["units"]:
        unit_id = unit["unit_id"]
        algo_cfg = unit["algorithm_config"]
        model = build_model(algo_cfg)
        
        t0 = time.time()
        steps_used = model.fit(X_train, y_train, budget)
        t_fit = time.time() - t0
        
        preds_train = model.predict(X_train)
        preds_dev = model.predict(X_dev)
        
        train_metrics = compute_metrics(y_train, preds_train, prefix="train")
        dev_metrics = compute_metrics(y_dev, preds_dev, prefix="dev")
        all_metrics = {**train_metrics, **dev_metrics}
        
        model_serialized = model.serialize()
        model_hash = compute_hash(model_serialized)
        
        result = {
            "schema_version": SCHEMA_UNIT_RESULT,
            "record_kind": "local_cpu_measurement",
            "unit_id": unit_id,
            "campaign_id": camp["campaign_id"],
            "run_id": f"run-{hashlib.sha256(f'{unit_id}_{t0}'.encode()).hexdigest()[:12]}",
            "status": "local_cpu_measured",
            "artifact_hashes": {
                "model_hash": model_hash,
                "train_data_hash": train_hash,
                "dev_data_hash": dev_hash,
                "algorithm_config_hash": compute_hash(algo_cfg),
            },
            "evaluator": {**EVALUATOR_SPEC, "hash": _evaluator_hash()},
            "environment": current_compatibility(),
            "usage": {
                "step_count": steps_used,
                "wall_time_sec": round(t_fit, 6),
                "token_count": "unknown",
                "cost_usd": "unknown",
            },
            "finished_at": utc_now(),
            "metrics": all_metrics,
            "model_state": model_serialized,
            "algorithm_config": copy.deepcopy(algo_cfg),
        }
        unit_results.append(result)
        
    camp["phase"] = CampaignPhase.SELECTION.value
    camp["unit_results"] = copy.deepcopy(unit_results)
    return camp, unit_results


def select_best_candidate(
    campaign: Dict[str, Any],
    unit_results: List[Dict[str, Any]],
    primary_metric: str = "dev_rmse",
    mode: str = "min",
) -> Dict[str, Any]:
    """Select the best candidate among evaluated unit results using dev split metric."""
    if not unit_results:
        raise ValueError("Cannot select from empty unit results.")
        
    best_res = None
    best_val = float("inf") if mode == "min" else float("-inf")
    
    for res in unit_results:
        metrics = res.get("metrics", {})
        if primary_metric not in metrics:
            raise KeyError(f"Primary metric '{primary_metric}' missing from unit result: {res['unit_id']}")
        val = metrics[primary_metric]
        if not math.isfinite(val):
            continue
        if (mode == "min" and val < best_val) or (mode == "max" and val > best_val):
            best_val = val
            best_res = res
            
    if best_res is None:
        raise ArborError("No candidate achieved finite valid metrics.")
    return copy.deepcopy(best_res)


def freeze_candidate(
    campaign: Dict[str, Any],
    selected_unit_result: Dict[str, Any],
    train_hash: str,
    dev_hash: str,
    holdout_hash: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Persist a content-bound frozen manifest before final evaluation."""
    phase = campaign.get("phase")
    if phase != CampaignPhase.SELECTION.value:
        raise InvalidPhaseError(f"Cannot freeze candidate in phase '{phase}'; must be in 'selection'.")
    if selected_unit_result.get("campaign_id") != campaign.get("campaign_id"):
        raise DataIntegrityError("Selected result belongs to a different campaign.")
    if selected_unit_result.get("schema_version") != SCHEMA_UNIT_RESULT:
        raise DataIntegrityError("Selected result is not a local CPU measurement record.")

    camp = copy.deepcopy(campaign)
    sel = copy.deepcopy(selected_unit_result)

    unit = next((item for item in camp.get("units", []) if item.get("unit_id") == sel.get("unit_id")), None)
    if unit is None or unit.get("algorithm_config") != sel.get("algorithm_config"):
        raise DataIntegrityError("Selected result configuration does not match the campaign candidate.")
    result_hashes = sel.get("artifact_hashes", {})
    if result_hashes.get("train_data_hash") != train_hash or result_hashes.get("dev_data_hash") != dev_hash:
        raise DataIntegrityError("Train or selection data hash differs from the measured result.")
    model_state = copy.deepcopy(sel["model_state"])
    if compute_hash(model_state) != result_hashes.get("model_hash"):
        raise DataIntegrityError("Selected model state hash differs from its measured artifact hash.")

    candidate_payload = {
        "schema_version": SCHEMA_LOCKED_CANDIDATE,
        "campaign_id": camp["campaign_id"],
        "unit_id": sel["unit_id"],
        "algorithm_config": copy.deepcopy(sel["algorithm_config"]),
        "model_state": model_state,
        "artifact_hashes": {
            "model_hash": result_hashes["model_hash"],
            "algorithm_config_hash": compute_hash(sel["algorithm_config"]),
            "train_hash": train_hash,
            "dev_hash": dev_hash,
            "holdout_hash": holdout_hash,
        },
        "dev_metrics": copy.deepcopy(sel["metrics"]),
    }
    manifest = {
        "schema_version": SCHEMA_FROZEN_MANIFEST,
        "campaign_id": camp["campaign_id"],
        "campaign_config_hash": _campaign_config_hash(camp),
        "unit_results_hash": compute_hash(camp.get("unit_results", [])),
        "selected_result_hash": compute_hash(sel),
        "train_selection_hashes": {"train": train_hash, "selection": dev_hash},
        "algorithm_config_hash": compute_hash(sel["algorithm_config"]),
        "model_state_hash": compute_hash(model_state),
        "model_hash": result_hashes["model_hash"],
        "evaluator": {**EVALUATOR_SPEC, "hash": _evaluator_hash()},
        "expected_final_dataset_hash": holdout_hash,
        "candidate_payload": candidate_payload,
        "frozen_at": utc_now(),
    }
    manifest_hash = compute_hash(manifest)
    locked_candidate = {**copy.deepcopy(candidate_payload), "manifest_hash": manifest_hash}
    manifest_path = _frozen_manifest_path(camp)
    try:
        write_exclusive_json(manifest_path, manifest)
    except FileExistsError as exc:
        raise DataIntegrityError("A frozen manifest already exists for this campaign ID.") from exc

    camp["phase"] = CampaignPhase.FINAL_LOCKED.value
    camp["locked_candidate"] = copy.deepcopy(locked_candidate)
    camp["frozen_manifest_hash"] = manifest_hash
    camp["frozen_manifest_file"] = manifest_path.name
    return camp, locked_candidate


def evaluate_final(
    campaign: Dict[str, Any],
    locked_candidate: Dict[str, Any],
    holdout_data: Dict[str, Any],
    expected_holdout_hash: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Atomically claim and score one final dataset for a frozen campaign.

    The durable exclusive claim is created before model reconstruction or scoring.
    It is never updated, cleared, or removed; failures after the claim remain spent.
    """
    camp = copy.deepcopy(campaign)
    phase = camp.get("phase")
    
    if phase == CampaignPhase.FINAL_EVALUATED.value or "final_evaluation" in camp:
        raise FinalAlreadyEvaluatedError(
            "Final holdout has already been evaluated for this campaign. "
            "Repeated evaluation of the final holdout is strictly forbidden to prevent data snooping."
        )
    if phase != CampaignPhase.FINAL_LOCKED.value:
        raise InvalidPhaseError(
            f"Cannot evaluate final holdout in phase '{phase}'; campaign must be 'final_locked'."
        )

    if "final_evaluation" in camp:
        raise FinalAlreadyEvaluatedError("Campaign object already contains a final result.")

    manifest_path = _frozen_manifest_path(camp)
    manifest = safe_read_json(manifest_path)
    manifest_hash = compute_hash(manifest)
    if manifest_hash != camp.get("frozen_manifest_hash") or manifest_hash != locked_candidate.get("manifest_hash"):
        raise DataIntegrityError("Frozen manifest hash does not match the campaign and candidate.")
    if manifest.get("schema_version") != SCHEMA_FROZEN_MANIFEST:
        raise DataIntegrityError("Frozen manifest schema is invalid.")
    if manifest.get("campaign_id") != camp.get("campaign_id"):
        raise DataIntegrityError("Frozen manifest belongs to a different campaign.")
    if _campaign_config_hash(camp) != manifest.get("campaign_config_hash"):
        raise DataIntegrityError("Campaign candidate configuration changed after freeze.")
    if compute_hash(camp.get("unit_results", [])) != manifest.get("unit_results_hash"):
        raise DataIntegrityError("Train/selection result records changed after freeze.")
    payload = manifest.get("candidate_payload")
    if not isinstance(payload, dict) or compute_hash(payload) != compute_hash({k: v for k, v in locked_candidate.items() if k != "manifest_hash"}):
        raise DataIntegrityError("Locked candidate configuration or model state changed after freeze.")
    embedded = camp.get("locked_candidate")
    if not isinstance(embedded, dict) or compute_hash(embedded) != compute_hash(locked_candidate):
        raise DataIntegrityError("Campaign and supplied locked candidate differ.")
    if manifest.get("algorithm_config_hash") != compute_hash(locked_candidate.get("algorithm_config")):
        raise DataIntegrityError("Frozen algorithm configuration hash mismatch.")
    if manifest.get("model_state_hash") != compute_hash(locked_candidate.get("model_state")):
        raise DataIntegrityError("Frozen model state hash mismatch.")
    if manifest.get("model_hash") != locked_candidate.get("artifact_hashes", {}).get("model_hash"):
        raise DataIntegrityError("Frozen model artifact hash mismatch.")
    if manifest.get("evaluator", {}).get("hash") != _evaluator_hash():
        raise DataIntegrityError("Evaluator version or implementation identity changed after freeze.")

    _validate_regression_split(holdout_data, "final dataset")
    actual_holdout_hash = compute_hash(holdout_data)
    frozen_holdout_hash = manifest.get("expected_final_dataset_hash")
    if frozen_holdout_hash and actual_holdout_hash != frozen_holdout_hash:
        raise DataIntegrityError("Final dataset does not match the dataset hash frozen by the campaign.")
    if expected_holdout_hash and actual_holdout_hash != expected_holdout_hash:
        raise DataIntegrityError(
            f"Holdout data hash mismatch! Expected {expected_holdout_hash}, got {actual_holdout_hash}"
        )

    claim = {
        "schema_version": SCHEMA_FINAL_CLAIM,
        "campaign_id": camp["campaign_id"],
        "manifest_hash": manifest_hash,
        "candidate_hash": compute_hash(payload),
        "evaluator_hash": _evaluator_hash(),
        "final_dataset_hash": actual_holdout_hash,
        "claimed_at": utc_now(),
        "state": "claimed_before_scoring",
    }
    try:
        write_exclusive_json(_claim_path(camp), claim)
    except FileExistsError as exc:
        raise FinalAlreadyEvaluatedError(
            "A durable final-score claim already exists for this campaign; the attempt remains spent."
        ) from exc

    # The claim is durable before any final predictions or metrics are computed.
    model = _restore_model(locked_candidate)
    X_holdout = [row[:] for row in holdout_data["X"]]
    y_holdout = holdout_data["y"][:]
    
    preds_holdout = model.predict(X_holdout)
    final_metrics = compute_metrics(y_holdout, preds_holdout, prefix="final")
    
    # Verification gate
    dev_rmse = locked_candidate["dev_metrics"].get("dev_rmse", float("inf"))
    final_rmse = final_metrics["final_rmse"]
    
    status = "local_cpu_numeric_result"
    degradation_advisory = final_rmse > dev_rmse * 2.0
        
    final_result = {
        "schema_version": SCHEMA_FINAL_EVALUATION,
        "campaign_id": camp["campaign_id"],
        "unit_id": locked_candidate["unit_id"],
        "holdout_data_hash": actual_holdout_hash,
        "manifest_hash": manifest_hash,
        "claim_file": _claim_path(camp).name,
        "evaluator": {**EVALUATOR_SPEC, "hash": _evaluator_hash()},
        "final_metrics": final_metrics,
        "dev_reference_metrics": locked_candidate["dev_metrics"],
        "status": status,
        "degradation_advisory": degradation_advisory,
        "independent_review": "pending",
        "evaluated_at": utc_now(),
    }
    
    camp["phase"] = CampaignPhase.FINAL_EVALUATED.value
    camp["final_evaluation"] = copy.deepcopy(final_result)
    return camp, final_result


# ==============================================================================
# Experience Ledger & Lesson Transfer
# ==============================================================================

class ExperienceStore:
    """Append-only experience ledger with explicit evidence and transfer gates."""
    
    def __init__(self, storage_path: Union[str, Path]):
        self.path = check_no_symlinks(storage_path)

    def log_lesson(
        self,
        campaign_id: str,
        unit_id: str,
        domain: str,
        project: str,
        hypothesis: str,
        outcome_summary: str,
        category: FailureCategory = FailureCategory.APPROACH,
        trust: TrustLevel = TrustLevel.PROVISIONAL,
        transfer_condition: Optional[Dict[str, Any]] = None,
        metrics_delta: Optional[Dict[str, Any]] = None,
        evidence_ref: str = "",
        evidence_date: Optional[str] = None,
        recommendation: Optional[Dict[str, Any]] = None,
        lesson_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a provisional or evidence-backed transferable lesson."""
        normalized_trust = normalize_trust(trust)
        if not isinstance(transfer_condition, (dict, type(None))):
            raise DataIntegrityError("transfer_condition must be a JSON object.")
        if not isinstance(hypothesis, str) or not hypothesis.strip() or not isinstance(outcome_summary, str) or not outcome_summary.strip():
            raise DataIntegrityError("Lessons require non-empty hypothesis and outcome summary.")
        conditions = copy.deepcopy(transfer_condition or {})
        compatibility = current_compatibility()
        if normalized_trust == TrustLevel.VERIFIED:
            if not isinstance(evidence_ref, str) or not evidence_ref.strip() or not evidence_date or not _iso_date(evidence_date):
                raise DataIntegrityError("Verified lessons require a non-empty evidence reference and ISO date.")
            if not conditions:
                raise DataIntegrityError("Verified lessons require explicit transfer conditions.")
            for key, value in compatibility.items():
                if key in conditions and conditions[key] != value:
                    raise DataIntegrityError(f"Verified lesson compatibility does not match this runtime: {key}.")
            conditions.update(compatibility)
        environment = compatibility
        lid = lesson_id or f"les-{hashlib.sha256(f'{campaign_id}_{unit_id}_{utc_now()}'.encode()).hexdigest()[:12]}"
        
        lesson = {
            "schema_version": SCHEMA_EXPERIENCE,
            "lesson_id": lid,
            "campaign_id": campaign_id,
            "unit_id": unit_id,
            "timestamp": utc_now(),
            "domain": domain,
            "project": project,
            "environment": environment,
            "trust": normalized_trust.value,
            "category": category.value if isinstance(category, FailureCategory) else str(category),
            "transfer_condition": conditions,
            "hypothesis": hypothesis,
            "outcome_summary": outcome_summary,
            "metrics_delta": metrics_delta or {},
            "evidence_ref": evidence_ref,
            "evidence_date": evidence_date,
            "recommendation": recommendation or {},
        }
        
        safe_append_jsonl(self.path, lesson)
        return lesson

    def query_lessons(
        self,
        domain: Optional[str] = None,
        project: Optional[str] = None,
        min_trust: TrustLevel = TrustLevel.VERIFIED,
        category: Optional[FailureCategory] = FailureCategory.APPROACH,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Query lessons filtering by domain, project, trust level, category, and transfer conditions.
        
        Strict filters:
        - Infrastructure failures are never returned when category is APPROACH.
        - Rejected / provisional lessons are filtered out by the default verified threshold.
        - All transfer_condition predicates must match against context.
        """
        all_records = safe_read_jsonl(self.path)
        matching = []
        
        target_trust = normalize_trust(min_trust)
        target_cat = category.value if isinstance(category, FailureCategory) and category is not None else (str(category) if category else None)
        trust_rank = {TrustLevel.REJECTED.value: 0, TrustLevel.PROVISIONAL.value: 1, TrustLevel.VERIFIED.value: 2}
        compatibility = current_compatibility()
        if context and any(key in context and context[key] != value for key, value in compatibility.items()):
            return []
        effective_context = {**(context or {}), **compatibility}
        
        for rec in all_records:
            # 1. Category check
            if target_cat and rec.get("category") != target_cat:
                continue
                
            # 2. Trust check
            try:
                rec_trust = normalize_trust(rec.get("trust", TrustLevel.REJECTED.value))
            except ValueError:
                continue
            if rec_trust == TrustLevel.REJECTED or trust_rank[rec_trust.value] < trust_rank[target_trust.value]:
                continue
            if rec_trust == TrustLevel.VERIFIED:
                if not isinstance(rec.get("evidence_ref"), str) or not rec["evidence_ref"].strip() or not _iso_date(rec.get("evidence_date")):
                    continue
                env = rec.get("environment", {})
                if not isinstance(env, dict) or any(env.get(k) != v for k, v in compatibility.items()):
                    continue
                conditions = rec.get("transfer_condition", {})
                if not isinstance(conditions, dict) or any(k not in conditions for k in compatibility):
                    continue
            if rec.get("category") == FailureCategory.INFRASTRUCTURE.value and target_cat == FailureCategory.APPROACH.value:
                continue
                
            # 3. Domain check
            if domain and rec.get("domain") not in (domain, "*"):
                continue
                
            # 4. Project check
            if project and rec.get("project") not in (project, "*"):
                continue
                
            # 5. Transfer condition evaluation against current context
            conds = rec.get("transfer_condition", {})
            if conds:
                condition_met = True
                for k, v in conds.items():
                    if k.endswith("_gt"):
                        ctx_key = k[:-3]
                        if ctx_key not in effective_context or not (effective_context[ctx_key] > v):
                            condition_met = False
                            break
                    elif k.endswith("_gte"):
                        ctx_key = k[:-4]
                        if ctx_key not in effective_context or not (effective_context[ctx_key] >= v):
                            condition_met = False
                            break
                    elif k.endswith("_lt"):
                        ctx_key = k[:-3]
                        if ctx_key not in effective_context or not (effective_context[ctx_key] < v):
                            condition_met = False
                            break
                    elif k.endswith("_lte"):
                        ctx_key = k[:-4]
                        if ctx_key not in effective_context or not (effective_context[ctx_key] <= v):
                            condition_met = False
                            break
                    else:
                        if effective_context.get(k) != v:
                            condition_met = False
                            break
                if not condition_met:
                    continue
                
            matching.append(rec)
            
        return matching


def apply_experience_to_candidates(
    candidate_configs: List[Dict[str, Any]],
    lessons: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Filter candidate configurations based on transferred lessons.
    
    Returns (active_candidates, pruned_candidates_with_reasons).
    """
    disallowed_algos = set()
    prune_reasons: Dict[str, str] = {}
    
    for les in lessons:
        try:
            if normalize_trust(les.get("trust", "rejected")) != TrustLevel.VERIFIED:
                continue
        except ValueError:
            continue
        if not isinstance(les.get("evidence_ref"), str) or not les["evidence_ref"].strip() or not _iso_date(les.get("evidence_date")):
            continue
        conditions = les.get("transfer_condition", {})
        compatibility = current_compatibility()
        environment = les.get("environment", {})
        if not isinstance(conditions, dict) or any(k not in conditions or conditions[k] != v for k, v in compatibility.items()):
            continue
        if not isinstance(environment, dict) or any(environment.get(k) != v for k, v in compatibility.items()):
            continue
        recom = les.get("recommendation", {})
        for algo in recom.get("disallowed_algorithms", []):
            disallowed_algos.add(algo)
            prune_reasons[algo] = f"Pruned by lesson '{les.get('lesson_id')}': {les.get('outcome_summary')}"
            
    active = []
    pruned = []
    
    for cfg in candidate_configs:
        algo = cfg.get("algorithm")
        if algo in disallowed_algos:
            p_entry = copy.deepcopy(cfg)
            p_entry["_prune_reason"] = prune_reasons[algo]
            pruned.append(p_entry)
        else:
            active.append(copy.deepcopy(cfg))
            
    return active, pruned


def validate_ag_campaign_spec(spec: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and return caller-authored native AG campaign input unchanged.

    This helper does not invent native candidates, executable checks, artifacts,
    acceptance, or a worker runtime. It is a schema gate for an explicit export.
    """
    top_fields = {
        "schema_version", "campaign_id", "goal", "workspace", "project_slug",
        "baseline_ref", "lockbox_ref", "tree_budget", "units",
    }
    if not isinstance(spec, dict) or set(spec) != top_fields:
        raise DataIntegrityError("AG export must contain exactly the declared campaign fields.")
    if spec.get("schema_version") != "arbor-campaign/v1":
        raise DataIntegrityError("AG export schema_version must be arbor-campaign/v1.")
    for name in ("campaign_id", "goal", "workspace", "project_slug", "baseline_ref", "lockbox_ref"):
        if not isinstance(spec.get(name), str) or not spec[name].strip():
            raise DataIntegrityError(f"AG export requires non-empty {name}.")
    if not isinstance(spec["units"], list) or not spec["units"]:
        raise DataIntegrityError("AG export requires a non-empty native unit list.")

    budget = spec["tree_budget"]
    budget_fields = {"max_parallel", "max_total_units", "max_depth", "allowed_pools", "allowed_write_globs"}
    if not isinstance(budget, dict) or set(budget) != budget_fields:
        raise DataIntegrityError("AG export tree_budget does not match the declared contract.")
    if not isinstance(budget["max_parallel"], int) or isinstance(budget["max_parallel"], bool) or budget["max_parallel"] < 1:
        raise DataIntegrityError("AG export max_parallel must be a positive integer.")
    if not isinstance(budget["max_total_units"], int) or isinstance(budget["max_total_units"], bool) or budget["max_total_units"] < len(spec["units"]):
        raise DataIntegrityError("AG export max_total_units must cover all supplied units.")
    if budget["max_depth"] != 1:
        raise DataIntegrityError("AG export max_depth must be 1 for this bounded campaign.")
    for name in ("allowed_pools", "allowed_write_globs"):
        values = budget[name]
        if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
            raise DataIntegrityError(f"AG export {name} must be a non-empty explicit list.")
    if any(pool.strip().lower() in {"cpu", "local-cpu", "arbor"} for pool in budget["allowed_pools"]):
        raise DataIntegrityError("Local CPU execution is not an AG worker pool.")
    for pattern in budget["allowed_write_globs"]:
        if pattern.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", pattern) or ".." in pattern.replace("\\", "/").split("/"):
            raise DataIntegrityError("AG write globs must be relative to the campaign workspace.")

    unit_fields = {
        "unit_id", "campaign_id", "parent_job_id", "hypothesis", "text", "scope", "inputs",
        "checks", "done_when", "maker", "reviewer", "dedupe_key",
    }
    ids = set()
    for unit in spec["units"]:
        if not isinstance(unit, dict) or set(unit) != unit_fields:
            raise DataIntegrityError("Each AG unit must contain exactly the declared native unit fields.")
        unit_id = unit["unit_id"]
        if not isinstance(unit_id, str) or not unit_id.strip() or unit_id in ids:
            raise DataIntegrityError("AG unit IDs must be non-empty and unique.")
        ids.add(unit_id)
        if unit["campaign_id"] != spec["campaign_id"]:
            raise DataIntegrityError("AG unit campaign_id must match its campaign.")
        if unit["parent_job_id"] is not None and (not isinstance(unit["parent_job_id"], str) or not unit["parent_job_id"].strip()):
            raise DataIntegrityError("AG parent_job_id must be null or a non-empty ID.")
        for name in ("hypothesis", "text", "dedupe_key"):
            if not isinstance(unit[name], str) or not unit[name].strip():
                raise DataIntegrityError(f"AG unit requires non-empty {name}.")
        for name in ("scope", "inputs"):
            values = unit[name]
            if not isinstance(values, list) or not values:
                raise DataIntegrityError(f"AG unit {name} must contain relative artifact paths.")
            for value in values:
                if not isinstance(value, str) or not value.strip() or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value) or ".." in value.replace("\\", "/").split("/"):
                    raise DataIntegrityError(f"AG unit {name} contains a non-relative path.")
        checks = unit["checks"]
        if not isinstance(checks, list) or not checks:
            raise DataIntegrityError("AG unit checks must be non-empty executable command strings.")
        for command in checks:
            if not isinstance(command, str) or not command.strip():
                raise DataIntegrityError("AG checks must be executable command strings.")
            try:
                words = shlex.split(command, posix=(os.name != "nt"))
            except ValueError as exc:
                raise DataIntegrityError("AG check command cannot be parsed.") from exc
            if not words or words[0].lower() in {"todo", "placeholder", "true", "echo"}:
                raise DataIntegrityError("AG checks must name an executable command, not a placeholder.")
        for name in ("done_when", "maker", "reviewer"):
            values = unit[name]
            if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v.strip() for v in values):
                raise DataIntegrityError(f"AG unit {name} must be a non-empty caller-supplied list.")
        if set(unit["maker"]) & set(unit["reviewer"]):
            raise DataIntegrityError("AG maker and reviewer candidate lists must be independent.")
        if len(set(unit["maker"])) != len(unit["maker"]) or len(set(unit["reviewer"])) != len(unit["reviewer"]):
            raise DataIntegrityError("AG maker/reviewer candidate lists must not contain duplicates.")
    if spec["baseline_ref"] not in ids:
        raise DataIntegrityError("AG baseline_ref must reference a supplied campaign unit.")
    return copy.deepcopy(spec)


def _campaign_output_dir(path: Union[str, Path]) -> Path:
    out = check_no_symlinks(path)
    if out.exists() and not out.is_dir():
        raise DataIntegrityError(f"Output path is not a directory: {out}")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _campaign_output_path(output_dir: Path, campaign_id: str, label: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", campaign_id):
        raise DataIntegrityError("Campaign ID is not a stable path-safe identifier.")
    return output_dir / f"arbor-{campaign_id}-{label}.json"


def _preflight_campaign_outputs(output_dir: Path, campaign_id: str, labels: Sequence[str]) -> None:
    paths = [_campaign_output_path(output_dir, campaign_id, label) for label in labels]
    paths.append(_frozen_manifest_path({"campaign_id": campaign_id, "workspace": str(output_dir)}))
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise DataIntegrityError(f"Campaign output already exists and will not be overwritten: {existing[0]}")


def _default_candidates() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    return (
        {"name": "baseline-mean", "algorithm": "mean_baseline"},
        [
            {"name": "ols-linear", "algorithm": "linear_regression", "fit_intercept": True},
            {"name": "ridge-reg", "algorithm": "ridge_regression", "alpha": 1.0, "fit_intercept": True},
            {"name": "stump-boost", "algorithm": "decision_stump_ensemble", "n_estimators": 15, "learning_rate": 0.1},
        ],
    )


def _prepare_campaign(
    campaign_id: str,
    output_dir: Union[str, Path],
    train_data: Dict[str, Any],
    selection_data: Dict[str, Any],
    project: str,
    domain: str,
    experience_file: Optional[Union[str, Path]] = None,
    context: Optional[Dict[str, Any]] = None,
    run_metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    _validate_regression_split(train_data, "train")
    _validate_regression_split(selection_data, "selection")
    out = _campaign_output_dir(output_dir)
    _preflight_campaign_outputs(out, campaign_id, ["campaign-locked", "candidate", "unit-results", "selection", "evidence"])
    baseline, candidates = _default_candidates()
    transferred: List[Dict[str, Any]] = []
    pruned: List[Dict[str, Any]] = []
    if experience_file:
        if not context:
            raise DataIntegrityError("Lesson transfer requires explicit campaign context.")
        lessons = ExperienceStore(experience_file).query_lessons(domain=domain, project=project, context=context)
        candidates, pruned = apply_experience_to_candidates(candidates, lessons)
        transferred = [lesson["lesson_id"] for lesson in lessons]
        if not 2 <= len(candidates) <= 4:
            raise DataIntegrityError("Context-valid lessons left fewer than two comparable alternatives.")

    campaign = create_campaign(
        campaign_id=campaign_id,
        goal="Bounded local CPU regression comparison; numeric evidence only.",
        workspace=str(out.resolve()),
        project_slug=project,
        baseline_config=baseline,
        alternative_configs=candidates,
    )
    campaign["domain"] = domain
    campaign["lesson_ids_applied"] = transferred
    campaign["pruned_candidates"] = pruned
    budget = {"max_steps": 20, "max_samples": max(len(train_data["X"]), len(selection_data["X"]))}
    campaign["run_metadata"] = {
        **(run_metadata or {}),
        "runtime": current_compatibility(),
        "training_budget": budget,
        "primary_metric": "dev_rmse",
    }
    campaign_dev, unit_results = run_campaign_units(campaign, train_data, selection_data, budget)
    selected = select_best_candidate(campaign_dev, unit_results)
    frozen, locked = freeze_candidate(
        campaign_dev,
        selected,
        compute_hash(train_data),
        compute_hash(selection_data),
    )
    evidence = {
        "campaign_id": campaign_id,
        "train_hash": compute_hash(train_data),
        "selection_hash": compute_hash(selection_data),
        **(run_metadata or {}),
        "runtime": current_compatibility(),
        "training_budget": budget,
        "lesson_ids_applied": transferred,
        "pruned_candidates": pruned,
        "selected_unit_id": selected["unit_id"],
        "selected_algorithm": selected["algorithm_config"]["algorithm"],
        "selected_metric": "dev_rmse",
        "selected_metric_value": selected["metrics"]["dev_rmse"],
        "final_dataset": "not supplied; create fresh dataset after freeze",
    }
    write_exclusive_json(_campaign_output_path(out, campaign_id, "campaign-locked"), frozen)
    write_exclusive_json(_campaign_output_path(out, campaign_id, "candidate"), locked)
    write_exclusive_json(_campaign_output_path(out, campaign_id, "unit-results"), unit_results)
    write_exclusive_json(_campaign_output_path(out, campaign_id, "selection"), selected)
    write_exclusive_json(_campaign_output_path(out, campaign_id, "evidence"), evidence)
    return evidence


def _read_data_file(path: Union[str, Path], name: str) -> Dict[str, Any]:
    data = safe_read_json(path)
    _validate_regression_split(data, name)
    return data


def _assert_external_final_path(dataset_path: Union[str, Path], campaign_workspace: Union[str, Path]) -> None:
    dataset = check_no_symlinks(dataset_path).resolve()
    workspace = check_no_symlinks(campaign_workspace).resolve()
    try:
        dataset.relative_to(workspace)
    except ValueError:
        return
    raise DataIntegrityError("Final dataset must be outside the frozen campaign workspace.")


# ==============================================================================
# CLI Entrypoint
# ==============================================================================

def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local CPU Arbor campaign helper. Local results are not Agent Graph acceptance.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    p_syn = subparsers.add_parser("run-synthetic", help="Run a same-command synthetic DEMO; not a blind pilot.")
    p_syn.add_argument("--campaign-id", default=f"demo-{int(time.time())}")
    p_syn.add_argument("--output-dir", default="./arbor-demo")
    p_syn.add_argument("--noise", type=float, default=0.1)
    p_syn.add_argument("--collinear", action="store_true")
    p_syn.add_argument("--seed", type=int, default=42)

    p_prepare = subparsers.add_parser("prepare", help="Train, select, and freeze using supplied train/selection data only.")
    p_prepare.add_argument("--campaign-id", required=True)
    p_prepare.add_argument("--output-dir", required=True)
    p_prepare.add_argument("--train-data", required=True, help="JSON file containing exactly X and y.")
    p_prepare.add_argument("--selection-data", required=True, help="JSON file containing exactly X and y.")
    p_prepare.add_argument("--domain", default="tabular_regression")
    p_prepare.add_argument("--project", required=True)
    p_prepare.add_argument("--experience-file")
    p_prepare.add_argument("--context-json", help="Explicit context JSON required when transferring lessons.")

    p_synth = subparsers.add_parser("prepare-synthetic", help="Prepare a synthetic train/selection-only CPU campaign.")
    p_synth.add_argument("--campaign-id", required=True)
    p_synth.add_argument("--output-dir", required=True)
    p_synth.add_argument("--seed", type=int, default=42)
    p_synth.add_argument("--noise", type=float, default=0.1)
    p_synth.add_argument("--collinear", action="store_true")
    p_synth.add_argument("--domain", default="tabular_regression")
    p_synth.add_argument("--project", default="arbor_pilot")
    p_synth.add_argument("--experience-file")
    p_synth.add_argument("--context-json")

    p_final = subparsers.add_parser("evaluate-final", help="Consume one external final dataset for a frozen campaign.")
    p_final.add_argument("--campaign-dir", required=True)
    p_final.add_argument("--campaign-id", required=True, help="Frozen campaign ID in the output directory.")
    p_final.add_argument("--dataset", required=True, help="External parent-created JSON file containing exactly X and y.")
    p_final.add_argument("--expected-dataset-hash", help="Optional independently computed SHA-256 canonical JSON hash.")

    p_hash = subparsers.add_parser("hash-dataset", help="Validate and print the canonical hash of an X/y dataset JSON file.")
    p_hash.add_argument("--dataset", required=True)

    p_export = subparsers.add_parser("export-ag", help="Validate and copy a caller-authored native AG campaign envelope.")
    p_export.add_argument("--spec", required=True, help="JSON authored from real AG candidates and executable checks.")
    p_export.add_argument("--output", required=True, help="New output JSON path; existing files are never replaced.")

    p_log = subparsers.add_parser("log-lesson", help="Append a lesson; trust defaults to provisional.")
    p_log.add_argument("--experience-file", required=True, help="Path to experience.jsonl.")
    p_log.add_argument("--campaign-id", required=True, help="Origin campaign ID.")
    p_log.add_argument("--unit-id", required=True, help="Origin unit ID.")
    p_log.add_argument("--domain", default="tabular_regression", help="Target domain.")
    p_log.add_argument("--project", default="arbor_pilot", help="Project slug.")
    p_log.add_argument("--category", choices=["approach", "infrastructure"], default="approach", help="Failure category.")
    p_log.add_argument("--trust", choices=["verified", "provisional", "rejected", "trusted", "untrusted"], default="provisional", help="Trust level; trusted/untrusted are legacy aliases.")
    p_log.add_argument("--hypothesis", required=True, help="Hypothesis statement.")
    p_log.add_argument("--summary", required=True, help="Outcome summary.")
    p_log.add_argument("--disallow-algo", nargs="*", default=[], help="Algorithms disallowed by this lesson.")
    p_log.add_argument("--evidence-ref", default="", help="Dated run/result reference for verified lessons.")
    p_log.add_argument("--evidence-date", help="Evidence date in YYYY-MM-DD form.")
    p_log.add_argument("--transfer-condition-json", help="Explicit JSON context predicates for verified lessons.")

    p_q = subparsers.add_parser("query-lessons", help="Query transferable lessons from experience ledger.")
    p_q.add_argument("--experience-file", required=True, help="Path to experience.jsonl.")
    p_q.add_argument("--domain", default="tabular_regression", help="Domain filter.")
    p_q.add_argument("--project", default="arbor_pilot", help="Project filter.")
    p_q.add_argument("--category", choices=["approach", "infrastructure"], default="approach", help="Category filter.")
    p_q.add_argument("--min-trust", choices=["verified", "provisional", "rejected", "trusted", "untrusted"], default="verified", help="Trust filter.")
    p_q.add_argument("--context-json", help="Explicit campaign context required to meet transfer conditions.")

    return parser


def _json_object_arg(value: Optional[str], name: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if value is None:
        return copy.deepcopy(default or {})
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise DataIntegrityError(f"{name} must be valid JSON.") from exc
    if not isinstance(parsed, dict):
        raise DataIntegrityError(f"{name} must be a JSON object.")
    return parsed


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "run-synthetic":
            out_dir = _campaign_output_dir(args.output_dir)
            _preflight_campaign_outputs(out_dir, args.campaign_id, ["DEMO_ONLY", "demo-unit-results", "demo-candidate", "demo-final-evaluation", "demo-campaign-final"])
            data = generate_synthetic_regression_data(seed=args.seed, noise=args.noise, collinear=args.collinear)
            baseline_cfg, alt_cfgs = _default_candidates()
            campaign = create_campaign(
                args.campaign_id,
                "Same-command synthetic DEMO only; not an external blind holdout pilot.",
                str(out_dir.resolve()), "arbor_pilot", baseline_cfg, alt_cfgs,
            )
            campaign["demo_only"] = True
            campaign["evaluation_mode"] = "same_command_synthetic_demo"
            budget = {"max_steps": 20, "max_samples": 500}
            campaign["run_metadata"] = {
                "seed": args.seed,
                "noise": args.noise,
                "collinear": args.collinear,
                "synthetic": True,
                "runtime": current_compatibility(),
                "training_budget": budget,
            }
            campaign_dev, unit_results = run_campaign_units(campaign, data["train"], data["dev"], budget)
            selected = select_best_candidate(campaign_dev, unit_results)
            campaign_locked, locked = freeze_candidate(
                campaign_dev, selected, data["hashes"]["train_hash"], data["hashes"]["dev_hash"],
                holdout_hash=data["hashes"]["holdout_hash"],
            )
            campaign_final, final_result = evaluate_final(
                campaign_locked, locked, data["final_holdout"], expected_holdout_hash=data["hashes"]["holdout_hash"]
            )
            final_result["demo_only"] = True
            final_result["external_lockbox"] = False
            write_exclusive_json(_campaign_output_path(out_dir, args.campaign_id, "DEMO_ONLY"), {
                "label": "DEMO ONLY",
                "external_lockbox": False,
                "note": "Same-command synthetic generation and final scoring are not blind pilot evidence.",
                "seed": args.seed,
                "noise": args.noise,
                "collinear": args.collinear,
                "data_hashes": data["hashes"],
                "runtime": current_compatibility(),
            })
            write_exclusive_json(_campaign_output_path(out_dir, args.campaign_id, "demo-unit-results"), unit_results)
            write_exclusive_json(_campaign_output_path(out_dir, args.campaign_id, "demo-candidate"), locked)
            write_exclusive_json(_campaign_output_path(out_dir, args.campaign_id, "demo-final-evaluation"), final_result)
            write_exclusive_json(_campaign_output_path(out_dir, args.campaign_id, "demo-campaign-final"), campaign_final)
            print(f"DEMO ONLY: {args.campaign_id}; final RMSE={final_result['final_metrics']['final_rmse']:.4f}; external lockbox=false")
            return 0

        if args.subcommand == "prepare":
            context = _json_object_arg(args.context_json, "context-json") if args.context_json else None
            evidence = _prepare_campaign(
                args.campaign_id, args.output_dir, _read_data_file(args.train_data, "train"),
                _read_data_file(args.selection_data, "selection"), args.project, args.domain,
                args.experience_file, context, {"input_mode": "caller_supplied_train_selection"},
            )
            print(json.dumps(evidence, sort_keys=True))
            return 0

        if args.subcommand == "prepare-synthetic":
            data = generate_synthetic_regression_data(
                seed=args.seed, noise=args.noise, collinear=args.collinear, holdout_ratio=0.0
            )
            default_context = {"noise": args.noise, "collinear": args.collinear}
            context = _json_object_arg(args.context_json, "context-json", default_context) if args.context_json else default_context
            evidence = _prepare_campaign(
                args.campaign_id, args.output_dir, data["train"], data["dev"], args.project, args.domain,
                args.experience_file, context,
                {"seed": args.seed, "noise": args.noise, "collinear": args.collinear, "synthetic": True},
            )
            print(json.dumps(evidence, sort_keys=True))
            return 0

        if args.subcommand == "evaluate-final":
            campaign_dir = check_no_symlinks(args.campaign_dir)
            campaign = safe_read_json(_campaign_output_path(campaign_dir, args.campaign_id, "campaign-locked"))
            locked = safe_read_json(_campaign_output_path(campaign_dir, args.campaign_id, "candidate"))
            _assert_external_final_path(args.dataset, campaign["workspace"])
            final_data = _read_data_file(args.dataset, "final dataset")
            campaign_final, result = evaluate_final(campaign, locked, final_data, args.expected_dataset_hash)
            write_exclusive_json(_campaign_output_path(campaign_dir, args.campaign_id, "final-evaluation"), result)
            write_exclusive_json(_campaign_output_path(campaign_dir, args.campaign_id, "campaign-final"), campaign_final)
            print(json.dumps(result, sort_keys=True))
            return 0

        if args.subcommand == "hash-dataset":
            print(compute_hash(_read_data_file(args.dataset, "dataset")))
            return 0

        if args.subcommand == "export-ag":
            spec = safe_read_json(args.spec)
            validated = validate_ag_campaign_spec(spec)
            write_exclusive_json(args.output, validated)
            print(f"Validated caller-authored AG campaign export: {args.output}; pending independent review.")
            return 0

        if args.subcommand == "log-lesson":
            store = ExperienceStore(args.experience_file)
            recom = {"disallowed_algorithms": args.disallow_algo} if args.disallow_algo else {}
            transfer = _json_object_arg(args.transfer_condition_json, "transfer-condition-json") if args.transfer_condition_json else {}
            lesson = store.log_lesson(
                campaign_id=args.campaign_id, unit_id=args.unit_id, domain=args.domain, project=args.project,
                category=FailureCategory(args.category), trust=normalize_trust(args.trust),
                hypothesis=args.hypothesis, outcome_summary=args.summary, recommendation=recom,
                evidence_ref=args.evidence_ref, evidence_date=args.evidence_date, transfer_condition=transfer,
            )
            print(f"Logged {lesson['trust']} lesson {lesson['lesson_id']} to {args.experience_file}")
            return 0

        if args.subcommand == "query-lessons":
            store = ExperienceStore(args.experience_file)
            context = _json_object_arg(args.context_json, "context-json") if args.context_json else None
            lessons = store.query_lessons(
                domain=args.domain, project=args.project, min_trust=normalize_trust(args.min_trust),
                category=FailureCategory(args.category), context=context,
            )
            print(json.dumps({"count": len(lessons), "lesson_ids": [item["lesson_id"] for item in lessons]}, sort_keys=True))
            return 0
    except (ArborError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
