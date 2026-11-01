"""Actual caller regressions from independent Opus reception; synthetic only."""
import datetime as dt, tempfile, unittest
from pathlib import Path
from unittest import mock
import quota_windows as q, quota_availability as a, kimi_usage as k, fleet
UTC=dt.timezone.utc

class LiveFindings(unittest.TestCase):
 def test_failed_refresh_is_not_fresh(self):
  old={'status':'fresh','observed_at':'2026-10-05T10:00:00Z','headroom':0.5}
  now=dt.datetime(2026,10,6,12,tzinfo=UTC)
  v=q.last_good_normalization(old,{'reason':'server'},observed_at=now)
  self.assertEqual(v['status'],'stale');self.assertEqual(v['observed_at'],old['observed_at'])
  self.assertEqual(v['last_good'],old);self.assertEqual(v['last_attempt_at'],'2026-10-06T12:00:00Z')

 def test_live_failed_check_preserves_prior_time_and_sample(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);stamp=a.stamp(1700000000)
   old={'schema_version':1,'status':'available','source':'kimi-native-usage','observed_at':stamp,'recheck_at':stamp,'windows':[{'scope':'weekly','used':25,'limit':100}]}
   path=a.cache_path(root,'synthetic');path.parent.mkdir(parents=True);a.adaptive.atomic_write(path,old)
   def failed():raise ValueError('synthetic-read-failure')
   v=a.check(root,'synthetic',{},probe=failed,lock_factory=fleet.ReleasableKernelLock,now=1700001200)
   self.assertEqual(v['status'],'unknown');self.assertEqual(v['observed_at'],stamp)
   self.assertEqual(v['last_good']['windows'],old['windows']);self.assertEqual(v['last_good']['observed_at'],stamp)
   self.assertNotEqual(v['last_attempt_at'],stamp);self.assertEqual(v['attempt_error']['reason'],'ValueError')
   self.assertEqual(a.read_cache(path)['last_good']['observed_at'],stamp)

 def usage(self,unknown=False):
  now=dt.datetime.now(UTC);short=(now+dt.timedelta(hours=1)).isoformat().replace('+00:00','Z');weekly=(now+dt.timedelta(days=4)).isoformat().replace('+00:00','Z')
  return {'status':'exhausted','source':'kimi-native-usage','scope':'weekly','reset_at':None if unknown else weekly,'no_promised_recovery':unknown,'covered_scopes':['short-window','weekly'],
   'windows':[{'scope':'short-window','used':100,'limit':100,'reset_at':short},{'scope':'weekly','used':100,'limit':100,'reset_at':None if unknown else weekly}]}

 def test_native_success_retains_latest_of_multiple_blocking_resets(self):
  row=self.usage()
  with mock.patch.object(k,'native_probe',return_value={'status':'available','source':'native-cli-check'}),mock.patch.object(k,'query',return_value=row):v=k.native_fallback(None,None,None)
  self.assertEqual(v['status'],'exhausted');self.assertEqual(v['reset_at'],row['reset_at']);self.assertEqual(v['scope'],'weekly')

 def test_native_success_cannot_replace_unknown_blocking_reset(self):
  with mock.patch.object(k,'native_probe',return_value={'status':'available','source':'native-cli-check'}),mock.patch.object(k,'query',return_value=self.usage(True)):v=k.native_fallback(None,None,None)
  self.assertIsNone(v['reset_at']);self.assertTrue(v['no_promised_recovery'])

 def test_confirmed_native_refusal_keeps_aggregate_unknown_reset(self):
  row=self.usage(True)
  native={'status':'exhausted','scope':'short-window','source':'native-cli-check','reset_at':row['windows'][0]['reset_at']}
  with mock.patch.object(k,'native_probe',return_value=native),mock.patch.object(k,'query',return_value=row):v=k.native_fallback(None,None,None)
  self.assertIsNone(v['reset_at']);self.assertTrue(v['no_promised_recovery'])

 def test_flat_percent_fields_use_zero_to_hundred(self):
  for percent,fraction in [(0,0),(1,0.01),(50,0.5),(100,1),(101,None),(True,None)]:
   with self.subTest(percent=percent):
    v=q.parse_minimax_payload({'interval_remaining_percent':percent},provider='minimax',account_or_pool='synthetic',observed_at=dt.datetime.now(UTC))
    self.assertEqual(v['interval']['remaining_fraction'],fraction)
