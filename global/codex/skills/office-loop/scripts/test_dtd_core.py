import math

import pytest

from dtd_core import (
    rule_of_three,
    saturation,
    beta_mean,
    beta_interval,
    value_iteration,
)


def test_rule_of_three_basic():
    assert rule_of_three(150) == pytest.approx(0.01997, rel=1e-3)


def test_rule_of_three_none():
    assert rule_of_three(0) is None
    assert rule_of_three(-5) is None


def test_rule_of_three_confidence_invalid():
    with pytest.raises(ValueError):
        rule_of_three(10, 0.0)
    with pytest.raises(ValueError):
        rule_of_three(10, 1.0)
    with pytest.raises(ValueError):
        rule_of_three(10, -0.1)
    with pytest.raises(ValueError):
        rule_of_three(10, 1.5)


def test_saturation_short():
    keys = ["a", "b", "a", "c", "b"]
    res = saturation(keys)
    assert res["unique"] == 3
    assert res["first_seen"] == [0, 1, 3]
    assert res["since_last_new"] == 1
    assert res["p_new_upper"] == rule_of_three(1)


def test_saturation_all_new():
    res = saturation(["a", "b", "c"])
    assert res["unique"] == 3
    assert res["first_seen"] == [0, 1, 2]
    assert res["since_last_new"] == 0
    assert res["p_new_upper"] is None


def test_saturation_empty():
    res = saturation([])
    assert res["unique"] == 0
    assert res["first_seen"] == []
    assert res["since_last_new"] == 0
    assert res["p_new_upper"] is None


def test_beta_mean():
    assert beta_mean(1, 1) == pytest.approx(2.0 / 3.0)
    assert beta_mean(0, 0) == pytest.approx(0.5)
    assert beta_mean(3, 10) == pytest.approx(4.0 / 12.0)


def test_beta_interval():
    lo, hi = beta_interval(3, 10)
    assert 0 <= lo <= hi <= 1
    m = beta_mean(3, 10)
    assert lo < m < hi


def test_beta_validation():
    with pytest.raises(ValueError):
        beta_mean(5, 3)
    with pytest.raises(ValueError):
        beta_mean(-1, 3)
    with pytest.raises(ValueError):
        beta_mean(1, -1)
    with pytest.raises(ValueError):
        beta_interval(5, 3)


def test_value_iteration_small_mdp():
    transitions = {
        (0, "a"): [(1.0, "T", 1.0)],
    }
    terminal_costs = {"T": 0.0}
    values, policy = value_iteration(transitions, terminal_costs)
    assert values[0] == pytest.approx(1.0)
    assert policy[0] == "a"


def test_value_iteration_tie():
    transitions = {
        (0, "a"): [(1.0, "T", 1.0)],
        (0, "b"): [(1.0, "T", 1.0)],
    }
    terminal_costs = {"T": 0.0}
    values, policy = value_iteration(transitions, terminal_costs)
    assert values[0] == pytest.approx(1.0)
    assert policy[0] == "a"


def test_value_iteration_nonconvergence():
    transitions = {
        (0, "a"): [(1.0, 1, 0.0)],
        (1, "a"): [(1.0, 0, 0.0)],
    }
    terminal_costs = {}
    with pytest.raises(RuntimeError):
        value_iteration(transitions, terminal_costs, max_iter=10)


def test_value_iteration_invalid_probs():
    terminal_costs = {"T": 0.0}
    transitions = {
        (0, "a"): [(0.5, "T", 1.0)],
    }
    with pytest.raises(ValueError):
        value_iteration(transitions, terminal_costs)
    transitions2 = {
        (0, "a"): [(1.5, "T", 1.0)],
    }
    with pytest.raises(ValueError):
        value_iteration(transitions2, terminal_costs)
    transitions3 = {
        (0, "a"): [(-0.1, "T", 1.0), (1.1, "T", 1.0)],
    }
    with pytest.raises(ValueError):
        value_iteration(transitions3, terminal_costs)


def test_value_iteration_missing_action():
    transitions = {
        (0, "a"): [(1.0, 1, 0.0)],
    }
    terminal_costs = {}
    with pytest.raises(ValueError):
        value_iteration(transitions, terminal_costs)


def test_hand_computed_mdp():
    transitions = {
        (0, "go"): [(1.0, "T", 5.0)],
        (1, "go"): [(0.5, "T", 0.0), (0.5, 1, 2.0)],
    }
    terminal_costs = {"T": 0.0}
    values, policy = value_iteration(transitions, terminal_costs)
    assert values[0] == pytest.approx(5.0)
    assert values[1] == pytest.approx(2.0)
    assert policy[0] == "go"
    assert policy[1] == "go"


def test_value_iteration_zero_prob_edge():
    transitions = {
        (0, "a"): [(0.0, 1, 99.0), (1.0, "T", 1.0)],
    }
    terminal_costs = {"T": 0.0}
    values, policy = value_iteration(transitions, terminal_costs)
    assert values[0] == pytest.approx(1.0)
    assert policy[0] == "a"
