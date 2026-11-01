import json,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from caretaker.sync import sync_hermes,OWNER_HUB,REMOTE_SCRIPT
from caretaker.publication import PublicationError

class SyncTests(unittest.TestCase):
    def test_wrong_root_and_owner_failure_do_not_contact_remote(self):
        with patch('caretaker.sync.subprocess.run') as run:
            with self.assertRaises(PublicationError):sync_hermes(Path('unrelated'))
            run.assert_not_called()
            run.return_value=SimpleNamespace(returncode=1,stdout=b'')
            with self.assertRaisesRegex(PublicationError,'owner_sync_blocked'):sync_hermes(OWNER_HUB/'vault')
            self.assertEqual(run.call_count,1)
    def test_fixed_remote_and_verified_ack(self):
        with patch('caretaker.sync.subprocess.run',side_effect=[SimpleNamespace(returncode=0,stdout=b''),SimpleNamespace(returncode=0,stdout=json.dumps({'status':'SYNCED','head':'abc'}).encode())]) as run:
            self.assertEqual(sync_hermes(OWNER_HUB/'vault')['status'],'SYNCED')
            self.assertEqual(run.call_args.args[0][-3:],['lab-server','python3','-'])
            self.assertIn("'--diff-filter=D'",REMOTE_SCRIPT)
            self.assertIn("'--ff-only',target",REMOTE_SCRIPT)
