import json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from broker import Policy,Reject,LOGIN_ACTION
class SavedLoginNavigation(unittest.TestCase):
 def setUp(self):
  self.raw={"schema":1,"profile_id":"test","allowed_origins":["https://example.test"],"allowed_actions":["chrome_open",LOGIN_ACTION],"saved_login":{"origin":"https://example.test","pages":["/login"],"posts":["/login"]},"sessions":{"a":"a"*32},"extension_token":"e"*32,"extension_id":"a"*32,"expires_at":9999999999999}
 def test_exact_login_only_for_explicit_open(self):
  p=Policy.from_dict(self.raw)
  self.assertEqual(p.permits_url("https://example.test/login",saved_login_page=True),"https://example.test/login")
  with self.assertRaises(Reject):p.permits_url("https://example.test/login")
 def test_variants_and_foreign_routes_remain_denied(self):
  p=Policy.from_dict(self.raw)
  for url in ["https://example.test/login?code=x","https://example.test/login#token","https://example.test/login/","https://example.test/login/authorize","https://example.test/oauth","https://other.test/login"]:
   with self.subTest(url=url),self.assertRaises(Reject):p.permits_url(url,saved_login_page=True)
 def test_absent_policy_does_not_unlock_login(self):
  del self.raw["saved_login"];self.raw["allowed_actions"]=["chrome_open"]
  p=Policy.from_dict(self.raw)
  with self.assertRaises(Reject):p.permits_url("https://example.test/login",saved_login_page=True)
 def test_owner_binding_cannot_unlock_identity_provider_and_consent_routes(self):
  for field in ["pages","posts"]:
   for route in ["/oauth/authorize","/saml/consume","/consent","/passkey","/mfa","/challenge"]:
    raw=json.loads(json.dumps(self.raw));raw["saved_login"][field]=[route]
    with self.subTest(field=field,route=route),self.assertRaises(Reject):Policy.from_dict(raw)
if __name__=="__main__":unittest.main()
