"""Pinned bundled Codex MCP transport. Local IPC only; never starts a second app server.

This exposes a narrow API. Delivery is a native agent delegation, not owner approval.
The caller supplies an explicit allowed thread and a trusted local rollout binding.
No app authentication, app databases, UI controls or executable shell strings are used.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

UUID = re.compile(r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$')
MAX_FRAME = 8 * 1024 * 1024
PIPE_PREFIX = '\\\\.\\pipe\\'
COMMAND_PREFIXES = ('/', '!', '@')
CAPABILITIES = {
    'kind': 'codex-desktop',
    'role': 'agent-delegation',
    'supports_exact_text': True,
    'supports_status_api': True,
    'supports_turn_start_observation': True,
    'supports_literal_prefixes': False,
    'supports_cancel': False,
    'supports_native_idempotency': False,
    'supports_atomic_last_turn_cas': False,
    'supports_latest_turn_items_in_status': False,
    'exact_match_source': 'read-only-rollout-tail',
    'cli_fallback': False,
    'second_app_server': False,
}

class Unavailable(RuntimeError): pass
class DeliveryUnknown(RuntimeError): pass

def pipe_available(pipe_path: str) -> bool:
    """True when the existing Desktop named pipe is present. Never creates one."""
    if not isinstance(pipe_path, str) or not pipe_path.startswith(PIPE_PREFIX):
        return False
    if '\n' in pipe_path or '\r' in pipe_path or '\x00' in pipe_path:
        return False
    try:
        return os.path.exists(pipe_path)
    except OSError:
        return False

def process_available(pid: int) -> bool:
    """True when the configured Desktop PID is still alive. Never starts it."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == 'nt':
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True

class AppTools:
    def __init__(self, node: str, server: str, server_sha256: str,
                 caller_thread: str, pipe_path: str, *, require_pipe: bool = True):
        if not UUID.fullmatch(caller_thread): raise ValueError('Invalid caller identity')
        executable, script = Path(node), Path(server)
        if not executable.is_absolute() or not executable.is_file(): raise ValueError('Missing pinned Node')
        if not script.is_absolute() or not script.is_file() or script.is_symlink(): raise ValueError('Invalid plugin path')
        if hashlib.sha256(script.read_bytes()).hexdigest() != server_sha256: raise Unavailable('Plugin changed; capability revalidation required')
        if not pipe_path.startswith(PIPE_PREFIX) or '\n' in pipe_path: raise ValueError('Local named pipe required')
        if require_pipe and not pipe_available(pipe_path):
            raise Unavailable('Desktop pipe unavailable')
        self.caller_thread = caller_thread
        env = os.environ.copy(); env['CODEX_APP_TOOLS_PIPE_PATH'] = pipe_path
        env.pop('TELEGRAM_BOT_TOKEN', None)
        self.proc = subprocess.Popen([str(executable), str(script)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            creationflags=0x08000000 if os.name=='nt' else 0)
        self.queue = queue.Queue(maxsize=100); self.counter=0; self.lock=threading.Lock()
        threading.Thread(target=self._read,daemon=True).start()
        try:
            self.request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},
                         'clientInfo':{'name':'openclaw-desktop-broker','version':'0.1'}})
            self.proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'); self.proc.stdin.flush()
            tools=self.request('tools/list',{}).get('tools',[])
            self.tool_names={t['name'] for t in tools}
            if not {'list_threads','read_thread','send_message_to_thread'} <= self.tool_names:
                raise Unavailable('Required native methods unavailable')
        except Exception:
            self.close(); raise

    def _read(self):
        try:
            while True:
                line=self.proc.stdout.readline(MAX_FRAME+1)
                if not line: break
                if len(line)>MAX_FRAME: break
                value=json.loads(line)
                self.queue.put(value,timeout=1)
        except Exception: pass
        finally:
            try:self.queue.put_nowait(None)
            except queue.Full:pass

    def request(self,method,params,timeout=20):
        with self.lock:
            if self.proc.poll() is not None:
                raise Unavailable('Desktop connection closed')
            self.counter+=1; request_id=self.counter
            payload=json.dumps({'jsonrpc':'2.0','id':request_id,'method':method,'params':params},ensure_ascii=False).encode('utf-8')
            if len(payload)>MAX_FRAME:raise ValueError('Request too large')
            try:
                self.proc.stdin.write(payload+b'\n');self.proc.stdin.flush()
                deadline=time.monotonic()+timeout
                while True:
                    value=self.queue.get(timeout=max(.01,deadline-time.monotonic()))
                    if value is None:raise Unavailable('Desktop connection closed')
                    if value.get('id')!=request_id:continue
                    if 'error' in value:raise Unavailable('Native request rejected')
                    return value.get('result',{})
            except (OSError,queue.Empty) as exc:
                raise Unavailable('Native result unavailable') from None

    def call(self,name,args):
        if name not in {'list_threads','read_thread','wait_threads','send_message_to_thread'}:raise ValueError('Operation not allowed')
        result=self.request('tools/call',{'name':name,'arguments':args,
                           '_meta':{'threadId':self.caller_thread}})
        if result.get('isError'):raise Unavailable('Native operation refused')
        blocks=result.get('content',[])
        for block in blocks:
            if block.get('type')=='text':
                try:return json.loads(block['text'])
                except ValueError:raise Unavailable('Native schema changed') from None
        raise Unavailable('Missing native result')

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=5)

