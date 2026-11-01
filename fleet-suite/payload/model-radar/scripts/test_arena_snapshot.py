from __future__ import annotations

import unittest

import arena_snapshot


class ArenaSnapshotTests(unittest.TestCase):
    def mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "source_dataset": "lmarena-ai/leaderboard-dataset",
            "mappings": [
                {
                    "profile": "grok",
                    "model": "grok-4.5",
                    "arena_ids": ["grok-4.5"],
                    "confidence": "verified",
                },
                {
                    "profile": "kimi",
                    "model": "k3",
                    "arena_ids": ["kimi-k3-max"],
                    "confidence": "candidate",
                },
            ],
        }

    def test_build_snapshot_matches_exact_alias_and_keeps_missing_explicit(self) -> None:
        rows = [
            {
                "model_name": "grok-4.5",
                "rating": 1200.0,
                "rating_lower": 1180.0,
                "rating_upper": 1220.0,
                "vote_count": 500,
                "rank": 1,
                "category": "Overall",
                "leaderboard_publish_date": "2026-07-20",
            },
            {
                "model_name": "other-model",
                "rating": 1100.0,
                "rank": 2,
                "category": "Overall",
            },
        ]
        snapshot = arena_snapshot.build_snapshot(
            {"text_style_control": rows},
            self.mapping(),
            generated_at="2026-07-21T12:00:00Z",
        )
        metric = snapshot["models"]["grok@grok-4.5"]["benchmarks"][
            "text_style_control"
        ]
        self.assertEqual(metric["status"], "matched")
        self.assertEqual(metric["percentile"], 100.0)
        missing = snapshot["models"]["kimi@k3"]["benchmarks"][
            "text_style_control"
        ]
        self.assertEqual(missing["status"], "missing")

    def test_map_rejects_duplicate_profile_model(self) -> None:
        mapping = self.mapping()
        mapping["mappings"].append(dict(mapping["mappings"][0]))
        with self.assertRaisesRegex(arena_snapshot.SnapshotError, "duplicate"):
            arena_snapshot.validate_map(mapping)


if __name__ == "__main__":
    unittest.main(verbosity=2)
