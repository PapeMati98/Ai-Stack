"""Source metadata must not hide a machine binding behind JSON escaping."""
import json
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from release_workbench.privacy import _scan_text

class EncodedOwnerPaths(unittest.TestCase):
    def rules(self,text):
        return [r['rule'] for r in _scan_text(text,'fixture.json',[])]

    def test_json_escaped_user_root_is_detected(self):
        # Fictional drive/owner: inert synthetic fixture string, never probed on any filesystem.
        value=r'Q:\Users\synthetic-owner-for-test-only\synthetic-runtime'
        self.assertIn('privacy.owner_path',self.rules(value))
        self.assertIn('privacy.owner_path',self.rules(json.dumps({'runtime_root':value})))

    def test_json_escaped_project_root_is_detected(self):
        # Fictional drive: inert synthetic fixture string, never probed; retained folder label is the unchanged detector signature.
        value=r'Z:\AL Lab\Agenci AI\synthetic-only-detector-fixture'
        self.assertIn('privacy.owner_path',self.rules(value))
        self.assertIn('privacy.owner_path',self.rules(json.dumps({'runtime_root':value})))

    def test_existing_placeholder_classification_is_preserved(self):
        for user in ('user','username','example'):
            with self.subTest(user=user):
                value='C:\\Users\\'+user+'\\.agents'
                self.assertNotIn('privacy.owner_path',self.rules(value))
                self.assertNotIn('privacy.owner_path',self.rules(json.dumps({'runtime_root':value})))

if __name__=='__main__':
    unittest.main()