class CodexDesktop:
    kind='codex-desktop'
    # Cancel is deliberately absent in installed app-tools API.
    supports_cancel=False
    supports_native_idempotency=False
    supports_atomic_last_turn_cas=False
    supports_latest_turn_items_in_status=False
    supports_literal_prefixes=False
    capabilities=CAPABILITIES
    def __init__(self, app: AppTools, allowed_threads: dict[str,dict], sessions_root: Path):
        self.app=app;self.allowed_threads=allowed_threads;self.sessions_root=sessions_root.resolve()
        if not self.sessions_root.is_dir():raise ValueError('Missing native sessions root')
        for thread,info in allowed_threads.items():
            if not UUID.fullmatch(thread):raise ValueError('Invalid target ID')
            path=Path(info['rollout_path'])
            if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(self.sessions_root):raise ValueError('Invalid rollout binding')
            if not path.name.endswith(thread+'.jsonl'):raise ValueError('Rollout identity mismatch')

    def status(self,thread):
        self._allowed(thread)
        value=self.app.call('read_thread',{'threadId':thread,'turnLimit':1,'includeOutputs':False,'maxOutputCharsPerItem':100})
        native=value.get('thread',{})
        if native.get('id')!=thread:raise Unavailable('Wrong native target')
        turns=value.get('turns',[])
        last=turns[0].get('id') if turns else None
        turn_status=turns[0].get('status') if turns else None
        from_status=bool(turns)
        # Status API omits latest turn items. Fall back to the rollout tail.
        if last is None:
            last=self._rollout_last_turn(thread)
        return {'target_id':thread,'state':native.get('status',{}).get('type','unknown'),
                'last_turn_id':last,'observed_at':time.time(),
                'native_turn_status':turn_status,
                'user_presence':'unknown','atomic_compare_and_swap':False,
                'turn_items_from_status':from_status,
                'supports_cancel':False,'supports_atomic_last_turn_cas':False}

    def _allowed(self,thread):
        if thread not in self.allowed_threads:raise ValueError('Target not enrolled')

    def _rollout_last_turn(self,thread):
        path=Path(self.allowed_threads[thread]['rollout_path'])
        if path.is_symlink() or not path.resolve().is_relative_to(self.sessions_root):
            raise Unavailable('Rollout binding changed')
        if not path.is_file():
            return None
        with path.open('rb') as stream:
            stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-MAX_FRAME))
            if size>MAX_FRAME:stream.readline()
            raw=stream.read(MAX_FRAME)
        last=None
        for line in raw.splitlines():
            try:e=json.loads(line)
            except ValueError:continue
            p=e.get('payload',{})
            meta=p.get('internal_chat_message_metadata_passthrough') or {}
            for candidate in (meta.get('turn_id'), p.get('turn_id')):
                if isinstance(candidate,str) and candidate:
                    last=candidate
        return last

    def send(self,thread,text,expected_last_turn,*,observed_at,supervisor=False):
        self._allowed(thread)
        if not isinstance(text,str) or not text or len(text.encode('utf-8'))>16000:raise ValueError('Text outside proven size')
        # Leading command behavior not tested. Never silently escape/rewrite input.
        if text.lstrip().startswith(COMMAND_PREFIXES):raise ValueError('Leading native command prefix not yet validated')
        if supervisor and not text.startswith('[OpenClaw — nadzór]'):raise ValueError('Supervisor sender label required')
        now=time.time()
        if now-observed_at>10 or observed_at>now+1:raise Unavailable('Stale observation')
        current=self.status(thread)
        if current['state']!='idle' or current['last_turn_id']!=expected_last_turn:raise Unavailable('Target active or changed')
        # No CAS is claimed: caller must serialize broker workers and pause on owner takeover.
        try:
            result=self.app.call('send_message_to_thread',{'threadId':thread,'prompt':text})
        except Exception:raise DeliveryUnknown('Send outcome unknown; readback required, do not resend') from None
        if result.get('threadId')!=thread:raise DeliveryUnknown('Unverified delivery acknowledgement')
        return {'target_id':thread,'state':'acknowledged','text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
                'role':'agent-delegation','turn_started':False}

    def readback(self,thread,text,*,not_before):
        self._allowed(thread)
        path=Path(self.allowed_threads[thread]['rollout_path'])
        if path.is_symlink() or not path.resolve().is_relative_to(self.sessions_root):raise Unavailable('Rollout binding changed')
        # Read only a bounded tail. If rotated/oversized/incomplete: UNKNOWN, never resend.
        with path.open('rb') as stream:
            stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-MAX_FRAME))
            if size>MAX_FRAME:stream.readline()
            raw=stream.read(MAX_FRAME)
        prefix='<codex_delegation>\n  <source_thread_id>'+self.app.caller_thread+'</source_thread_id>\n  <input>'
        expected=prefix+text+'</input>\n</codex_delegation>'
        matches=[];completed={};tools={}
        for line in raw.splitlines():
            try:e=json.loads(line)
            except ValueError:continue
            p=e.get('payload',{})
            if e.get('type')=='response_item' and p.get('type')=='function_call_output' and p.get('name')=='send_message_to_thread' and p.get('output')==expected:
                meta=p.get('internal_chat_message_metadata_passthrough',{})
                if meta.get('create_time',0)>=not_before:matches.append(meta.get('turn_id'))
            if e.get('type')=='event_msg' and p.get('type')=='task_complete':completed[p.get('turn_id')]=p.get('last_agent_message','')
        matches=list(dict.fromkeys(matches))
        if len(matches)!=1:return {'state':'delivery_unknown','matching_turns':len(matches)}
        turn=matches[0]
        return {'state':'completed' if turn in completed else 'running','turn_id':turn,
                'exact_unicode_match':True,'text_sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
                'result':completed.get(turn),'source':'read-only native rollout','role':'agent-delegation'}
