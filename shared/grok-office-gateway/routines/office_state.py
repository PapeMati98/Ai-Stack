#!/usr/bin/env python3
"""Agent Office routine state (GO-STATE-20261024). Atomic RMW: flock (max 5s) + tmp/fsync/rename + sha256 + re-read.
Commands: init | show | get DK | reserve OP DK PAYLOAD_FILE | mark DK ATTEMPT_ID STATUS [SENT_ID]
STATUS in: sent (needs SENT_ID) | delivery_unknown | not_sent
Exit: 0 reserved -> caller MAY send exactly once with returned slot
      2 daily limit (deny)   3 BLOCKED (do not send)
      4 no send allowed (sent_id known -> read only / attempt still in flight)
      5 payload conflict under same dedupe_key (do not send)"""
import sys, os, json, fcntl, hashlib, tempfile, datetime, argparse, time, uuid
from zoneinfo import ZoneInfo
LIMIT=12; LOCK_TIMEOUT=5.0
def today(): return datetime.datetime.now(ZoneInfo("Europe/Warsaw")).date().isoformat()
def now(): return datetime.datetime.now(ZoneInfo("Europe/Warsaw")).isoformat(timespec="seconds")
def digest(d): return hashlib.sha256(json.dumps(d,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
class Blocked(Exception): pass
def out(code,**k): print(json.dumps(k,ensure_ascii=False)); return code
def load(p):
    if not os.path.exists(p): raise Blocked("state missing")
    try: w=json.load(open(p,encoding="utf-8"))
    except Exception as e: raise Blocked(f"corrupt json: {e}")
    if not isinstance(w,dict) or "data" not in w or w.get("sha256")!=digest(w["data"]): raise Blocked("checksum mismatch")
    return w["data"]
def save(p,d):
    w={"sha256":digest(d),"data":d}; dn=os.path.dirname(os.path.abspath(p))
    fd,tmp=tempfile.mkstemp(dir=dn,prefix=".tmp-")
    with os.fdopen(fd,"w",encoding="utf-8") as f: json.dump(w,f,ensure_ascii=False,indent=1); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,p); dfd=os.open(dn,os.O_RDONLY); os.fsync(dfd); os.close(dfd)
    if load(p)!=d: raise Blocked("write verification failed")
def lock(p):
    lf=open(p+".lock","a"); t=time.monotonic()
    while True:
        try: fcntl.flock(lf,fcntl.LOCK_EX|fcntl.LOCK_NB); return lf
        except BlockingIOError:
            if time.monotonic()-t>LOCK_TIMEOUT: raise Blocked("lock timeout 5s")
            time.sleep(0.05)
def main():
    a=argparse.ArgumentParser(); a.add_argument("--state",default="/workspace/agent-office-state/state.json")
    a.add_argument("cmd"); a.add_argument("args",nargs="*"); o=a.parse_args(); p=o.state; sentinel=p+".initialized"
    try:
        lf=lock(p)
        if o.cmd=="init":
            if os.path.exists(sentinel): load(p); return out(0,result="exists_valid")
            if os.path.exists(p): raise Blocked("state without sentinel")
            save(p,{"marker":"GO-STATE-20261024","limit":LIMIT,"counts":{},"entries":{}})
            open(sentinel,"w").write(now()); return out(0,result="initialized")
        if not os.path.exists(sentinel): raise Blocked("not initialized")
        d=load(p); day=today(); cnt=d["counts"].get(day,0)
        if o.cmd=="show": return out(0,day=day,count=cnt,state=d)
        if o.cmd=="get": return out(0,entry=d["entries"].get(o.args[0]))
        if o.cmd=="reserve":
            op,dk,pf=o.args
            try: payload=json.load(open(pf,encoding="utf-8"))
            except Exception as e: raise Blocked(f"payload unreadable: {e}")
            ps=digest(payload); e=d["entries"].get(dk)
            if e:
                if e["payload_sha256"]!=ps: return out(5,result="payload_conflict",dedupe_key=dk)
                if e.get("sent_id"): return out(4,result="sent_id_known_read_only",sent_id=e["sent_id"])
                last=e["attempts"][-1]["status"]
                if last not in ("delivery_unknown","not_sent"): return out(4,result="attempt_in_flight_no_send",last_status=last,slot=e["attempts"][-1]["slot"])
            if cnt>=LIMIT: return out(2,result="denied_daily_limit",day=day,count=cnt)
            cnt+=1; d["counts"][day]=cnt
            att={"attempt_id":uuid.uuid4().hex,"slot":cnt,"day":day,"reserved_at":now(),"status":"reserved"}
            if e: e["attempts"].append(att)
            else: d["entries"][dk]={"operation_id":op,"payload":payload,"payload_sha256":ps,"sent_id":None,"attempts":[att]}
            save(p,d); return out(0,result="reserved",day=day,slot=cnt,attempt_id=att["attempt_id"],attempt=len(d["entries"][dk]["attempts"]))
        if o.cmd=="mark":
            dk,aid,st=o.args[0],o.args[1],o.args[2]; sid=o.args[3] if len(o.args)>3 else None
            e=d["entries"].get(dk)
            if not e: raise Blocked("unknown dedupe_key")
            att=e["attempts"][-1]
            if att.get("attempt_id")!=aid or att["status"]!="reserved": raise Blocked("attempt_id mismatch or not reserved")
            if st=="sent":
                if not sid: raise Blocked("sent requires sent_id")
                e["sent_id"]=sid
            elif st not in ("delivery_unknown","not_sent"): raise Blocked("bad status")
            att["status"]=st; att["marked_at"]=now(); save(p,d); return out(0,result="marked",status=st)
        raise Blocked("unknown cmd")
    except Blocked as e: return out(3,result="BLOCKED",reason=str(e))
sys.exit(main())
