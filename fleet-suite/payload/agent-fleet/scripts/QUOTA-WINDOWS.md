# Quota windows

Pure parsing used by existing Kimi /usage and Codex metadata collectors. Duration, not positional primary/secondary labels, selects scope. Multiple spent windows require the latest reset; an unknown reset promises no recovery. Elapsed counters require recheck. Code-review and resource-family windows remain independent.

Optional offline MiniMax normalization (no credentials/network/cache writes):
`python quota_windows.py --provider minimax --account-or-pool <pool> --observed-at <zoned ISO date of sample> --input <redacted JSON>`

The actual `model_remains` array is supported: `general` coding and `video` are distinct; 0..100 remaining percentages work with zero token denominators. HTTP200 base_resp errors and empty samples are unknown. Explicit existing identity fields must match the supplied binding. A supplied binding identifies an offline sample; it does not prove which live account produced it. Live MiniMax retrieval is not implemented.

Source inspiration: AIUsage commit 7935fe0feac7bb8a30b4ba86f52e1e15f3f4cf90. Python code authored locally; no foreign executable code or proxy installed.
