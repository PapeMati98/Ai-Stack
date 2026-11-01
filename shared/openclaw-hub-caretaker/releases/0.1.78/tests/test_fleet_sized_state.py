import json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from caretaker.fleet_publication_runner import sized_state_reader


class SizedStateReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'run.json'
        self.path.write_text(json.dumps({'completed_at':'2026-10-11T22:00:00Z','quota':{'remaining':None}}))
    def test_small_file_passes_exact_size_encoding_path_to_canonical_reader(self):
        seen={}
        def original(path,**kw):seen.update(kw);seen['path']=path;return {'verified':'by-original'},None
        result=sized_state_reader(original)(self.path,max_bytes=67108864,encoding='ascii')
        self.assertEqual(result,({'verified':'by-original'},None))
        self.assertEqual(seen,{'path':self.path,'max_bytes':self.path.stat().st_size,'encoding':'ascii'})
    def test_policy_limit_never_raised(self):
        def original(path,**kw):return kw['max_bytes'],None
        self.assertEqual(sized_state_reader(original)(self.path,max_bytes=8),(8,None))
    def test_absent_and_oserror_keep_original_diagnostic(self):
        for error in (FileNotFoundError(),PermissionError()):
            with self.subTest(error=type(error).__name__),patch.object(Path,'lstat',side_effect=error):
                original=lambda path,**kw:('canonical-diagnostic',kw['max_bytes'])
                self.assertEqual(sized_state_reader(original)(self.path,max_bytes=64),('canonical-diagnostic',64))
    def test_growth_is_refused_by_canonical_limit_check(self):
        def original(path,*,max_bytes,encoding):
            path.write_text(path.read_text()+' ')
            return (None,'corrupt-cache-ignored') if path.stat().st_size>max_bytes else (json.loads(path.read_text()),None)
        self.assertEqual(sized_state_reader(original)(self.path,max_bytes=67108864),(None,'corrupt-cache-ignored'))
    def test_original_refusal_for_nonregular_and_link_is_retained(self):
        for result in ((None,'corrupt-cache-ignored'),(None,'missing')):
            with self.subTest(result=result):
                self.assertEqual(sized_state_reader(lambda *a,**k:result)(self.path,max_bytes=67108864),result)


if __name__=='__main__':unittest.main()
