"""Retired stores refuse access before locks, diagnostics or cursor writes."""
import hashlib
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from office_support import OfficeTestCase
from coordfs.atomic import exclusive_create, replace_with_backoff, unlink_with_backoff
from coordfs.errors import CoordError
from coordfs.events import append_event, read_events, rotate_if_needed, _move_to_archive
from coordfs.leases import CoordBoard
from coordfs.store import ensure_home

def tree(home):
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else 'dir'
            for p in home.rglob('*')}

class FrozenStoreTests(OfficeTestCase):
    def freeze(self, marker):
        self.home.mkdir(parents=True,exist_ok=True)
        path=self.home/marker;path.parent.mkdir(exist_ok=True);path.write_text('retired')
        return tree(self.home)

    def test_office_all_boundaries_refuse_without_any_write(self):
        for marker in ['FROZEN','office/FROZEN']:
            with self.subTest(marker=marker):
                before=self.freeze(marker)
                calls=[lambda:self.alice.send(None,'test',channel='test'),lambda:self.alice.presence(ttl_s=900),
                       lambda:self.alice.read(),lambda:self.alice.who(),lambda:self.alice.channels(),
                       lambda:self.alice.ack('a'*32),lambda:self.alice.project_resolve('a')]
                for call in calls:
                    result=call();self.assertEqual((result['status'],result['code']),('error','store_frozen'))
                    self.assertEqual(before,tree(self.home))

    def test_coord_and_events_and_atomic_cannot_mutate(self):
        before=self.freeze('office/FROZEN')
        existing=self.home/'retained';existing.write_text('data');before=tree(self.home)
        for call in [lambda:ensure_home(self.home),lambda:CoordBoard(home=self.home),
                     lambda:append_event(self.home,'test'),lambda:read_events(self.home),
                     lambda:exclusive_create(self.home/'new.lock',b'lock'),
                     lambda:replace_with_backoff(existing,self.home/'renamed'),lambda:unlink_with_backoff(existing)]:
            with self.assertRaises(CoordError) as raised:call()
            self.assertEqual(raised.exception.code,'store_frozen');self.assertEqual(before,tree(self.home))

    def test_unfrozen_store_still_works(self):
        self.assertEqual(self.alice.presence()['status'],'ok')
        self.assertEqual(self.alice.send(None,'hello',channel='test')['status'],'ok')
        self.assertEqual(self.bob.read(channel='test')['status'],'ok')
        board=CoordBoard(home=self.home)
        self.assertEqual(board.claim('synthetic-resource','alice')['status'],'ok')
        self.assertEqual(board.release('synthetic-resource','alice')['status'],'ok')
        append_event(self.home,'synthetic-event')
        self.assertIn('synthetic-event',[event['type'] for event in read_events(self.home)])

    def test_root_marker_rejects_low_level_mutations(self):
        target=self.home/'retained';target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('retained')
        before=self.freeze('FROZEN')
        for call in [lambda:ensure_home(self.home),lambda:CoordBoard(home=self.home),
                     lambda:append_event(self.home,'test'),
                     lambda:exclusive_create(self.home/'new.lock',b'lock'),
                     lambda:replace_with_backoff(target,self.home/'renamed'),
                     lambda:unlink_with_backoff(target)]:
            with self.assertRaises(CoordError) as raised:call()
            self.assertEqual(raised.exception.code,'store_frozen')
            self.assertEqual(tree(self.home),before)

    def test_symlink_entry_inside_frozen_store_cannot_be_unlinked(self):
        self.home.mkdir(parents=True,exist_ok=True)
        target=Path(self.tmp.name)/'outside';target.write_text('retained')
        link=self.home/'link'
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f'symlinks unavailable: {exc}')
        before=self.freeze('FROZEN')
        with self.assertRaises(CoordError) as raised:unlink_with_backoff(link)
        self.assertEqual(raised.exception.code,'store_frozen')
        self.assertEqual(tree(self.home),before)
        self.assertEqual(target.read_text(),'retained')

    def test_direct_event_rotation_and_archive_refuse_before_link(self):
        target=self.home/'events.jsonl';target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text('history');target.with_name('events.jsonl.1').write_text('older')
        before=self.freeze('office/FROZEN')
        for call in [lambda:rotate_if_needed(target,10,1,1),
                     lambda:_move_to_archive(target,self.home/'archived-events.jsonl')]:
            with self.assertRaises(CoordError) as raised:call()
            self.assertEqual(raised.exception.code,'store_frozen');self.assertEqual(tree(self.home),before)

    def test_cached_board_cannot_read_or_sweep_after_freeze(self):
        board=CoordBoard(home=self.home)
        before=self.freeze('office/FROZEN')
        for call in [board.ls,board.sweep,lambda:board.who('test')]:
            with self.assertRaises(CoordError) as raised:call()
            self.assertEqual(raised.exception.code,'store_frozen');self.assertEqual(tree(self.home),before)

    def test_offline_snapshot_restore_retains_frozen_data(self):
        import office_store_snapshot as snapshots
        before=self.freeze('office/FROZEN')
        out=Path(self.tmp.name)/'snapshot';dest=Path(self.tmp.name)/'restored'
        snapshots.snapshot(self.home,out);snapshots.verify(out);snapshots.restore(out,dest)
        self.assertEqual(tree(dest),before)
        self.assertEqual(self.office('copy',home=dest).read()['code'],'store_frozen')

    def test_real_cli_refuses_frozen_read_and_send_without_side_effect(self):
        before=self.freeze('office/FROZEN')
        entry=Path(__file__).resolve().parents[1]/'office.py'
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        for command in [['read'],['send','--channel','synthetic','--body','synthetic']]:
            result=subprocess.run([sys.executable,'-I','-B',str(entry),'--home',str(self.home),
                                   '--harness','codex','--session-id','frozen-cli-test',*command],
                                  capture_output=True,text=True,env=env,timeout=10,
                                  **({'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}))
            self.assertNotEqual(result.returncode,0)
            self.assertEqual(json.loads(result.stdout)['code'],'store_frozen')
            self.assertEqual(tree(self.home),before)

if __name__=='__main__':unittest.main()
