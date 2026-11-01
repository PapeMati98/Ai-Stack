"""Format preserving add-only MCP configuration helpers, reused from the local installer."""
import json, pathlib, re, tomllib
import yaml
NAME="gcp-readonly"

def ensure_plain(p):
    for node in [p, *p.parents]:
        try:
            meta = node.lstat()  # includes dangling links/reparse points
        except FileNotFoundError:
            continue
        if node.is_symlink():
            raise ValueError('Redirected destination')
        if getattr(meta, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Reparse destination')


def object_bounds(text, path):
    """Find the exact JSON object span, while preserving all unrelated bytes."""
    decoder=json.JSONDecoder()
    start=len(text)-len(text.lstrip())
    for key in path:
        if text[start]!='{': raise ValueError('Expected JSON object')
        pos=start+1; found=False
        while True:
            while text[pos].isspace() or text[pos]==',': pos+=1
            if text[pos]=='}': break
            name,end=decoder.raw_decode(text,pos)
            pos=end
            while text[pos].isspace(): pos+=1
            if text[pos]!=':': raise ValueError('Invalid JSON key')
            pos+=1
            while text[pos].isspace(): pos+=1
            _,end=decoder.raw_decode(text,pos)
            if name==key: start=pos; found=True; break
            pos=end
        if not found: return None
    value,end=decoder.raw_decode(text,start)
    if not isinstance(value,dict): raise ValueError('Expected JSON object')
    return start,end,value

def json_insert(text, path, value):
    parsed=json.loads(text)
    if not isinstance(parsed,dict): raise ValueError('Config root must be an object')
    # Missing intermediate keys are constructed as a single new subtree.
    for depth in range(len(path)-1,-1,-1):
        bounds=object_bounds(text,path[:depth])
        if bounds is not None:
            start,end,obj=bounds; key=path[depth]
            if key in obj:
                if obj[key]==value: return text
                raise ValueError('Existing target conflicts')
            for tail in reversed(path[depth+1:]): value={tail:value}
            addition=(',' if obj else '')+'\n'+json.dumps(key)+': '+json.dumps(value,ensure_ascii=False)+'\n'
            result=text[:end-1]+addition+text[end-1:]
            json.loads(result)
            return result
    raise ValueError('Config object unavailable')

def patch(spec, original):
    text=original.decode('utf-8-sig')
    bom=b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b''
    if not text.strip(): text='{}\n' if spec['format']=='json' else ''
    entry=server(spec)
    if spec['format']=='json':
        # Strict JSON only: JSONC with comments fails instead of losing them.
        result=json_insert(text,[*spec['key'].split('.'),NAME],entry)
    elif spec['format']=='toml':
        parsed=tomllib.loads(text)
        entries=parsed.get(spec['key'],{})
        if NAME in entries:
            if entries[NAME]==entry: return original
            raise ValueError('Existing target conflicts')
        # The emitted table and the exact-equality gate share one entry.
        result=text+'\n['+spec['key']+'.'+NAME+']\n'
        for key,value in entry.items():
            if not isinstance(value,(str,bool,int,list)):
                raise ValueError('Unsupported TOML entry value')
            result+=json.dumps(key)+' = '+json.dumps(value,ensure_ascii=False)+'\n'
        tomllib.loads(result)
    else:
        parsed=yaml.safe_load(text) or {}
        entries=parsed.get(spec['key'],{}) or {}
        if NAME in entries:
            if entries[NAME]==entry: return original
            raise ValueError('Existing target conflicts')
        payload='  '+NAME+': '+json.dumps(entry)+'\n'
        if spec['key'] not in parsed:
            result=text+'\n'+spec['key']+':\n'+payload
        else:
            match=re.search(r'^'+re.escape(spec['key'])+r':[^\n]*(?:\n|$)',text,re.M)
            if not match or match.group().strip() not in [spec['key']+':',spec['key']+': {}']:
                raise ValueError('Unsupported inline YAML MCP block')
            result=text[:match.start()]+spec['key']+':\n'+payload+text[match.end():]
        check=yaml.safe_load(result)
        before=dict(parsed); after=dict(check)
        before.pop(spec['key'],None); after.pop(spec['key'],None)
        if before!=after or check[spec['key']].get(NAME)!=entry:
            raise ValueError('YAML preservation failed')
    return bom+result.encode('utf-8')

