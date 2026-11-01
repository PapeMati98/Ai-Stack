def _redacted_fixture(kind, ordinal, binary=False):
    """Return a deterministic public synthetic fixture value.

    The result is newly authored public data derived only from the pinned
    rule label and a per-file ordinal. It is never derived from any original
    value. Runtime values retain the original detector category.
    """
    alphabet = 'abcdefghijklmnopqrstuvwxyz0123456789'
    seed = (len(kind) * 131 + int(ordinal) * 7919 + 17) & 0x7FFFFFFF
    chars = []
    for _ in range(40):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        chars.append(alphabet[seed % 36])
    body = ''.join(chars)
    if kind == 'vendor-token':
        body = 'gh' + 'p_' + body
    elif kind == 'aws-access-key-id':
        body = 'AK' + 'IA' + body[:16].upper()
    elif kind == 'private-key':
        marker = 'PRIVATE' + ' KEY'
        body = ('-----' + 'BEGIN ' + marker + '-----' + chr(10) + body + chr(10)
                + '-----' + 'END ' + marker + '-----')
    if binary:
        return body.encode('ascii')
    return body


import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import scheduled_tick


TASK_TMP = Path(__file__).resolve().parents[1] / ".test-tmp"


class ScheduledTickTests(unittest.TestCase):
 def _control(self, root: Path) -> Path:
  state=root/'control-state'
  path=root/'control.json'
  path.write_text(json.dumps({'state_dir':str(state)}),encoding='utf-8')
  return path

 def _invoke(self, control: Path):
  output=io.StringIO()
  with redirect_stdout(output):
   code=scheduled_tick.main(['--control',str(control)])
  return code,json.loads(output.getvalue())

 def test_sets_task_temp_and_control_env_for_tick_then_restores_environment(self):
  TASK_TMP.mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   root=Path(directory);control=self._control(root);observed=[]
   saved={key:os.environ.get(key) for key in (
    'TEMP','TMP','PYTHONDONTWRITEBYTECODE','HUB_CARETAKER_ALLOW_EXEC')}
   with patch.dict(os.environ, {
    'TEMP':'original-temp','TMP':'original-tmp',
    'PYTHONDONTWRITEBYTECODE':'0','HUB_CARETAKER_ALLOW_EXEC':'0'},
    clear=False):
    def fake_tick(path):
     observed.append({key:os.environ.get(key) for key in saved})
     return {'status':'OK','new_events':0}
    with patch('scheduled_tick.maintenance.tick',side_effect=fake_tick):
     code,result=self._invoke(control)
    self.assertEqual(code,0)
    self.assertEqual(result['status'],'OK')
    self.assertEqual(len(observed),1)
    task_temp=Path(observed[0]['TEMP'])
    self.assertEqual(task_temp,Path(observed[0]['TMP']))
    self.assertTrue(task_temp.is_relative_to(root/'control-state'/'task-temp'))
    self.assertEqual(observed[0]['PYTHONDONTWRITEBYTECODE'],'1')
    self.assertEqual(observed[0]['HUB_CARETAKER_ALLOW_EXEC'],'1')
   for key,value in saved.items():
    self.assertEqual(os.environ.get(key),value)

 def test_degraded_or_nested_blocked_report_returns_one(self):
  TASK_TMP.mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   control=self._control(Path(directory))
   report={'status':'OK','projects':[{
    'pipeline':{'status':'BLOCKED_REQUIRES_DIAGNOSIS'},
    'publication':{'status':'PIPELINE_BLOCKED'},
   }]}
   with patch('scheduled_tick.maintenance.tick',return_value=report):
    code,result=self._invoke(control)
   self.assertEqual(code,1)
   self.assertEqual(result,report)

 def test_exception_writes_redacted_fatal_latest_and_returns_one(self):
  TASK_TMP.mkdir(parents=True,exist_ok=True)
  with tempfile.TemporaryDirectory(dir=str(TASK_TMP)) as directory:
   root=Path(directory);control=self._control(root)
   secret=('Authorization: Bearer ' + _redacted_fixture('bearer-token', 1))
   with patch('scheduled_tick.maintenance.tick',
              side_effect=RuntimeError(secret)):
    code,result=self._invoke(control)
   self.assertEqual(code,1)
   self.assertEqual(result['status'],'BLOCKED')
   self.assertEqual(result['error_type'],'RuntimeError')
   latest=json.loads((root/'control-state'/'latest.json').read_text(
    encoding='utf-8'))
   self.assertEqual(latest,result)
   encoded=(json.dumps(result)+json.dumps(latest)).lower()
   self.assertNotIn('synthetic-secret',encoded)
   self.assertNotIn(secret.lower(),encoded)


if __name__=='__main__':
 unittest.main()
