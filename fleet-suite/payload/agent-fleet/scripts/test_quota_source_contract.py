"""Parent acceptance cases derived from pinned public response schemas."""
import datetime as dt, importlib.util, unittest
from pathlib import Path
import quota_windows as mod
NOW=dt.datetime(2026,10,6,10,tzinfo=dt.timezone.utc)
class SourceContract(unittest.TestCase):
 def test_minimax_actual_array_percent_without_token_denominator(self):
  row={'base_resp':{'status_code':0},'model_remains':[{'model_name':'general','current_interval_total_count':0,'current_interval_usage_count':0,'current_interval_remaining_percent':99,'current_weekly_total_count':0,'current_weekly_usage_count':0,'current_weekly_remaining_percent':100,'end_time':int((NOW+dt.timedelta(hours=5)).timestamp()*1000),'weekly_end_time':int((NOW+dt.timedelta(days=5)).timestamp()*1000)},{'model_name':'video','current_interval_remaining_percent':0}]}
  v=mod.parse_minimax_payload(row,provider='minimax',account_or_pool='synthetic-minimax-plan',observed_at=NOW)
  self.assertEqual(v['interval']['remaining_fraction'],0.99)
  self.assertEqual(v['weekly']['remaining_fraction'],1.0)
  self.assertTrue(v['has_fraction_evidence'])
 def test_minimax_empty_is_unknown(self):
  v=mod.parse_minimax_payload({},provider='minimax',account_or_pool='synthetic-minimax-plan',observed_at=NOW)
  self.assertEqual(v['status'],'unknown')
 def test_unknown_blocking_reset_cannot_promise_known_short_reset(self):
  payload={'usages':{'limit_5h':{'used_ratio':1,'reset_time':'2026-10-06T15:00:00Z'},'limit_7d':{'used_ratio':1}}}
  v=mod.parse_kimi_payload(payload,now=NOW)
  self.assertEqual(v['status'],'exhausted')
  self.assertIsNone(v['reset_at'])
  self.assertTrue(v['no_promised_recovery'])
class ReviewFindings(unittest.TestCase):
 def test_minimax_seconds_and_milliseconds_same_reset(self):
  for scale in (1,1000):
   end=NOW+dt.timedelta(hours=5)
   v=mod.parse_minimax_payload({'base_resp':{'status_code':0},'model_remains':[{'model_name':'general','current_interval_remaining_percent':99,'end_time':int(end.timestamp()*scale)}]},provider='minimax',account_or_pool='pool',observed_at=NOW)
   self.assertEqual(v['interval']['reset_at'],'2026-10-06T15:00:00Z')
 def test_elapsed_kimi_short_and_positive_weekly_require_recheck(self):
  v=mod.parse_kimi_payload({'usages':{'limit_5h':{'used_ratio':1,'reset_time':'2026-10-06T09:55:00Z'},'limit_7d':{'used_ratio':0.2,'reset_time':'2026-10-08T10:00:00Z'}}},now=NOW)
  self.assertEqual(v['status'],'unknown');self.assertTrue(v['no_promised_recovery']);self.assertIsNone(v['reset_at'])
 def test_last_good_and_attempt_have_distinct_dates(self):
  good={'observed_at':'2026-10-05T12:00:00Z','status':'fresh'}
  v=mod.last_good_normalization(good,{'reason':'server-error'},observed_at=NOW)
  self.assertEqual(v['observed_at'],good['observed_at']);self.assertEqual(v['last_attempt_at'],'2026-10-06T10:00:00Z')
 def test_identity_and_explicit_date(self):
  for payload,provider,observed in [({'account_or_pool':'another'},'minimax',NOW),({},'codex',NOW),({},'minimax',NOW.replace(tzinfo=None)),({},'minimax',None)]:
   with self.assertRaises(mod.QuotaWindowError):mod.parse_minimax_payload(payload,provider=provider,account_or_pool='pool',observed_at=observed)
 def test_empty_and_duplicate_general_are_unknown(self):
  for rows in ([],[{'model_name':'video','current_interval_remaining_percent':100}],[{'model_name':'general','current_interval_remaining_percent':0}]*2):
   v=mod.parse_minimax_payload({'model_remains':rows},provider='minimax',account_or_pool='pool',observed_at=NOW)
   self.assertEqual(v['status'],'unknown')
 def test_actual_codex_consumer_keeps_zero_without_reset(self):
  import quota_collectors
  event={'timestamp':'2026-10-06T10:00:00Z','type':'event_msg','payload':{'type':'token_count','rate_limits':{'primary':{'used_percent':100,'window_minutes':300},'secondary':{'used_percent':20,'window_minutes':10080,'resets_in_seconds':172800}}}}
  sample=quota_collectors._codex_rate_limit_sample(event)
  self.assertEqual(sample[2]['short'][0],0);self.assertIsNone(sample[2]['short'][1])
  self.assertEqual(sample[2]['weekly'][1],NOW+dt.timedelta(days=2))
 def test_relative_reset_is_anchored_to_sample(self):
  self.assertEqual(mod.parse_reset('in 5 hours',observed_at=NOW)[0],NOW+dt.timedelta(hours=5))
 def test_offline_cli_rejects_missing_date_and_identity_mismatch(self):
  import argparse,contextlib,io,json,tempfile
  directory=Path(tempfile.mkdtemp(prefix='quota-source-cli-'));path=directory/'sample.json'
  path.write_text(json.dumps({'account_or_pool':'another','model_remains':[]}),encoding='utf-8')
  for observed in (None,'2026-10-06T10:00:00Z'):
   with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
    rc=mod._cli_normalize(argparse.Namespace(provider='minimax',account_or_pool='pool',observed_at=observed,input=str(path)))
   self.assertEqual(rc,2)
if __name__=='__main__':unittest.main()

class NumericEdgeContract(unittest.TestCase):
    def test_huge_numeric_scalar_is_unknown_not_exception(self):
        for fn in (mod.sanitize_fraction,mod.sanitize_percent,mod.sanitize_non_negative,mod.sanitize_positive):
            with self.subTest(fn=fn.__name__):
                self.assertIsNone(fn(10**400,label="unreadable-provider-scalar"))

    def test_invalid_present_short_window_cannot_be_hidden_by_valid_weekly(self):
        for value in (1.2,True,-0.1,"bad",None):
            with self.subTest(value=value),self.assertRaises(mod.QuotaWindowError):
                mod.parse_kimi_payload({"usages":{"limit_5h":{"used_ratio":value},"limit_7d":{"used_ratio":0.2}}})

    def test_malformed_present_ratio_window_shape_is_not_available(self):
        for short in ({},None,False,"garbage"):
            with self.subTest(short=short),self.assertRaises(mod.QuotaWindowError):
                mod.parse_kimi_payload({"usages":{"limit_5h":short,"limit_7d":{"used_ratio":0.2}}})

