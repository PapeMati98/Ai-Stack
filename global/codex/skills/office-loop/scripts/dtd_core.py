import math
from collections.abc import Sequence
from typing import Any, Hashable


def rule_of_three(n: int, confidence: float = 0.95) -> float | None:
    if not (0.0 < confidence < 1.0):
        raise ValueError("confidence must be in (0, 1)")
    if n is None or n != n or n <= 0:
        return None
    return -math.log(1.0 - confidence) / n


def saturation(keys: Sequence[Hashable], confidence: float = 0.95) -> dict:
    keys = list(keys)
    first_seen: list[int] = []
    seen: set = set()
    for i, k in enumerate(keys):
        if k not in seen:
            seen.add(k)
            first_seen.append(i)
    unique = len(first_seen)
    if first_seen:
        since_last_new = len(keys) - 1 - first_seen[-1]
    else:
        since_last_new = 0
    return {
        "unique": unique,
        "since_last_new": since_last_new,
        "p_new_upper": rule_of_three(since_last_new, confidence),
        "first_seen": first_seen,
    }


def _beta_params(successes: int, trials: int, prior_a: float, prior_b: float):
    if successes < 0 or trials < 0:
        raise ValueError("successes and trials must be non-negative")
    if successes > trials:
        raise ValueError("successes cannot exceed trials")
    a = prior_a + successes
    b = prior_b + (trials - successes)
    return a, b


def beta_mean(successes: int, trials: int, prior_a: float = 1.0, prior_b: float = 1.0) -> float:
    a, b = _beta_params(successes, trials, prior_a, prior_b)
    return a / (a + b)


def beta_interval(
    successes: int,
    trials: int,
    prior_a: float = 1.0,
    prior_b: float = 1.0,
    z: float = 1.96,
) -> tuple[float, float]:
    a, b = _beta_params(successes, trials, prior_a, prior_b)
    mean = a / (a + b)
    denom = (a + b) ** 2 * (a + b + 1)
    var = (a * b) / denom if denom != 0 else 0.0
    sd = math.sqrt(var) if var >= 0 else 0.0
    lo = mean - z * sd
    hi = mean + z * sd
    lo = max(0.0, min(1.0, lo))
    hi = max(0.0, min(1.0, hi))
    if lo > hi:
        lo, hi = hi, lo
    return (lo, hi)


def value_iteration(transitions, terminal_costs, max_iter: int = 1000, tol: float = 1e-9):
    terminal_set = set(terminal_costs)

    actions_by_state: dict[Any, list[Any]] = {}
    outcomes_by: dict[tuple, list] = {}
    for key, outcomes in transitions.items():
        state, action = key
        actions_by_state.setdefault(state, []).append(action)
        outcomes_by[(state, action)] = list(outcomes)

    non_terminal = set(actions_by_state.keys())

    for (state, action), outcomes in outcomes_by.items():
        probs = [o[0] for o in outcomes]
        for p in probs:
            if not (0.0 <= p <= 1.0):
                raise ValueError("probability out of [0,1]")
        s = sum(probs)
        if abs(s - 1.0) > 1e-6:
            raise ValueError("probabilities must sum to 1")

    seen: set = set()
    stack = list(non_terminal)
    while stack:
        s = stack.pop()
        if s in seen:
            continue
        seen.add(s)
        if s in terminal_set:
            continue
        if s not in non_terminal:
            raise ValueError("non-terminal state without action")
        for action in actions_by_state.get(s, []):
            for (p, ns, step_cost) in outcomes_by[(s, action)]:
                if p == 0:
                    continue
                if ns not in terminal_set and ns not in non_terminal:
                    raise ValueError("non-terminal state without action")
                if ns not in seen:
                    stack.append(ns)

    if not non_terminal:
        return {}, {}

    def can_reach_terminal(s: Any) -> bool:
        visited: set = set()
        work = [s]
        while work:
            cur = work.pop()
            if cur in visited:
                continue
            visited.add(cur)
            if cur in terminal_set:
                return True
            if cur not in non_terminal:
                continue
            for action in actions_by_state.get(cur, []):
                for (p, ns, step_cost) in outcomes_by[(cur, action)]:
                    if p == 0:
                        continue
                    if ns in terminal_set:
                        return True
                    if ns in non_terminal and ns not in visited:
                        work.append(ns)
        return False

    values: dict[Any, float] = {}
    for s in non_terminal:
        values[s] = 0.0 if can_reach_terminal(s) else float("inf")

    def q_value(s, action):
        q = 0.0
        for (p, ns, step_cost) in outcomes_by[(s, action)]:
            if p == 0:
                continue
            if ns in terminal_set:
                v_next = terminal_costs[ns]
            else:
                v_next = values[ns]
            q += p * (step_cost + v_next)
        return q

    converged = False
    for _ in range(max_iter):
        new_values: dict[Any, float] = {}
        delta = 0.0
        nonfinite = False
        for s in non_terminal:
            best_key = None
            best_q = None
            for action in actions_by_state[s]:
                q = q_value(s, action)
                key = (q, str(action))
                if best_key is None or key < best_key:
                    best_key = key
                    best_q = q
            new_values[s] = best_q if best_q is not None else float("inf")
            old = values[s]
            diff = new_values[s] - old
            if not math.isfinite(diff):
                nonfinite = True
            else:
                if abs(diff) > delta:
                    delta = abs(diff)
        values = new_values
        if (not nonfinite) and delta < tol:
            converged = True
            break

    if not converged:
        raise RuntimeError("value iteration did not converge")

    policy: dict[Any, Any] = {}
    for s in non_terminal:
        best_key = None
        best_action = None
        for action in actions_by_state[s]:
            q = q_value(s, action)
            key = (q, str(action))
            if best_key is None or key < best_key:
                best_key = key
                best_action = action
        policy[s] = best_action

    return values, policy
