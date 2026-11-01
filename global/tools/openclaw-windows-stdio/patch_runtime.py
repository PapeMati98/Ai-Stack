"""Explicit, hash-bound apply/verify/rollback. No deleting or package install."""
import argparse,hashlib,json
from pathlib import Path
from changes import generate

HERE=Path(__file__).resolve().parent
PINNED_DIST=Path.home()/'.agents/runtime/openclaw/2026.9.4/node_modules/openclaw/dist'
PINS=json.loads((HERE/'pins.json').read_text(encoding='utf-8'))['files']
def sha(data):return hashlib.sha256(data).hexdigest()
def state(dist):
    rows=[]
    for pin in PINS:
        path=dist/pin['relative']
        if any(x.is_symlink() or (getattr(x.lstat(),'st_file_attributes',0)&0x400) for x in (path,*path.parents) if x.exists()):raise ValueError('linked runtime path')
        data=path.read_bytes();digest=sha(data)
        if digest not in (pin['before_sha256'],pin['after_sha256']):raise ValueError('unrecognised native source hash: '+pin['relative'])
        rows.append({'relative':pin['relative'],'sha256':digest,'state':'PATCHED' if digest==pin['after_sha256'] else 'ORIGINAL'})
    return rows
def apply(dist,receipt):
    rows=state(dist)
    if all(row['state']=='PATCHED' for row in rows):return {'status':'CURRENT','writes':0,'files':rows}
    if any(row['state']!='ORIGINAL' for row in rows):raise ValueError('mixed runtime state; recover using existing receipt')
    if receipt.exists():raise ValueError('receipt already exists; preserve it')
    sources={pin['relative']:(dist/pin['relative']).read_bytes() for pin in PINS}
    generated=generate({name:data.decode('utf-8').replace('\r\n','\n') for name,data in sources.items()})
    for pin in PINS:
        if sha(generated[pin['relative']].encode('utf-8'))!=pin['after_sha256']:raise ValueError('patch generator differs from accepted artifact')
    backup=receipt.parent/(receipt.stem+'-preimages');backup.mkdir(parents=True,exist_ok=False)
    record={'status':'PREPARED','dist':str(dist.resolve()),'files':[],'writes':0}
    for i,pin in enumerate(PINS):
        path=backup/(str(i)+'.preimage');path.write_bytes(sources[pin['relative']])
        if sha(path.read_bytes())!=pin['before_sha256']:raise ValueError('backup verification failed')
        record['files'].append({**pin,'backup':str(path)})
    receipt.write_text(json.dumps(record,indent=2),encoding='utf-8')
    try:
        for pin in PINS:
            (dist/pin['relative']).write_bytes(generated[pin['relative']].encode('utf-8'));record['writes']+=1
        if not all(row['state']=='PATCHED' for row in state(dist)):raise ValueError('post-apply verification failed')
        record['status']='APPLIED'
    except Exception:
        for pin in record['files']:
            current=sha((dist/pin['relative']).read_bytes())
            if current==pin['after_sha256']:(dist/pin['relative']).write_bytes(Path(pin['backup']).read_bytes())
        record['status']='ROLLED_BACK_ON_ERROR'
        receipt.write_text(json.dumps(record,indent=2),encoding='utf-8');raise
    receipt.write_text(json.dumps(record,indent=2),encoding='utf-8')
    return record
def rollback(dist,receipt):
    record=json.loads(receipt.read_text(encoding='utf-8'))
    if record.get('dist')!=str(dist.resolve()):raise ValueError('receipt destination mismatch')
    if record.get('files')!=[{**pin,'backup':str(receipt.parent/(receipt.stem+'-preimages')/(str(i)+'.preimage'))} for i,pin in enumerate(PINS)]:raise ValueError('receipt pins/backups mismatch')
    # Validate every destination and preimage before the first restore.
    state(dist)
    for pin in record['files']:
        if sha(Path(pin['backup']).read_bytes())!=pin['before_sha256']:raise ValueError('rollback preimage drift')
    writes=0
    for pin in record['files']:
        target=dist/pin['relative']
        if sha(target.read_bytes())!=pin['before_sha256']:target.write_bytes(Path(pin['backup']).read_bytes());writes+=1
    if not all(row['state']=='ORIGINAL' for row in state(dist)):raise ValueError('restore verification failed')
    record.update(status='ROLLED_BACK',rollback_writes=writes)
    receipt.write_text(json.dumps(record,indent=2),encoding='utf-8');return record
def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','verify','apply','rollback'])
    parser.add_argument('--dist',type=Path,default=PINNED_DIST)
    parser.add_argument('--receipt',type=Path)
    args=parser.parse_args()
    if args.dist.resolve()!=PINNED_DIST.resolve():parser.error('CLI writes only the pinned native OpenClaw dist; fixtures use Python API')
    if args.action in ('apply','rollback') and args.receipt is None:parser.error('explicit --receipt required')
    if args.action in ('plan','verify'):result={'status':'PASS','writes':0,'files':state(args.dist)}
    elif args.action=='apply':result=apply(args.dist,args.receipt)
    else:result=rollback(args.dist,args.receipt)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
