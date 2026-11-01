"""Raw official results cross bounded frames; no transcript projection by model."""
import io, json, time
import pytest
from coordfs import codex_relay_cycle as c

TOKEN='1'*32

def frames(raw):
    data=json.dumps(raw,ensure_ascii=True,separators=(',',':'))
    chunks=[data[i:i+480] for i in range(0,len(data),480)]
    return [dict(token=TOKEN,part=i,data=s,final=i==len(chunks)-1) for i,s in enumerate(chunks)]

def host(rows):
    data=''.join(json.dumps(r)+'\n' for r in rows).encode()
    return c.HostIO(io.BytesIO(data),io.BytesIO(),time.monotonic)

def test_large_raw_result_and_unicode_preserved():
    raw={'content':[{'type':'text','text':'Zażółć 🛰 '+('synthetic-only '*80000)}],'isError':False}
    rows=frames(raw)
    assert max(len(json.dumps(r)) for r in rows)<1024
    assert host(rows).read_result(TOKEN)==raw

@pytest.mark.parametrize('change',[lambda r:r.update(part=2),lambda r:r.update(token='2'*32),
    lambda r:r.update(data='ą'),lambda r:r.update(data='x'*481),lambda r:r.update(final='true'),
    lambda r:r.update(unexpected=True)])
def test_malformed_chunk_refuses(change):
    rows=frames({'content':[{'text':'synthetic-only'*100}]});change(rows[0])
    with pytest.raises(c.CycleError):host(rows).read_result(TOKEN)

def test_truncated_result_and_legacy_interleaving_refuse():
    rows=frames({'content':[{'text':'synthetic-only'*100}]})
    with pytest.raises(c.CycleError):host(rows[:-1]).read_result(TOKEN)
    with pytest.raises(c.CycleError):host([rows[0],{'token':TOKEN,'result':{}}]).read_result(TOKEN)

def test_result_limit_refuses_without_send(monkeypatch):
    monkeypatch.setattr(c,'_MAX_RESULT_CHARS',100)
    with pytest.raises(c.CycleError,match='stdin_result_oversized'):
        host(frames({'content':[{'text':'synthetic-only'*100}]})).read_result(TOKEN)
