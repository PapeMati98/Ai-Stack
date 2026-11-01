"""Owner-authorized read-only PROD helpdesk collectors. No writes or raw bodies."""
from __future__ import annotations
import argparse,base64,json,shlex,subprocess,sys,urllib.request,urllib.error
from pathlib import Path
from datetime import datetime,timezone

_AUTO = Path(__file__).resolve().parents[1] / 'automation'
if str(_AUTO) not in sys.path:
    sys.path.insert(0, str(_AUTO))
from openclaw_ops.helpdesk import derive_revision

# Comments are not in the Workspace helpdesk_tickets schema (verified columns:
# id,title,description,type,status,urgency,importance,module_tag,reporter_id,
# assignee_id,deadline,approved_by_id,approved_at,rejected_reason,created_at,
# updated_at,closed_at,session_id,request_id,error_id). CRM JSON exposes
# title/description/status/severity and not a comments collection.
WORKSPACE_COVERAGE = 'open_bug_title_body_status_severity_comments_unavailable'
CRM_COVERAGE = 'authenticated_account_visible_bug_title_body_status_severity_comments_unavailable'

WORKSPACE_QUERY = '''import sqlite3,json,os
from dotenv import dotenv_values
from pathlib import Path
d=dotenv_values('/app/.env')
expected='sqlite:////app/data/workspace.db'
actual=os.getenv('DATABASE_URL', d.get('DATABASE_URL'))
default=os.getenv('WORKSPACE_DEFAULT_DB_NAME',d.get('WORKSPACE_DEFAULT_DB_NAME','workspace_dev.db'))
if actual not in (None,'',expected) or (not actual and default != 'workspace.db'):
    print(json.dumps({'status':'BLOCKED','reason':'production_database_binding_changed'}));raise SystemExit(0)
c=sqlite3.connect('file:/app/data/workspace.db?mode=ro',uri=True,timeout=3)
c.execute('PRAGMA query_only=ON')
c.row_factory=sqlite3.Row
rows=c.execute("SELECT id,title,description,status,urgency,importance,created_at,updated_at FROM helpdesk_tickets WHERE type='bug' AND status IN ('reported','under_review','approved','in_progress','deferred') ORDER BY id LIMIT 501").fetchall()
if len(rows)>500:
    print(json.dumps({'status':'BLOCKED','reason':'pagination_required','total_at_least':501}))
else:
    print(json.dumps({'status':'OK','environment':'prod','project':'example-workspace','items':[dict(r) for r in rows]}))
c.close()
'''

def content_revision(updated_at, *, title='', body='', status='', severity=''):
    """Hash available source fields in memory. Comments are not collected."""
    return derive_revision(updated_at, title=title or '', body=body or '',
                           status=status or '', severity=severity or '',
                           comments=None)

def workspace(run=subprocess.run):
    code=base64.b64encode(WORKSPACE_QUERY.encode()).decode()
    command='docker exec example-workspace-api python -c '+shlex.quote("exec(__import__('base64').b64decode('"+code+"'))")
    try:
        p=run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=8','example-workspace-prod',command],capture_output=True,timeout=30,encoding='utf-8')
        if p.returncode: return {'status':'BLOCKED','reason':'ssh_read_failed','project':'example-workspace'}
        if len(p.stdout.encode())>256_000: return {'status':'BLOCKED','reason':'oversize_response','project':'example-workspace'}
        result=json.loads(p.stdout)
        if result.get('status')!='OK': return result
        items=[]
        for r in result['items']:
            if not isinstance(r.get('id'),int) or r['status'] not in {'reported','under_review','approved','in_progress','deferred'}: raise ValueError()
            updated=str(r.get('updated_at') or r.get('created_at') or '')
            datetime.fromisoformat(updated.replace('Z','+00:00'))
            revision=content_revision(
                updated, title=r.get('title') or '', body=r.get('description') or '',
                status=r.get('status') or '',
                severity=f"{r.get('urgency') or ''}/{r.get('importance') or ''}")
            items.append({'issue_id':str(r['id']),'revision':revision,'status':r['status'],'priority':'high' if r.get('urgency')=='high' or r.get('importance')=='high' else 'unspecified','url':'https://workspace.example.invalid/helpdesk'})
        return {'status':'OK','environment':'prod','project':'example-workspace','items':items,'coverage':WORKSPACE_COVERAGE,'comments_coverage':'unavailable','read_only':True}
    except (OSError,subprocess.TimeoutExpired,ValueError,KeyError,TypeError):
        return {'status':'BLOCKED','reason':'invalid_or_unavailable_source','project':'example-workspace'}

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None

def crm(token: str|None=None,opener=None):
    # Token is supplied in memory by the approved local credential adapter only.
    # Absence never triggers cloud credential harvesting, token minting or browser-cookie extraction.
    if not token: return {'status':'BLOCKED','project':'example-crm','reason':'read_only_auth_not_configured'}
    opener=opener or urllib.request.build_opener(NoRedirect())
    req=urllib.request.Request('https://crm.example.invalid/api/helpdesk?kind=BUG',headers={'Authorization':'Bearer '+token,'Accept':'application/json'},method='GET')
    try:
        with opener.open(req,timeout=30) as response:
            raw=response.read(2_000_001)
        if len(raw)>2_000_000: raise ValueError('bounded')
        rows=json.loads(raw)
        if not isinstance(rows,list) or len(rows)>500: raise ValueError('schema')
        items=[]
        for r in rows:
            if r.get('kind')!='BUG': continue
            if r.get('status') not in {'NEW','CONFIRMED','IN_PROGRESS','TESTING'} and r.get('needsReview') is not True: continue
            issue=str(r.get('id',''))
            if not issue or len(issue)>80 or not all(c.isalnum() or c=='-' for c in issue): raise ValueError('identity')
            updated=str(r.get('updatedAt') or r.get('createdAt') or '')
            datetime.fromisoformat(updated.replace('Z','+00:00'))
            status=r.get('status')
            if status not in {'NEW','CONFIRMED','IN_PROGRESS','TESTING','FIXED','CLOSED','WONT_FIX'}: raise ValueError('status')
            severity=str(r.get('severity')) if r.get('severity') in {'CRITICAL','HIGH','MEDIUM','LOW'} else ''
            revision=content_revision(
                updated, title=r.get('title') or '', body=r.get('description') or '',
                status=status, severity=severity)
            items.append({'issue_id':issue,'revision':revision,'status':status,'priority':severity if severity else 'unspecified','needs_review':bool(r.get('needsReview')),'url':'https://crm.example.invalid/helpdesk'})
        return {'status':'OK','environment':'prod','project':'example-crm','items':items,'coverage':CRM_COVERAGE,'comments_coverage':'unavailable','read_only':True}
    except (OSError,urllib.error.HTTPError,ValueError,KeyError,TypeError):
        return {'status':'BLOCKED','reason':'auth_or_source_unavailable','project':'example-crm'}

def main():
    a=argparse.ArgumentParser();a.add_argument('--project',choices=['example-workspace','example-crm'],required=True);a.add_argument('--output',type=Path);args=a.parse_args()
    result=workspace() if args.project=='example-workspace' else crm()
    result['observed_at']=datetime.now(timezone.utc).isoformat()
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        # Evidence files are new per run, never overwrite prior observations.
        with args.output.open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status']=='OK' else 2

if __name__=='__main__':raise SystemExit(main())
