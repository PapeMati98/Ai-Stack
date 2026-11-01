import json
import tomllib
import unittest
import yaml
import client_config as fmt


class ConfigTests(unittest.TestCase):
    def setUp(self):
        fmt.server=lambda spec:{'command':'C:/Python/pythonw.exe','args':['-I','C:/pilot/adc_entry.py']}
    def test_json_preserves_other_bytes_and_is_idempotent(self):
        original=b'{ "other": {"leave":"unchanged"}, "mcpServers": {"old":{"command":"old"}} }\n'
        spec={'format':'json','key':'mcpServers'}
        patched=fmt.patch(spec,original)
        self.assertIn(b'"other": {"leave":"unchanged"}',patched)
        before=json.loads(original);after=json.loads(patched);after['mcpServers'].pop(fmt.NAME)
        self.assertEqual(before,after)
        self.assertEqual(fmt.patch(spec,patched),patched)
    def test_toml_and_yaml_preserve_other_entries(self):
        for form,raw,key,parse in [('toml',b'# Keep\n[x]\nvalue=1\n[mcp_servers.old]\ncommand="old"\n','mcp_servers',tomllib.loads),
                                   ('yaml',b'# Keep\nother: true\nmcp_servers:\n  old: {command: old}\n','mcp_servers',yaml.safe_load)]:
            before=parse(raw.decode());patched=fmt.patch({'format':form,'key':key},raw)
            self.assertIn(b'# Keep',patched)
            after=parse(patched.decode());after[key].pop(fmt.NAME)
            self.assertEqual(before,after)
    def test_conflicting_entry_is_not_overwritten(self):
        original=b'{"mcpServers":{"gcp-readonly":{"command":"other"}}}'
        with self.assertRaises(ValueError):fmt.patch({'format':'json','key':'mcpServers'},original)


if __name__=='__main__':unittest.main()
