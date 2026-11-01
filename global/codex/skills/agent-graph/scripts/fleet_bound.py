"""Use Fleet's existing reuse API in a pre-bound detached Graph worktree.

The normal Fleet CLI still handles routing, readiness, quota and provider calls.
This adapter only supplies its existing preserved-workspace parameters.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
sys.dont_write_bytecode = True


def validate_worktree(path, base):
    path = Path(path).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("Exact base commit SHA required")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    def git(*argv):
        return subprocess.run(["git", *argv], cwd=path, capture_output=True, text=True, timeout=15, **options)
    root = git("rev-parse", "--show-toplevel")
    if root.returncode or Path(root.stdout.strip()).resolve() != path or not (path / ".git").is_file():
        raise ValueError("Bound implementation requires a linked worktree root, never primary checkout")
    if git("symbolic-ref", "-q", "HEAD").returncode == 0:
        raise ValueError("Bound implementation requires detached HEAD")
    if git("merge-base", "--is-ancestor", base, "HEAD").returncode:
        raise ValueError("Bound worktree does not descend from the declared base")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fleet", required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not os.environ.get("AGENT_GRAPH_ATTEMPT_ID"):
        raise ValueError("This adapter is launched by Graph, not a global Fleet override")
    workspace = validate_worktree(Path.cwd(), args.base_commit)
    path = Path(args.fleet).resolve()
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("fleet", path)
    fleet = importlib.util.module_from_spec(spec)
    sys.modules["fleet"] = fleet
    spec.loader.exec_module(fleet)
    original = fleet.execute_single
    def execute(task, **kwargs):
        if kwargs.get("mode") != "implement" or not kwargs.get("allow_write"):
            raise fleet.FleetError("Bound-worktree adapter requires explicit implement/allow-write")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        def common(directory):
            result = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                    cwd=directory, capture_output=True, text=True, timeout=15, **options)
            if result.returncode:
                raise fleet.FleetError("Cannot resolve common Git repository")
            return Path(result.stdout.strip()).resolve()
        if common(kwargs["workspace"]) != common(workspace):
            raise fleet.FleetError("Bound worktree belongs to another repository")
        return original(task, **{**kwargs, "existing_execution_workspace": workspace,
                                 "preserved_base_commit_sha": args.base_commit,
                                 "preserved_base_provenance": "preserved"})
    fleet.execute_single = execute
    argv = args.args[1:] if args.args[:1] == ["--"] else args.args
    if "run" not in argv or any(action in argv for action in ("council", "quality-gate", "security-gate")):
        raise ValueError("Only the ordinary Fleet run command is supported")
    return fleet.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
