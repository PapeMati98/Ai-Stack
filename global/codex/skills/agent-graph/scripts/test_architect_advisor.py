import copy
import unittest
from unittest.mock import patch
import architect_advisor as advisor


class SnapshotTests(unittest.TestCase):
    def snapshot(self, entries):
        profiles = {'cli': {'models': {'high': 'worker'}, 'execution_profiles': entries}}
        before = copy.deepcopy(profiles)
        def build(fleet, expanded, quotas, readiness, task, capacity):
            self.assertEqual(task['mode'], 'advisory')
            self.assertEqual(task['required_tools'], ['filesystem-read'])
            return {'candidates': [{'agent': 'cli', 'model': name}
                                   for name in expanded['cli']['models'].values()]}
        with patch.object(advisor, 'make_snapshot', side_effect=build):
            value = advisor.architect_snapshot(None, profiles, {}, {}, capacity=2)
        self.assertEqual(profiles, before)
        return {c['model']: c for c in value['candidates']}

    def test_explicit_only_architect_is_included_without_name_inference(self):
        value = self.snapshot({'a': {'model': 'explicit-architect', 'model_class': 'mythos', 'coding_rank': 1},
                               'b': {'model': 'worker', 'model_class': 'opus', 'coding_rank': 2}})
        self.assertEqual(value['explicit-architect']['model_class'], 'mythos')
        self.assertEqual(value['worker']['coding_rank'], 2)

    def test_missing_class_stays_unknown_even_for_famous_model_name(self):
        value = self.snapshot({'a': {'model': 'gpt-6-astra'}})
        self.assertEqual(value['gpt-6-astra']['model_class'], 'unknown')

    def test_inconsistent_profiles_cannot_borrow_stronger_classification(self):
        value = self.snapshot({'a': {'model': 'same', 'model_class': 'mythos', 'coding_rank': 1},
                               'b': {'model': 'same', 'model_class': 'opus', 'coding_rank': 2}})
        self.assertEqual(value['same']['model_class'], 'unknown')
        self.assertIsNone(value['same']['coding_rank'])

    def test_matching_profiles_keep_explicit_classification(self):
        value = self.snapshot({'a': {'model': 'same', 'model_class': 'mythos', 'coding_rank': 1},
                               'b': {'model': 'same', 'model_class': 'mythos', 'coding_rank': 1}})
        self.assertEqual(value['same']['model_class'], 'mythos')

    def test_boolean_rank_does_not_become_best_rank(self):
        value = self.snapshot({'a': {'model': 'worker', 'model_class': 'opus', 'coding_rank': True}})
        self.assertIsNone(value['worker']['coding_rank'])


if __name__ == '__main__':
    unittest.main()
