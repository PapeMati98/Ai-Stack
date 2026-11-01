"""Exact-file read-only MCP. No account access, network, shell or write surface."""
from __future__ import annotations
import argparse,json,os,sys
from pathlib import Path
from typing import Any
import zcode_read_paths as paths

MAX_FRAME=65536
MAX_OUTPUT=16384
MAX_FILE=1048576
SCHEMA='fleet-zcode-read/1'
TOOLS=[{'name':'fleet_read_file','description':'Read UTF-8 bytes from one admitted file; offset and limit are byte counts.','inputSchema':{'type':'object','properties':{'path':{'type':'string'},'offset':{'type':'integer','minimum':0},'limit':{'type':'integer','minimum':1,'maximum':8192}},'required':['path'],'additionalProperties':False}}, {'name':'fleet_list_files','description':'List the exact admitted file paths; never scans a directory.','inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]
CHROME_NAMES=('fleet_chrome_attachment','fleet_chrome_open','fleet_chrome_read','fleet_chrome_wait','fleet_chrome_close')


def _relative(value: Any) -> str:
    relative=paths.normalize_relative(value)
    if any(part.casefold()=='.zcode' for part in relative.split('/')): raise ValueError('Protected path')
    return relative

def _signature(info: os.stat_result) -> list[int]:
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns]

def _open_identity(info: os.stat_result) -> list[int]:
    # Windows Python may expose birth time in lstat.ctime and change time in
    # fstat.ctime. Compare shared identity fields across APIs; still compare
    # each API's full signature before/after to refuse mutations.
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns]

def _utf8_width(first: int) -> int | None:
    if first<0x80: return 1
    if 0xC2<=first<=0xDF: return 2
    if 0xE0<=first<=0xEF: return 3
    if 0xF0<=first<=0xF4: return 4
    return None

def _info(workspace: Path, relative: str) -> os.stat_result:
    target=workspace/relative;paths._check_directory_chain(target.parent)
    return paths._regular_file(target,MAX_FILE)

def build_policy(workspace: Path | str, admitted_files: list[str], max_tool_calls: int, *, owner_home: str | None=None, chrome_assignment: dict | None=None) -> dict:
    if type(max_tool_calls) is not int or not 1<=max_tool_calls<=1000: raise ValueError('Mandatory cumulative budget')
    if not isinstance(admitted_files,list) or not 1<=len(admitted_files)<=256: raise ValueError('Exact files required')
    owner_home=owner_home or str(Path.home());base=paths.validate_workspace(str(Path(workspace).absolute()),owner_home); files=[_relative(f) for f in admitted_files]
    if len({f.casefold() for f in files})!=len(files): raise ValueError('Duplicate admissions')
    policy={'schema':SCHEMA,'workspace':str(base),'owner_home':owner_home,'files':files,'max_tool_calls':max_tool_calls,'fingerprints':{f:_signature(_info(base,f)) for f in files}}
    if chrome_assignment is not None:
        import fleet_chrome_integration as chrome
        route=chrome.validate_fleet_assignment(chrome_assignment,'zcode')
        if route['transport']!='cli' or route['mode']!='review': raise ValueError('ZCode Chrome is review-only CLI')
        policy['chrome_assignment']=route
    return policy

def _validate(value: Any) -> dict:
    base={'schema','workspace','owner_home','files','max_tool_calls','fingerprints'}
    if not isinstance(value,dict) or set(value) not in (base,base|{'chrome_assignment'}) or value.get('schema')!=SCHEMA: raise ValueError('Policy shape')
    rebuilt=build_policy(value['workspace'],value['files'],value['max_tool_calls'],owner_home=value['owner_home'],chrome_assignment=value.get('chrome_assignment'))
    if rebuilt!=value: raise ValueError('Policy or admitted files changed')
    return rebuilt

def _error(mid: Any, code: int=-32602) -> dict:
    return {'jsonrpc':'2.0','id':mid,'error':{'code':code,'message':'Fleet read request refused'}}

def bounded(value: dict) -> str:
    text=json.dumps(value,ensure_ascii=False,separators=(',',':'))
    try: wire=text.encode('utf-8')
    except UnicodeError:
        # Refuse malformed surrogate text explicitly; never replace wire bytes.
        text=json.dumps(_error(value.get('id'),-32000),separators=(',',':'))
        wire=text.encode('ascii')
    if len(wire)>MAX_OUTPUT:
        text=json.dumps(_error(value.get('id'),-32000),separators=(',',':'))
    return text

def _write_stdout(text: str) -> None:
    wire=text.encode('utf-8')+b'\n'
    sys.stdout.buffer.write(wire)
    sys.stdout.buffer.flush()

def _write_stderr(text: str) -> None:
    # Diagnostics are fixed ASCII literals; use bytes so Windows console code
    # pages and Python UTF-8 environment flags cannot change their encoding.
    sys.stderr.buffer.write(text.encode('ascii')+b'\n')
    sys.stderr.buffer.flush()

