"""Offline Windows regression: guarded prompt bytes must equal the native file."""
import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import fleet
import zcode_runtime as z

class PromptBytesTests(unittest.TestCase):
    def materialize(self, root, prompt):
        profile=json.loads(fleet.DEFAULT_PROFILES.read_text(encoding='utf-8'))['profiles']['zcode']
        with patch.object(z,'validate_zcode_profile'), patch.object(z,'build_command',return_value=['inert-native']), patch.object(z,'prepare_run'):
            fleet.build_command('zcode','inert-native',prompt=prompt,workspace=root,mode='advisory',model='GLM-5.3',complexity='low',timeout=30,run_dir=root,native_subagent_limit=0,enable_web=False,profile=profile,max_model_tool_calls=4)
        return (root/'zcode-prompt.txt').read_bytes()

    def test_final_exact_cap_multiline_unicode_bytes_are_unchanged(self):
        suffix=z.scoped_prompt('',{'files':['Zażółć/🧠-'+str(i)+'.txt' for i in range(12)]})
        prefix='ż😀\n'*200
        padding=49152-len((prefix+suffix).encode('utf-8'))
        prompt=prefix+'a'*padding+suffix
        self.assertEqual(len(prompt.encode('utf-8')),49152)
        with tempfile.TemporaryDirectory(prefix='zcode-prompt-byte-') as directory:
            actual=self.materialize(Path(directory),prompt)
        self.assertEqual(actual,prompt.encode('utf-8'))
        self.assertEqual(len(actual),49152)

    def test_explicit_crlf_is_not_retranslated(self):
        prompt='Zażółć😀\r\nSecond\nThird\rEnd'
        with tempfile.TemporaryDirectory(prefix='zcode-prompt-byte-') as directory:
            self.assertEqual(self.materialize(Path(directory),prompt),prompt.encode('utf-8'))

    def test_existing_prompt_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='zcode-prompt-byte-') as directory:
            root=Path(directory);path=root/'zcode-prompt.txt';path.write_bytes(b'preserved')
            with self.assertRaises(FileExistsError):self.materialize(root,'new\n')
            self.assertEqual(path.read_bytes(),b'preserved')

if __name__=='__main__':unittest.main()
