"""Installed client uses only a pinned config; explicit test-home stays local."""
import hashlib,json,unittest
from pathlib import Path
from unittest.mock import patch
from office_support import OfficeTestCase
from coordfs.office_cli import default_remote_config,main
from coordfs.errors import CoordError

class DefaultRemoteTests(OfficeTestCase):
    def test_source_without_installed_remote_can_remain_local(self):
        self.assertIsNone(default_remote_config(Path(self.tmp.name)))
    def test_pinned_profile_selects_adjacent_config(self):
        root=Path(self.tmp.name);config=root/'office-remote.json';config.write_bytes(b'{"test":"remote"}')
        (root/'.agent-office-runtime.json').write_text(json.dumps({'remote_config_sha256':hashlib.sha256(config.read_bytes()).hexdigest()}))
        self.assertEqual(default_remote_config(root),config)
        config.write_bytes(b'changed')
        with self.assertRaises(CoordError) as raised:default_remote_config(root)
        self.assertEqual(raised.exception.code,'invalid_remote_config')
    def test_missing_config_or_loose_file_cannot_fall_back(self):
        root=Path(self.tmp.name);(root/'.agent-office-runtime.json').write_text(json.dumps({'remote_config_sha256':'a'*64}))
        with self.assertRaises(CoordError):default_remote_config(root)
        other=root/'unmanaged';other.mkdir();(other/'office-remote.json').write_text('{}')
        with self.assertRaises(CoordError):default_remote_config(other)
    def test_local_only_installed_marker_without_remote_config(self):
        root=Path(self.tmp.name)
        (root/'.agent-office-runtime.json').write_text(json.dumps({'schema_version':1,'tool':'runtime'}))
        self.assertIsNone(default_remote_config(root))
        (root/'office-remote.json').write_text('{}')
        with self.assertRaises(CoordError) as raised:default_remote_config(root)
        self.assertEqual(raised.exception.code,'invalid_remote_config')
    def test_cli_selects_default_only_without_explicit_home(self):
        config=Path(self.tmp.name)/'office-remote.json'
        with patch('coordfs.office_cli.default_remote_config',return_value=config) as choose,patch('coordfs.office_cli.dispatch',return_value=None) as dispatch:
            self.assertEqual(main(['--harness','codex','--session-id','test','channels']),0)
            self.assertEqual(dispatch.call_args[0][0].remote_config,config);choose.assert_called_once()
        for flag in ['--home','--runtime-home']:
            with patch('coordfs.office_cli.default_remote_config') as choose,patch('coordfs.office_cli.dispatch',return_value=None):
                main([flag,str(self.home),'channels']);choose.assert_not_called()

if __name__=='__main__':unittest.main()
