"""Native Windows transport regressions, with no provider/network execution."""
import os
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.environ.get('FLEET_TEST_ROOT', str(Path(__file__).parent)))
import fleet


class NativeMuseTests(unittest.TestCase):
    def test_muse_readiness_requires_affirmative_sandbox_evidence(self):
        profile=fleet.load_profiles(fleet.DEFAULT_PROFILES)['muse']
        probe={'launchable':True,'exit_code':0,'output':'Muse Code 1.4.9'}
        result=fleet.routing_v2.readiness_from_probes(profile,executable='native.exe',version_probe=probe,deep_probe={'launchable':True,'output':'unrecognized output'})
        self.assertFalse(result['ready'])
        self.assertIn('required-runtime-probe-confirmation-missing',result['reasons'])
        confirmation='\n'.join(profile['readiness']['required_deep_probe_lines'])
        result=fleet.routing_v2.readiness_from_probes(profile,executable='native.exe',version_probe=probe,deep_probe={'launchable':True,'output':confirmation})
        self.assertTrue(result['ready'])
        old_probe=dict(probe,output='Muse Code 1.4.6')
        outdated=fleet.routing_v2.readiness_from_probes(profile,executable='native.exe',version_probe=old_probe,deep_probe={'launchable':True,'output':confirmation})
        self.assertFalse(outdated['ready'])

    def command(self, executable, workspace, run_dir, mode='review'):
        return fleet.build_command(
            'muse', str(executable), prompt='Literal $() & | < > % text; no shell expansion.',
            workspace=workspace, mode=mode, model='muse-spark-1.3', complexity='medium',
            timeout=30, run_dir=run_dir, native_subagent_limit=0, enable_web=False,
            materialize=True, reasoning_level='medium',
        )[0]

    def test_native_paths_and_literal_prompt_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); workspace=root/'workspace with spaces'; workspace.mkdir()
            exe=root/'muse-bin-1.0.1-R123.exe'; exe.touch()
            command=self.command(exe, workspace, root/'run')
            self.assertEqual(command[:2], [str(exe), 'exec'])
            self.assertEqual(command[command.index('--workspace')+1], str(workspace.resolve()))
            self.assertEqual(command[command.index('--prompt-file')+1], str((root/'run/muse-prompt.txt').resolve()))
            self.assertEqual((root/'run/muse-prompt.txt').read_text(), 'Literal $() & | < > % text; no shell expansion.')
            self.assertNotIn('-d', command)
            self.assertNotIn('/root/.local/bin/muse', command)
            self.assertIn('--disable-write', command)
            self.assertIn('--disable-shell', command)
            self.assertIn('--disable-web-tools', command)
            self.assertEqual(command[command.index('--approval-mode')+1], 'never')

    def test_native_gate_rejects_shells_and_outside_install_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); install=root/'Programs/muse'; install.mkdir(parents=True)
            native=install/'muse-bin-1.0.1-R123.exe';native.touch()
            with patch.dict(os.environ, {'LOCALAPPDATA':str(root)}), patch.object(fleet, 'MUSE_NATIVE_SHA256', hashlib.sha256(b'').hexdigest()):
                fleet.require_muse_native_executable(str(native))
                for path in [install/'muse.cmd',install/'wsl.exe',root/native.name]:
                    path.touch()
                    with self.assertRaises(fleet.FleetError):
                        fleet.require_muse_native_executable(str(path))
                native.write_bytes(b'tampered')
                with self.assertRaises(fleet.FleetError):
                    fleet.require_muse_native_executable(str(native))

    def test_endpoint_and_update_overrides_do_not_reach_child(self):
        env={'MUSE_AUTH_URL':'https://invalid.example','MUSE_AUTH_PATH':'other-auth.json',
             'MUSE_LAUNCHER_URL':'https://invalid.example/install.ps1',
             'MUSE_CHANNEL_URL':'https://invalid.example','MUSE_DOWNLOAD_HOST':'invalid.example',
             'MUSE_NO_AUTO_UPDATE':'0','MUSE_LOGIN':'1','DO_NOT_TRACK':'0','PATH':'test'}
        actual=fleet.harden_muse_environment(env)
        self.assertNotIn('MUSE_AUTH_URL',actual)
        self.assertNotIn('MUSE_AUTH_PATH',actual)
        self.assertNotIn('MUSE_LAUNCHER_URL',actual)
        self.assertNotIn('MUSE_CHANNEL_URL',actual)
        self.assertNotIn('MUSE_DOWNLOAD_HOST',actual)
        self.assertEqual(actual['MUSE_NO_AUTO_UPDATE'],'1')
        self.assertEqual(actual['MUSE_LOGIN'],'0')
        self.assertEqual(actual['DO_NOT_TRACK'],'1')
        self.assertEqual(env['MUSE_LOGIN'],'1')

    def test_contributor_and_wrong_model_still_rejected(self):
        for model in ['muse-spark-1.3-contributor','Muse Spark Contributor','muse-spark-1.2']:
            with self.assertRaises(fleet.FleetError):fleet.require_safe_muse_model(model)


if __name__=='__main__':unittest.main()