class Broker:
    def __init__(self,policy: dict, *, cli_factory=None):
        self.policy=_validate(policy);self.calls=0;self.chrome_cli=None;self.tools=list(TOOLS)
        assignment=self.policy.get('chrome_assignment')
        if assignment is not None:
            import fleet_chrome_integration as chrome
            self.chrome_cli=(cli_factory or chrome.ManagedCLI)(assignment,max_calls=self.policy['max_tool_calls'])
            for spec in chrome.tool_specs(read_only=True):
                spec=dict(spec);spec['name']='fleet_'+spec['name']
                self.tools.append(spec)

    def close(self):
        if self.chrome_cli is not None:
            self.chrome_cli.close();self.chrome_cli=None

    def _read(self,args: Any) -> dict:
        if not isinstance(args,dict) or not {'path'}<=set(args)<={'path','offset','limit'}: raise ValueError('Read args')
        relative=_relative(args['path']); offset=args.get('offset',0);limit=args.get('limit',8192)
        if type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=8192: raise ValueError('Byte bounds')
        if relative not in self.policy['files']: raise ValueError('Not admitted')
        base=Path(self.policy['workspace']);target=base/relative;before=_info(base,relative)
        if _signature(before)!=self.policy['fingerprints'][relative]: raise ValueError('File drift')
        requested_offset=offset;actual_offset=offset;next_min_limit=None
        with target.open('rb') as stream:
            opened=os.fstat(stream.fileno())
            if _open_identity(opened)!=_open_identity(before) or opened.st_nlink!=1: raise ValueError('Open drift')
            if 0<offset<before.st_size:
                stream.seek(offset);current=stream.read(1)
                if current and 0x80<=current[0]<=0xBF:
                    prefix_start=max(0,offset-3);stream.seek(prefix_start);prefix=stream.read(offset-prefix_start)
                    back=0
                    for byte in reversed(prefix):
                        if 0x80<=byte<=0xBF: back+=1;continue
                        if 0xC2<=byte<=0xF4: back+=1
                        break
                    actual_offset=offset-back
            stream.seek(actual_offset);data=stream.read(limit)
            try: content=data.decode('utf-8')
            except UnicodeDecodeError as exc:
                if exc.reason!='unexpected end of data' or actual_offset+len(data)>=before.st_size: raise ValueError('Not UTF8 text') from exc
                next_min_limit=_utf8_width(data[exc.start])
                if next_min_limit is None: raise ValueError('Not UTF8 text') from exc
                data=data[:exc.start];content=data.decode('utf-8')
            next_offset=actual_offset+len(data)
            if next_offset<before.st_size and next_min_limit is None:
                stream.seek(next_offset);following=stream.read(1)
                if following: next_min_limit=_utf8_width(following[0])
            if _signature(os.fstat(stream.fileno()))!=_signature(opened): raise ValueError('Read drift')
        if _signature(_info(base,relative))!=_signature(before): raise ValueError('Path drift')
        if '\0' in content: raise ValueError('Not UTF8 text')
        return {'path':relative,'content':content,'offset':actual_offset,'requested_offset':requested_offset,'bytes':len(data),'file_bytes':before.st_size,'next_offset':next_offset,'next_min_limit':next_min_limit,'truncated':next_offset<before.st_size}

    def handle(self,message: Any) -> dict | None:
        mid=None
        try:
            if not isinstance(message,dict) or set(message)-{'jsonrpc','id','method','params'} or message.get('jsonrpc')!='2.0': return _error(None)
            mid=message.get('id');method=message.get('method');params=message.get('params',{})
            if mid is not None and (type(mid) not in (int,str) or len(str(mid).encode())>128): return _error(None)
            if not isinstance(method,str) or not isinstance(params,dict): return _error(mid)
            if method=='notifications/initialized' and mid is None: return None
            if method=='initialize':
                return {'jsonrpc':'2.0','id':mid,'result':{'protocolVersion':'2024-11-05','capabilities':{'tools':{}},'serverInfo':{'name':'fleet-zcode-read','version':'1'}}}
            if method=='tools/list': return {'jsonrpc':'2.0','id':mid,'result':{'tools':self.tools}}
            if method!='tools/call': return _error(mid,-32601)
            self.calls+=1
            if self.calls>self.policy['max_tool_calls']: raise ValueError('Budget')
            if set(params)-{'name','arguments','_meta'} or type(params.get('name')) is not str: raise ValueError('Call args')
            name=params['name'];args=params.get('arguments',{})
            if name=='fleet_read_file': value=self._read(args)
            elif name=='fleet_list_files' and args=={}: value={'files':self.policy['files']}
            elif name in CHROME_NAMES and self.chrome_cli is not None:
                value=self.chrome_cli.call(name,args)
            else: raise ValueError('Unknown tool')
            result={'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False)}]}
            if name in CHROME_NAMES and value.get('status') in {'denied','unknown'}: result['isError']=True
            response={'jsonrpc':'2.0','id':mid,'result':result}
            if len(json.dumps(response,ensure_ascii=False).encode('utf-8'))>MAX_OUTPUT: raise ValueError('Output bound')
            return response
        except (ValueError,TypeError,OSError,KeyError,RecursionError):
            return _error(mid,-32000)

def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument('--policy',type=Path,required=True);args=parser.parse_args()
    try:
        info=paths._regular_file(args.policy,MAX_FRAME)
        broker=Broker(json.loads(paths._read_bytes(args.policy,info,MAX_FRAME).decode('utf-8')))
        import atexit; atexit.register(broker.close)
    except (ValueError,TypeError,OSError,UnicodeError,RecursionError): _write_stderr('Fleet read policy refused');return 126
    while True:
        raw=sys.stdin.buffer.readline(MAX_FRAME+1)
        if not raw: return 0
        if len(raw)>MAX_FRAME: _write_stdout(bounded(_error(None)));return 126
        try: message=json.loads(raw)
        except (ValueError,UnicodeError,RecursionError): _write_stdout(bounded(_error(None)));continue
        value=broker.handle(message)
        if value is not None: _write_stdout(bounded(value))

if __name__=='__main__': raise SystemExit(main())
