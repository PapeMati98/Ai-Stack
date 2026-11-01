"""Add only this reviewed MCP entry to existing Windows tool configurations.

The OAuth credential file is not opened. Private config preimages can contain unrelated
secrets and stay inside the already protected OAuth directory. No app restarts.
"""
import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import client_config as fmt

FILES = ('server.py','adc_entry.py','client_config.py','register.py','README.md',
         'policy.example.json','test_server.py','test_adc_entry.py','test_stdio.py','test_client_config.py')


def digest(b): return hashlib.sha256(b).hexdigest()


def make_entry(spec, pythonw, entry, policy, credentials):
    args=['-I',str(entry),'--policy',str(policy),'--credentials-file',str(credentials)]
    if spec.get('shape')=='opencode':
        return {'type':'local','command':[str(pythonw),*args],'enabled':True,'timeout':30000}
    result={'command':str(pythonw),'args':args}
    if spec['id'] in ('codex','grok'): result['enabled']=True
    if spec['id']=='codex': result['startup_timeout_sec']=30
    if spec['id']=='minimax': result.update(type='stdio',enabled=True)
    if spec['id'] in ('kimi-code','kimi-desktop'): result.update(enabled=True,startupTimeoutMs=30000)
    if spec['id']=='hermes': result['connect_timeout']=30
    return result


def source_hashes(root):
    return {name:digest((root/name).read_bytes()) for name in FILES}


def deploy(specs, pythonw, entry, policy, credentials, backup, evidence, apply=False, approved_hashes=None):
    source=Path(__file__).resolve().parent
    current=source_hashes(source)
    for path in (pythonw,entry,policy,credentials,backup):
        if not path.is_absolute(): raise ValueError('Absolute paths required')
        fmt.ensure_plain(path)
    if pythonw.name.lower()!='pythonw.exe': raise ValueError('GUI Python launcher required')
    if not credentials.is_relative_to(backup.parent) or not policy.is_relative_to(backup.parent):
        raise ValueError('Private storage mismatch')
    fmt.server=lambda s:make_entry(s,pythonw,entry,policy,credentials)
    changes=[]; skipped=[]
    for spec in specs:
        path=Path(spec['path']);fmt.ensure_plain(path)
        if not path.is_file(): skipped.append(spec['id']);continue
        before=path.read_bytes();after=fmt.patch(spec,before)
        changes.append((spec,path,before,after))
    report={'applied':False,'source_hashes':current,'skipped_absent':skipped,
            'clients':[{'id':s['id'],'path':str(p),'changed':a!=b,'before_hash':digest(a),'after_hash':digest(b)} for s,p,a,b in changes]}
    evidence.write_text(json.dumps(report,indent=2),encoding='utf-8')
    if not apply:return report
    if approved_hashes!=current:raise ValueError('Current source approval required')
    if not backup.parent.is_dir():raise ValueError('Existing protected private directory required')
    if source_hashes(entry.parent)!=current:raise ValueError('Installed source mismatch')
    # Parent validates the private root DACL before calling --apply.
    for s,p,a,b in changes:
        if p.read_bytes()!=a:raise ValueError('Concurrent configuration change')
    backup.mkdir(exist_ok=False)
    for s,p,a,b in changes:
        if a!=b:(backup/(s['id']+'.preimage')).write_bytes(a)
    written=[]
    try:
        for s,p,a,b in changes:
            fmt.ensure_plain(p)
            if p.read_bytes()!=a:raise ValueError('Concurrent configuration change')
            if a==b:continue
            written.append((p,a,b))
            with p.open('wb') as stream:stream.write(b);stream.flush()
            if p.read_bytes()!=b:raise ValueError('Configuration verification failed')
    except Exception:
        report['rollback_pending']=[]
        for p,a,b in reversed(written):
            if p.read_bytes()==b:p.write_bytes(a)
            else:report['rollback_pending'].append(str(p))
        evidence.write_text(json.dumps(report,indent=2),encoding='utf-8')
        raise
    report.update(applied=True,private_backup_root=str(backup))
    evidence.write_text(json.dumps(report,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    for key in ('clients','pythonw','entry','policy','credentials','backup','evidence'):
        parser.add_argument('--'+key,required=True,type=Path)
    parser.add_argument('--approved-hashes',type=Path)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:
        result=deploy(json.loads(args.clients.read_text(encoding='utf-8')),args.pythonw,args.entry,args.policy,args.credentials,args.backup,args.evidence,
                      args.apply,json.loads(args.approved_hashes.read_text()) if args.approved_hashes else None)
        print(json.dumps({'applied':result['applied'],'clients':len(result['clients']),'changed':sum(x['changed'] for x in result['clients']),'skipped_absent':result['skipped_absent']}))
    except Exception as e:
        print(json.dumps({'applied':False,'error':type(e).__name__,'reason':str(e) if isinstance(e,ValueError) else 'Registration failed'}))
        raise SystemExit(1)
