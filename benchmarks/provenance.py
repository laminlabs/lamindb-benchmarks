"""Capture immutable Git references for a benchmark and its editable packages."""
from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit


def _git(directory: Path, *args: str, optional: bool = False) -> str:
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        if optional:
            return ""
        raise RuntimeError(f"Cannot read Git {args[0]} in {directory}")
    return result.stdout.rstrip("\n")


def repository_url(remote: str) -> str:
    """Convert SSH/HTTPS origins to browser URLs without embedded credentials."""
    scp = re.fullmatch(r"[^/@:]+@([^/:]+):(.+)", remote)
    if scp:
        host, path = scp.groups()
    else:
        parsed = urlsplit(remote)
        if parsed.scheme not in {"https", "http", "ssh"} or not parsed.hostname:
            raise ValueError("Git origin must be an SSH or HTTP(S) repository URL")
        host, path = parsed.hostname, parsed.path.lstrip("/")
    path = path.rstrip("/").removesuffix(".git")
    if not path:
        raise ValueError("Git origin has no repository path")
    return f"https://{host}/{path}"


def git_revision(directory: Path, *, paths: list[str] | None = None) -> dict:
    root = Path(_git(directory, "rev-parse", "--show-toplevel"))
    commit = _git(root, "rev-parse", "HEAD")
    remote = _git(root, "remote", "get-url", "origin")
    repo = repository_url(remote)
    status_args = ["status", "--porcelain", "--untracked-files=all"]
    if paths:
        status_args.extend(["--", *paths])
    dirty = _git(root, *status_args)
    return {
        "repository": repo,
        "commit": commit,
        "branch": _git(root, "symbolic-ref", "--short", "HEAD", optional=True) or None,
        "url": f"{repo}/tree/{commit}",
        "dirty": bool(dirty),
        "changes": dirty.splitlines(),
    }


def editable_dependencies() -> dict:
    """The lockfile's editable paths alone do not identify their source revisions."""
    packages = {}
    for name in ("lamindb-core", "lamindb-setup", "lamin-cli", "lamin-utils", "bionty", "pertdb"):
        distribution = importlib.metadata.distribution(name)
        source = json.loads(distribution.read_text("direct_url.json") or "{}")
        item = {"version": distribution.version}
        if source.get("dir_info", {}).get("editable"):
            parsed = urlsplit(source["url"])
            if parsed.scheme != "file":
                raise ValueError(f"Editable package {name} has no local source path")
            item.update(git_revision(Path(unquote(parsed.path))))
        packages[name] = item
    return packages


def benchmark_provenance(root: Path, experiment: Path, config: Path) -> dict:
    """Link to HEAD and record local changes without requiring a commit or push."""
    root = root.resolve()
    try:
        experiment_path = experiment.resolve().relative_to(root).as_posix()
        config_path = config.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError("The experiment and config must be inside the Git repository") from error
    for required in ("benchmarks/run_benchmark.py", f"{experiment_path}/setup.py",
                     f"{experiment_path}/query.py", config_path, "pyproject.toml", "uv.lock"):
        if not (root / required).is_file():
            raise ValueError(f"Required benchmark file is missing: {required}")
    revision = git_revision(root)
    published = _git(root, "for-each-ref", "--format=%(refname)",
                     "--contains", revision["commit"], "refs/remotes/origin")
    base = revision["repository"]
    blob = f"{base}/blob/{revision['commit']}"
    dependencies = editable_dependencies()
    revision.update({
        "published": bool(published),
        "experiment_path": experiment_path,
        "experiment_url": f"{revision['url']}/{quote(experiment_path, safe='/')}",
        "config_path": config_path,
        "config_url": f"{blob}/{quote(config_path, safe='/')}",
        "runner_url": f"{blob}/benchmarks/run_benchmark.py",
        "editable_dependencies": dependencies,
        "editable_dependencies_clean": all(not item.get("dirty", False) for item in dependencies.values()),
    })
    return revision


def attach_git_provenance(run, provenance: dict) -> None:
    """Run.reference pins the whole checkout; the transform links to its script."""
    run.reference = provenance["url"]
    run.reference_type = "url"
    run.extra_data = {**(run.extra_data or {}), "git": provenance}
    run.save(update_fields=["reference", "reference_type", "extra_data"])
    run.transform.reference = provenance["runner_url"]
    run.transform.reference_type = "url"
    run.transform.save(update_fields=["reference", "reference_type"])
