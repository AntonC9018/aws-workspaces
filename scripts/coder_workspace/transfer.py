#!/usr/bin/env python3
"""Validated local source snapshots and matching remote prepare/collect commands."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


def git(root, *args):
    return subprocess.run(["git", "-c", "pack.threads=1", "-c", "pack.windowMemory=64m",
                           "-C", str(root), *args], check=True, capture_output=True).stdout


def relative(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("Paths must be nonempty relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("..", ".git", ".aws", ".ssh", ".gnupg") for part in path.parts) or str(path) == ".":
        raise ValueError(f"Unsafe transfer path: {value}")
    return str(path)


def contained(root, value):
    root = Path(root).resolve()
    value = relative(value)
    path = root
    for part in PurePosixPath(value).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Transfer paths must not traverse symlinks: {path}")
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"Path escapes its root: {value}")
    return path


def selected_files(root, selections):
    files = {}
    for selection in selections:
        path = contained(root, selection)
        if not path.exists():
            raise ValueError(f"Selected path does not exist: {path}")
        candidates = sorted(path.rglob("*")) if path.is_dir() else [path]
        for child in candidates:
            rel = child.relative_to(root).as_posix()
            checked = contained(root, rel)
            if checked.is_file():
                files[rel] = checked
            elif not checked.is_dir():
                raise ValueError(f"Only regular files/directories can be transferred: {checked}")
    return files


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def extract_files(archive_path, destination):
    with tarfile.open(archive_path) as archive:
        for member in archive.getmembers():
            contained(destination, member.name)
            if not member.isfile():
                raise ValueError("Selected-file archives may contain only regular files")
        archive.extractall(destination, filter="data")


def inventory(path):
    """Resolve the manifest without AWS access or modifying source repositories."""
    path = Path(path).resolve()
    manifest = json.loads(path.read_text())
    if manifest.get("version") != 1 or not manifest.get("projects"):
        raise ValueError("Manifest requires version: 1 and at least one project")
    result = {"version": 1, "projects": [], "files": []}
    destinations = []
    names = set()
    for project in manifest["projects"]:
        name = project["name"]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,31}", name) or name in names:
            raise ValueError("Project names must be unique, 1–32 letters/digits/hyphens")
        names.add(name)
        root = (path.parent / project["path"]).resolve()
        if Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
            raise ValueError(f"Project path must be a Git repository root: {root}")
        if (root / ".gitmodules").exists() or git(root, "ls-files", "--stage").find(b"160000 ") >= 0:
            raise ValueError("Submodules must be declared as separate projects in this first manifest version")
        attributes = root / ".gitattributes"
        if attributes.exists() and "filter=lfs" in attributes.read_text():
            raise ValueError("Git LFS content needs explicit materialization; this first snapshot version does not support LFS")
        revision = project.get("revision", "HEAD")
        commit = git(root, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").decode().strip()
        destination = relative(project.get("destination", name))
        destinations.append(destination)
        untracked = selected_files(root, project.get("include_untracked", []))
        tracked = set(git(root, "ls-files", "-z").decode().split("\x00"))
        if set(untracked) & tracked:
            raise ValueError("include_untracked must contain only files outside Git's index")
        artifacts = [relative(p) for p in project.get("artifacts", [])]
        result["projects"].append({"name": name, "local_path": str(root), "destination": destination,
            "commit": commit, "artifacts": artifacts,
            "untracked": {rel: digest(source) for rel, source in untracked.items()},
            "untracked_modes": {rel: source.stat().st_mode & 0o777 for rel, source in untracked.items()},
            "patch_sha256": hashlib.sha256(git(root, "diff", "--binary", "--full-index", "HEAD", "--")).hexdigest()})
    for extra in manifest.get("files", []):
        source = (path.parent / extra["source"]).absolute()
        if not source.is_file() or source.is_symlink():
            raise ValueError("Extra files must be explicit regular files, not directories or symlinks")
        if any(part in (".git", ".aws", ".ssh", ".gnupg") for part in source.parts):
            raise ValueError("Credential stores and Git internals cannot be extra files")
        destination = relative(extra["destination"])
        destinations.append(destination)
        result["files"].append({"source": str(source), "destination": destination,
                                "sha256": digest(source), "mode": source.stat().st_mode & 0o777})
    for i, first in enumerate(destinations):
        for second in destinations[i + 1:]:
            if first == second or first.startswith(second + "/") or second.startswith(first + "/"):
                raise ValueError(f"Overlapping destinations: {first}, {second}")
    result["snapshot"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()[:24]
    return result


def pack(manifest_path, target):
    plan = inventory(manifest_path)
    target = Path(target)
    target.mkdir(parents=True, exist_ok=False)
    for project in plan["projects"]:
        root = Path(project["local_path"])
        stage = target / project["name"]
        stage.mkdir()
        # A temporary bare repository creates a named bundle ref without touching
        # refs, the index, or uncommitted work in the user's repository.
        with tempfile.TemporaryDirectory() as temp:
            git(temp, "init", "--bare", "--quiet")
            git(temp, "fetch", "--no-tags", str(root), project["commit"])
            git(temp, "update-ref", "refs/heads/snapshot", project["commit"])
            git(temp, "bundle", "create", str(stage / "source.bundle"), "refs/heads/snapshot")
        patch = git(root, "diff", "--binary", "--full-index", "HEAD", "--")
        (stage / "changes.patch").write_bytes(patch)
        if hashlib.sha256(patch).hexdigest() != project["patch_sha256"]:
            raise ValueError("Source changed while preparing snapshot; rerun sync")
        with tarfile.open(stage / "untracked.tar", "w") as archive:
            for rel, expected in project["untracked"].items():
                source = contained(root, rel)
                if digest(source) != expected:
                    raise ValueError("Untracked source changed while preparing snapshot")
                archive.add(source, arcname=rel, recursive=False)
    for extra in plan["files"]:
        target_file = contained(target / "extra", extra["destination"])
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(extra["source"], target_file)
        if digest(target_file) != extra["sha256"]:
            raise ValueError("Extra file changed while preparing snapshot")
    (target / "manifest.json").write_text(json.dumps(plan, indent=2))
    return plan


def prepare(stage):
    stage = Path(stage).resolve()
    plan = json.loads((stage / "manifest.json").read_text())
    snapshot = plan["snapshot"]
    if not re.fullmatch(r"[0-9a-f]{24}", snapshot):
        raise ValueError("Invalid snapshot ID")
    parent = Path.home() / "projects/snapshots"
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / snapshot
    if destination.exists():
        raise ValueError("Snapshot already exists; existing remote edits will not be overwritten")
    # Build outside the final location; promote only after all projects succeed.
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=parent) as temporary:
        temporary = Path(temporary)
        for project in plan["projects"]:
            root = contained(temporary, project["destination"])
            root.parent.mkdir(parents=True, exist_ok=True)
            git(temporary, "clone", "--quiet", "--branch", "snapshot", str(stage / project["name"] / "source.bundle"), str(root))
            if git(root, "rev-parse", "HEAD").decode().strip() != project["commit"]:
                raise ValueError("Bundle revision does not match snapshot")
            patch = stage / project["name"] / "changes.patch"
            if digest(patch) != project["patch_sha256"]:
                raise ValueError("Patch hash mismatch")
            if patch.stat().st_size:
                git(root, "apply", "--binary", str(patch))
            extract_files(stage / project["name"] / "untracked.tar", root)
            for rel, expected in project["untracked"].items():
                contained(root, rel).chmod(project["untracked_modes"][rel])
                if digest(contained(root, rel)) != expected or contained(root, rel).stat().st_mode & 0o777 != project["untracked_modes"][rel]:
                    raise ValueError("Untracked file hash mismatch")
            git(root, "remote", "remove", "origin")
        for extra in plan["files"]:
            target = contained(temporary, extra["destination"])
            target.parent.mkdir(parents=True, exist_ok=True)
            source = contained(stage / "extra", extra["destination"])
            if digest(source) != extra["sha256"]:
                raise ValueError("Extra file hash mismatch")
            shutil.copy2(source, target)
        (temporary / "manifest.json").write_text(json.dumps(plan, indent=2))
        os.rename(temporary, destination)
    return {p["name"]: str(destination / p["destination"]) for p in plan["projects"]}


def collect(snapshot):
    if not re.fullmatch(r"[0-9a-f]{24}", snapshot):
        raise ValueError("Invalid snapshot ID")
    root = Path.home() / "projects/snapshots" / snapshot
    plan = json.loads((root / "manifest.json").read_text())
    cache = Path.home() / ".cache/coder-transfers"
    cache.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="result-", dir=cache))
    for project in plan["projects"]:
        source = contained(root, project["destination"])
        output = stage / project["name"]
        output.mkdir()
        git(source, "bundle", "create", str(output / "commits.bundle"), "HEAD", "--branches")
        (output / "changes.patch").write_bytes(git(source, "diff", "--binary", "--full-index", "HEAD", "--"))
        project["result_commit"] = git(source, "rev-parse", "HEAD").decode().strip()
        new_files = [p for p in git(source, "ls-files", "--others", "--exclude-standard", "-z").decode().split("\x00") if p]
        with tarfile.open(output / "untracked.tar", "w") as archive:
            for rel, path in selected_files(source, new_files).items():
                archive.add(path, arcname=rel, recursive=False)
        project["missing_artifacts"] = [p for p in project["artifacts"] if not contained(source, p).exists()]
        artifacts = selected_files(source, [p for p in project["artifacts"] if p not in project["missing_artifacts"]])
        for rel, path in artifacts.items():
            target = contained(output / "artifacts", rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    plan["missing_files"] = []
    for extra in plan["files"]:
        source = contained(root, extra["destination"])
        if not source.exists():
            plan["missing_files"].append(extra["destination"])
            continue
        if not source.is_file():
            raise ValueError("An extra input file became a non-file")
        target = contained(stage / "extra", extra["destination"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    plan["result_hashes"] = {p.relative_to(stage).as_posix(): digest(p) for p in stage.rglob("*") if p.is_file()}
    (stage / "manifest.json").write_text(json.dumps(plan, indent=2))
    return str(stage)


def restore(stage):
    stage = Path(stage)
    plan = json.loads((stage / "manifest.json").read_text())
    for rel, expected in plan["result_hashes"].items():
        if digest(contained(stage, rel)) != expected:
            raise ValueError("Retrieved result hash mismatch")
    checkouts = stage / "checkouts"
    checkouts.mkdir()
    for project in plan["projects"]:
        source = stage / project["name"]
        target = checkouts / project["name"]
        git(checkouts, "clone", "--quiet", str(source / "commits.bundle"), str(target))
        git(target, "checkout", "--quiet", "--detach", project["result_commit"])
        patch = source / "changes.patch"
        if patch.stat().st_size:
            git(target, "apply", "--binary", str(patch))
        extract_files(source / "untracked.tar", target)
        git(target, "remote", "remove", "origin")
    return plan


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "collect", "cleanup"])
    parser.add_argument("path")
    args = parser.parse_args()
    if args.action == "cleanup":
        path = Path(args.path).resolve()
        base = Path.home() / ".cache/coder-transfers"
        if path.parent != base.resolve() or not re.fullmatch(r"(?:input|result)-[A-Za-z0-9_]+", path.name):
            raise ValueError("Refusing to clean a path outside transfer staging")
        shutil.rmtree(path)
        return
    print(json.dumps(prepare(args.path) if args.action == "prepare" else collect(args.path)))


if __name__ == "__main__":
    main()
