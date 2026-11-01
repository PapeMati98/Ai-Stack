"""Select existing canonical proposals for an explicitly enabled owner curator.

Selection is metadata-only. Curating never alters another agent's proposal or
source; exact content review and publication remain in curate_once.py.
"""
import json,re
from pathlib import Path
from .publication import fm,_ids,safe_file,PublicationError


def proposal_identities(vault: Path) -> list[tuple[str, str]]:
    """Snapshot every existing proposal path and readable ID for an epoch baseline."""
    vault = Path(vault)
    identities = []
    for path in sorted((vault / 'proposals').glob('*/*.md')):
        name = path.relative_to(vault).as_posix()
        try:
            text = safe_file(vault, name).read_text(encoding='utf-8-sig').replace('\r\n', '\n')
            ident = fm(text, 'id')
        except (OSError, UnicodeError, PublicationError):
            ident = None
        identities.append((name, ident if isinstance(ident, str) else ''))
    return identities


def _eligible(vault: Path):
    vault=Path(vault)
    accounted=set()
    for path in (vault/'governance/curations').glob('*.json'):
        row=json.loads(safe_file(vault,path.relative_to(vault).as_posix()).read_text(encoding='utf-8'))
        for item in row.get('proposals',[]):
            if item.get('disposition') in {'synthesized','historical','unresolved','orphan'}:
                accounted.add(item.get('proposal_id'))
    eligible=[]
    for path in (vault/'proposals').glob('*/*.md'):
        name=path.relative_to(vault).as_posix();text=safe_file(vault,name).read_text(encoding='utf-8-sig').replace('\r\n','\n')
        try:
            pid=fm(text,'id')
            if pid in accounted or fm(text,'classification') not in {'internal','public'}:continue
            targets=_ids(text,'target_pages')
            if not isinstance(pid,str) or not re.fullmatch(r'prop-[A-Za-z0-9._-]+',pid):continue
            eligible.append((pid,name,targets,fm(text,'project_slug')))
        except PublicationError:
            # An unknown metadata format is never implicitly admitted.
            continue
    return sorted(eligible)

def _matches(row,project):
    target='wiki/projects/'+project+'.md'
    return row[3] in {None,'',project} and (target in row[2] or target.removeprefix('wiki/').removesuffix('.md') in row[2])

def select_pending(vault: Path, project_slug: str, limit: int=4) -> list[str]:
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}',project_slug) or not 1<=limit<=8:
        raise PublicationError('invalid_backlog_scope')
    return [row[1] for row in _eligible(vault) if _matches(row,project_slug)][:limit]

def pending_projects(vault: Path, known_projects: list[str]) -> list[str]:
    rows=_eligible(vault)
    return [p for p in known_projects if re.fullmatch(r'[a-z0-9][a-z0-9-]{0,100}',p) and any(_matches(row,p) for row in rows)]
