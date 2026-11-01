"""Select one native terminal answer; do not scan code/tool/history as a report."""
import json
import re

def claude_terminal_value(raw):
    """Only a native successful result envelope, never assistant/tool history."""
    try:
        value=json.loads(raw)
        events=[value] if isinstance(value,dict) else []
    except (ValueError,RecursionError):
        events=[]
        try:
            events=[json.loads(line) for line in raw.splitlines() if line.strip()]
        except (ValueError,RecursionError):
            return None,None,'CLAUDE_STREAM_INCOMPLETE'
    results=[]; session=None
    for event in events:
        if not isinstance(event,dict): return None,None,'CLAUDE_EVENT_INVALID'
        if event.get('type')=='system' and event.get('subtype')=='init':
            session=event.get('session_id'); results=[]
        if (event.get('type')=='result' or len(events)==1 and 'result' in event
                and ('modelUsage' in event or 'duration_ms' in event)):
            if session and event.get('session_id')!=session:
                return None,None,'CLAUDE_SESSION_MISMATCH'
            results.append(event)
    if len(results)!=1: return None,None,'CLAUDE_TERMINAL_ABSENT_OR_CONFLICTING'
    result=results[0]
    if result.get('is_error') or result.get('subtype','success')!='success':
        return None,'failed','CLAUDE_TERMINAL_FAILED'
    value=result.get('structured_output')
    if value is None:
        text=terminal_payload(result.get('result'))
        try: value=json.loads(text) if text else None
        except (ValueError,RecursionError): value=None
    return value if isinstance(value,dict) else None,'success',None

def terminal_payload(text):
    if not isinstance(text,str): return None
    text=text.strip()
    fenced=re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```',text,re.S|re.I)
    if fenced: text=fenced.group(1).strip()
    if not text.startswith('{') or not text.endswith('}') or '```' in text:
        return None
    return text  # Existing bounded recovery may repair malformed terminal JSON.

def muse_terminal_text(raw):
    active=None; terminals=[]; malformed=0
    for line in raw.splitlines():
        try: event=json.loads(line)
        except (ValueError,RecursionError): malformed+=1; continue
        if not isinstance(event,dict): continue
        kind=event.get('payload_type') or event.get('type') or event.get('event') or ''
        payload=event.get('payload') if isinstance(event.get('payload'),dict) else event
        stream=payload.get('run_stream') or event.get('run_stream')
        if kind=='run.lifecycle.started':
            active=stream; terminals=[]
        elif kind.startswith('run.terminal.'):
            if not stream or active is not None and stream!=active:
                return None,'unknown','TERMINAL_RUN_MISMATCH_OR_MISSING'
            reason=payload.get('terminal') or kind.removeprefix('run.terminal.')
            terminals.append((str(reason).casefold(),payload.get('text')))
    if len(terminals)!=1:
        return None,None,'TERMINAL_ABSENT_OR_CONFLICTING'
    reason,text=terminals[0]
    if reason not in {'completed','success','succeeded','done'}:
        return None,reason,'TERMINAL_FAILED'
    if malformed:
        return None,reason,'TERMINAL_STREAM_INCOMPLETE'
    return text if isinstance(text,str) else None,reason,None
