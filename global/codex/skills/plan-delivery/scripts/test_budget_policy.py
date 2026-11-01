import copy
import unittest

import budget_pools as b
import plan_contract as p


class BudgetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.ceiling = b.example_owner_ceiling(planning_and_review=10, execution=20)
        self.ledger = b.empty_ledger()

    def test_legacy_hard_cannot_borrow(self):
        legacy_ceiling = copy.deepcopy(self.ceiling)
        del legacy_ceiling['token_policy']
        with self.assertRaises(b.BudgetPoolError):
            b.authorize_pool_spend(legacy_ceiling, self.ledger, b.POOL_PLANNING, 11)

    def test_estimate_warns_without_asking_owner(self):
        self.ceiling['token_policy'] = 'estimate'
        original = copy.deepcopy(self.ceiling)
        result = b.authorize_pool_spend(self.ceiling, self.ledger, b.POOL_PLANNING, 31)
        self.assertEqual(result['status'], 'AUTHORIZED')
        self.assertTrue(result['estimate_warning'])
        self.assertFalse(result['owner_approval_required'])
        self.assertFalse(result['borrowed_from_other_pool'])
        self.assertEqual(result['additional_estimate_tokens'], 21)
        self.assertEqual(self.ceiling, original)

    def test_unknown_telemetry_is_not_zero_or_cash(self):
        result = b.charge_token_usage(total_tokens=None, token_status='unknown', max_tokens=10)
        self.assertIsNone(result['attested_tokens'])
        self.assertFalse(result['estimated'])
        self.assertEqual(result['status'], 'unknown')
        self.assertNotIn('cost_usd', result)

    def test_invalid_and_mismatched_policy_rejected(self):
        self.ceiling['token_policy'] = 'unlimited'
        with self.assertRaises(b.BudgetPoolError):
            b.validate_owner_ceiling(self.ceiling)
        self.ceiling['token_policy'] = 'hard'
        with self.assertRaises(p.ContractError):
            p.validate_owner_ceiling_field(self.ceiling, {'max_total_tokens':20, 'token_policy':'estimate'})

    def test_new_clock_and_policies_validate(self):
        for policy in ('hard','estimate'):
            for clock in ('elapsed','active'):
                p.validate_budget(dict(max_total_tokens=20,max_wall_seconds=60,max_amendments=0,max_total_attempts=2,token_policy=policy,time_policy=policy,clock=clock), 'budget', True)

    def test_default_owner_token_policy_is_estimate(self):
        ceiling = b.example_owner_ceiling(planning_and_review=10, execution=20)
        self.assertEqual(ceiling['token_policy'], 'estimate')
        p.validate_budget(
            dict(max_total_tokens=20, max_wall_seconds=60, max_amendments=0, max_total_attempts=2),
            'budget',
            True,
        )
        p.validate_budget(
            dict(max_attempts=1, max_wall_seconds=30, max_tokens=None, max_tool_calls=1),
            'node',
        )

if __name__ == '__main__':
    unittest.main()
