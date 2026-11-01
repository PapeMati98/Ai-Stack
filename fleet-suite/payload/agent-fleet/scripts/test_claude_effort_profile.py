"""Native CLI argv must use documented effort values, including Sonnet."""
import json
from pathlib import Path

import fleet


def test_all_active_claude_profiles_generate_supported_native_effort(tmp_path):
    profile_path = Path(__file__).resolve().parents[1] / 'references/profiles.json'
    profile = json.loads(profile_path.read_text(encoding='utf-8'))['profiles']['claude']
    supported = {'low', 'medium', 'high', 'xhigh', 'max'}
    for name, execution in profile['execution_profiles'].items():
        if execution['lifecycle'] != 'ACTIVE':
            continue
        command, _, _ = fleet.build_command(
            'claude', 'synthetic-claude.exe', prompt='synthetic', workspace=tmp_path,
            mode='review', model=execution['model'], complexity='low', timeout=30,
            run_dir=tmp_path, native_subagent_limit=0, enable_web=False,
            materialize=False, reasoning_level=execution['reasoning_level'],
            profile=profile,
        )
        effort = command[command.index('--effort') + 1]
        assert effort in supported, (name, effort)
        assert command[command.index('--model') + 1] == execution['model']
    sonnet = profile['execution_profiles']['claude-sonnet-5-balanced']
    assert sonnet['model'] == 'sonnet'
    assert sonnet['reasoning_level'] == 'medium'
