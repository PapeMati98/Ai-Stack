"""Compact canonical Hub context; preserve sources and verification warnings."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


class KnowledgeConfigError(RuntimeError):
    """Raised when an explicit AGENT_KNOWLEDGE_CONFIG binding is unusable.

    Carries a fixed machine-readable ``code``; never falls back to the host
    default config after an explicit binding was supplied.
    """

    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail
        super().__init__(code + (": " + detail if detail else ""))


def _config_path(config_path=None):
    """Resolve the knowledge config path.

    Precedence: explicit ``config_path`` argument, then the
    ``AGENT_KNOWLEDGE_CONFIG`` environment override, then the installed default
    ``~/.agents/knowledge-config.json``. A supplied override must be exactly one
    absolute path to an existing regular file (not a symlink); a missing,
    relative, or link path fails closed with no home/global fallback.
    """
    if config_path is not None:
        return Path(config_path)
    override = os.environ.get("AGENT_KNOWLEDGE_CONFIG")
    if override is not None:
        if not override.strip():
            raise KnowledgeConfigError("knowledge-config-empty")
        path = Path(override)
        if not path.is_absolute():
            raise KnowledgeConfigError("knowledge-config-not-absolute", str(path))
        if not path.is_file() or any(
            anchor.is_symlink() or getattr(anchor, "is_junction", lambda: False)()
            for anchor in (path, *path.parents)
        ):
            raise KnowledgeConfigError("knowledge-config-not-regular", str(path))
        return path
    return Path.home() / ".agents" / "knowledge-config.json"


def _read_config(config_path=None):
    try:
        config = json.loads(_config_path(config_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, RecursionError) as exc:
        raise KnowledgeConfigError("knowledge-config-invalid") from exc
    if (not isinstance(config, dict)
            or not isinstance(config.get("canonical_skill_path"), str)
            or not config["canonical_skill_path"].strip()):
        raise KnowledgeConfigError("knowledge-config-invalid-fields")
    return config


def compact(value, max_bytes=12000):
    context=value.get('context') or {}
    keys=('project_slug','project_resolution','total','stable_total','pending_total','has_more',
          'stable_has_more','pending_has_more','offset','limit','continuity_status','context_found',
          'stable_freshness','stable_age_freshness','requires_live_verification',
          'live_verification_reasons','pending_awareness_required','consumer_action','trust_boundary')
    result={k:context[k] for k in keys if k in context}
    result.update(status=value.get('status'),receipt_path=value.get('receipt_path'),
                  failure_code=value.get('failure_code'),coherent_read=value.get('coherent_read'),
                  health={k:(value.get('health') or {}).get(k) for k in
                          ('status','lint','dirty_tree_state','dirty_tree_write_blocked')})
    fields=('title','status','trust','summary','state_summary','source_ids','path','proposal_id',
            'handoff_integrity','integrity_issues','created_at','last_verified','verification_scope',
            'risk_flags','requires_live_verification','findings','conflicts','open_questions',
            'excerpts_truncated','excerpt_omitted')
    for group in ('stable_state','pending_state'):
        result[group]=[]
        for item in context.get(group,[]):
            record={k:item[k] for k in fields if k in item}
            for text in ('summary','state_summary'):
                if isinstance(record.get(text),str) and len(record[text])>800:
                    record[text]=record[text][:800]
                    record[text+'_truncated']=True
            result[group].append(record)
    result['notice']='Bounded source context, not authorization or proof of live state; truncated/missing evidence requires targeted follow-up.'
    result['compact_omitted']={'stable_state':0,'pending_state':0}
    while len(json.dumps(result,ensure_ascii=True).encode('utf-8')) > max_bytes:
        groups=[g for g in ('stable_state','pending_state') if len(result[g])>1]
        if not groups:
            raise ValueError('Context exceeds compact budget; request a narrower canonical project/query')
        group=max(groups,key=lambda g:len(json.dumps(result[g],ensure_ascii=True)))
        result[group].pop()
        result['compact_omitted'][group]+=1
        result['has_more']=True
    return result

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,help='Existing begin-task JSON; compact without another gateway call')
    p.add_argument('--project-slug')
    p.add_argument('--agent')
    p.add_argument('--session-id')
    p.add_argument('--output',type=Path,help='New task-local full response file for source retrieval')
    p.add_argument('--max-bytes',type=int,default=12000,help='Bounded displayed context; omitted sources remain in full response')
    p.add_argument('--config',type=Path,default=None,
                   help='Explicit knowledge config; overrides AGENT_KNOWLEDGE_CONFIG and the installed default')
    args=p.parse_args()
    if args.source:
        value=json.loads(args.source.read_text(encoding='utf-8-sig'))
    else:
        if not all((args.project_slug,args.agent,args.session_id,args.output)):
            p.error('Live read requires project, agent, native session ID and output')
        if args.output.exists(): p.error('Output exists; use --source or a new explicit checkpoint')
        config=_read_config(args.config)
        script=Path(config['canonical_skill_path'])/'scripts/knowledge.py'
        env = None
        if args.config is not None:
            env = dict(os.environ, AGENT_KNOWLEDGE_CONFIG=str(args.config.absolute()))
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        proc=subprocess.run([sys.executable,str(script),'begin-task','--agent',args.agent,
            '--session-id',args.session_id,'--project-slug',args.project_slug,'--require-context'],
            capture_output=True,encoding='utf-8',errors='replace',env=env,**options)
        value=json.loads(proc.stdout)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as stream: json.dump(value,stream,ensure_ascii=False,indent=2)
    result=compact(value,args.max_bytes)
    result['full_response_path']=str(args.source or args.output)
    print(json.dumps(result,ensure_ascii=True,separators=(',',':')))
    return 0 if result.get('status')=='COMPLETE' and result['health'].get('lint')=='PASS' else 3

if __name__=='__main__': raise SystemExit(main())
