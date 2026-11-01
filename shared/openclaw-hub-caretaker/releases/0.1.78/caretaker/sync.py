"""Explicit owner-only synchronization to the already configured Hermes Hub.

Explicit configured destinations, clean fast-forward only, no file deletions and no
credentials in configuration or output. A blocked sync is visible, never reset.
"""
import json,os,re,subprocess
from pathlib import Path,PurePosixPath
from .publication import PublicationError

OWNER_HUB=Path(__file__).resolve().parents[4]/'knowledge-hub'
REMOTE_SCRIPT=r'''
import json,subprocess,sys
from pathlib import Path
bindings=json.loads(BINDINGS_JSON)
r=Path(bindings['hub'])
def git(*args):
 p=subprocess.run(['git','-c','core.hooksPath=/dev/null','-C',str(r),*args],capture_output=True,text=True,timeout=90)
 if p.returncode:raise RuntimeError('remote_git_refused')
 return p.stdout.strip()
try:
 if git('symbolic-ref','--short','HEAD')!='contrib/robert-hermes':raise RuntimeError('unexpected_branch')
 if git('status','--porcelain'):raise RuntimeError('hermes_dirty_keep_pending')
 if git('remote','get-url','origin')!=bindings['origin']:raise RuntimeError('unexpected_remote')
 git('fetch','origin','main')
 target=git('rev-parse','origin/main');before=git('rev-parse','HEAD')
 git('merge-base','--is-ancestor',before,target)
 if git('diff','--name-only','--diff-filter=D',before,target):raise RuntimeError('file_deletion_refused')
 git('merge','--ff-only',target)
 p=subprocess.run(['python3',str(r/'skills/llm-wiki/scripts/knowledge.py'),'lint','--vault',str(r/'vault')],capture_output=True,text=True,timeout=120)
 if p.returncode or json.loads(p.stdout).get('error_count')!=0:raise RuntimeError('remote_lint_failed')
 print(json.dumps({'status':'SYNCED','before':before,'head':git('rev-parse','HEAD')}))
except Exception as exc:
 print(json.dumps({'status':'BLOCKED','reason':str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__}));sys.exit(1)
'''

from caretaker import tick_budget

def sync_hermes(vault: Path) -> dict:
    bindings={}
    for name,key in (('HUB_CARETAKER_HERMES_HUB_ROOT','hub'),('HUB_CARETAKER_HERMES_GIT_ORIGIN','origin')):
        value=os.environ.get(name,'')
        path=PurePosixPath(value)
        if not value.strip() or '\0' in value or not path.is_absolute() or value.startswith('//') or '..' in path.parts:
            raise PublicationError('sync_remote_binding_required:'+name)
        bindings[key]=value
    target=os.environ.get('HUB_CARETAKER_HERMES_SSH_TARGET','')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,254}',target):
        raise PublicationError('sync_remote_binding_required:HUB_CARETAKER_HERMES_SSH_TARGET')
    if Path(vault).resolve()!= (OWNER_HUB/'vault').resolve():
        raise PublicationError('sync_not_configured_owner_vault')
    owner=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(OWNER_HUB/'scripts/sync-owner.ps1'),'-Action','publish-main'],capture_output=True,timeout=tick_budget.timeout(180, minimum=10),creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    if owner.returncode:raise PublicationError('owner_sync_blocked_preserve_worktree')
    script=REMOTE_SCRIPT.replace('BINDINGS_JSON',repr(json.dumps(bindings)))
    remote=subprocess.run(['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',target,'python3','-'],input=script.encode(),capture_output=True,timeout=tick_budget.timeout(300, minimum=10),creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    try:result=json.loads(remote.stdout)
    except (ValueError,UnicodeError):raise PublicationError('hermes_sync_unverified')
    if remote.returncode or result.get('status')!='SYNCED':raise PublicationError('hermes_sync_blocked:'+str(result.get('reason','unknown')))
    return result
