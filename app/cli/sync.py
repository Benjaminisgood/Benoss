from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path, PurePosixPath
from typing import Iterable
from urllib.parse import urljoin

import requests


CONFIG_DIRNAME = ".benoss"
CONFIG_FILENAME = "config.json"
CONFIG_VERSION = 1


DEFAULT_EXCLUDES = [
    ".benoss/**",
    ".git/**",
    ".DS_Store",
    "__pycache__/**",
    "*.pyc",
    ".venv/**",
    "node_modules/**",
]


def _die(message: str, code: int = 2) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _posix_relpath(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    return PurePosixPath(rel.as_posix()).as_posix()


def _safe_join(root: Path, rel_posix: str) -> Path:
    rel_posix = str(rel_posix or "").replace("\\", "/").lstrip("/")
    if not rel_posix:
        raise ValueError("empty path")
    parts = PurePosixPath(rel_posix).parts
    if any(p in {"", ".", ".."} for p in parts):
        raise ValueError("invalid path")
    target = (root / Path(*parts)).resolve()
    root_resolved = root.resolve()
    if root_resolved not in target.parents and target != root_resolved:
        raise ValueError("path escapes root")
    return target


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _normalize_base_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    return value.rstrip("/") + "/"


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        raise ValueError(f"invalid json: {path}") from exc


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", "utf-8")


def _load_repo_config(repo_dir: Path) -> dict:
    cfg_path = repo_dir / CONFIG_DIRNAME / CONFIG_FILENAME
    cfg = _read_json(cfg_path)
    if not cfg:
        return {}
    if int(cfg.get("version") or 0) != CONFIG_VERSION:
        raise ValueError(f"unsupported config version: {cfg.get('version')}")
    return cfg


def _save_repo_config(repo_dir: Path, base_url: str, project_id: int) -> Path:
    cfg_path = repo_dir / CONFIG_DIRNAME / CONFIG_FILENAME
    _write_json(
        cfg_path,
        {
            "version": CONFIG_VERSION,
            "base_url": base_url,
            "project_id": int(project_id),
        },
    )
    return cfg_path


def _load_ignore_patterns(repo_dir: Path) -> list[str]:
    patterns = list(DEFAULT_EXCLUDES)
    ignore_file = repo_dir / ".benossignore"
    try:
        for line in ignore_file.read_text("utf-8").splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            patterns.append(raw)
    except FileNotFoundError:
        pass
    return patterns


def _matches_any(path_posix: str, patterns: list[str]) -> bool:
    for pat in patterns:
        if fnmatch.fnmatch(path_posix, pat):
            return True
        # Convenience: treat "dir/" as "dir/**"
        if pat.endswith("/") and fnmatch.fnmatch(path_posix, pat + "**"):
            return True
    return False


@dataclass(frozen=True)
class RemoteFile:
    path: str
    url: str
    sha256: str
    size_bytes: int


class BenossClient:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: int = 60):
        self.base_url = _normalize_base_url(base_url)
        if not self.base_url:
            raise ValueError("missing base url")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()

    def login(self) -> None:
        login_url = urljoin(self.base_url, "login")
        resp = self.session.post(
            login_url,
            data={"username": self.username, "password": self.password, "remember": "on"},
            allow_redirects=False,
            timeout=self.timeout,
        )
        if resp.status_code in (302, 303):
            return
        if resp.status_code == 200:
            raise RuntimeError("login failed (check username/password)")
        raise RuntimeError(f"login failed (http {resp.status_code})")

    def _json(self, method: str, path: str, **kwargs):
        url = urljoin(self.base_url, path.lstrip("/"))
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if resp.status_code in (401, 403):
            raise RuntimeError("not authorized (login required / forbidden)")
        if not resp.ok:
            message = f"request failed: {resp.status_code}"
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("error"):
                    message = str(data.get("error"))
            except Exception:
                pass
            raise RuntimeError(message)
        return resp.json()

    def get_project(self, project_id: int) -> tuple[dict, dict[str, RemoteFile]]:
        data = self._json("GET", f"/api/projects/{int(project_id)}")
        project = data.get("project") or {}
        files = data.get("files") or []
        remote = {}
        for item in files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            url = str(item.get("url") or "").strip()
            if not path or not url:
                continue
            remote[path] = RemoteFile(
                path=path,
                url=url,
                sha256=str(item.get("sha256") or ""),
                size_bytes=int(item.get("size_bytes") or 0),
            )
        return project, remote

    def upload_file(self, project_id: int, rel_path: str, file_path: Path) -> None:
        url = urljoin(self.base_url, f"api/projects/{int(project_id)}/files/upload")
        with file_path.open("rb") as f:
            resp = self.session.post(url, data={"path": rel_path}, files={"file": f}, timeout=None)
        if resp.status_code in (401, 403):
            raise RuntimeError("upload forbidden (not owner?)")
        if not resp.ok:
            message = f"upload failed: {resp.status_code}"
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("error"):
                    message = str(data.get("error"))
            except Exception:
                pass
            raise RuntimeError(message)

    def delete_remote(self, project_id: int, rel_path: str) -> None:
        self._json(
            "DELETE",
            f"/api/projects/{int(project_id)}/files",
            json={"path": rel_path},
            headers={"Content-Type": "application/json"},
        )

    def create_push_request(self, project_id: int, message: str = "") -> int:
        data = self._json(
            "POST",
            f"/api/projects/{int(project_id)}/push-requests",
            json={"message": message or ""},
            headers={"Content-Type": "application/json"},
        )
        pr = (data or {}).get("push_request") or {}
        pr_id = int(pr.get("id") or 0)
        if not pr_id:
            raise RuntimeError("failed to create push request")
        return pr_id

    def upload_push_request_file(self, push_request_id: int, rel_path: str, file_path: Path) -> None:
        url = urljoin(self.base_url, f"api/push-requests/{int(push_request_id)}/files/upload")
        with file_path.open("rb") as f:
            resp = self.session.post(url, data={"path": rel_path}, files={"file": f}, timeout=None)
        if resp.status_code in (401, 403):
            raise RuntimeError("push request upload forbidden")
        if not resp.ok:
            message = f"push request upload failed: {resp.status_code}"
            try:
                data = resp.json()
                if isinstance(data, dict) and data.get("error"):
                    message = str(data.get("error"))
            except Exception:
                pass
            raise RuntimeError(message)


def _iter_local_files(repo_dir: Path, patterns: list[str]) -> Iterable[tuple[str, Path]]:
    for root, dirnames, filenames in os.walk(repo_dir):
        root_path = Path(root)
        # prune ignored directories early
        kept = []
        for d in dirnames:
            full = root_path / d
            if full.is_symlink():
                continue
            rel = _posix_relpath(full, repo_dir)
            if _matches_any(rel + "/**", patterns):
                continue
            kept.append(d)
        dirnames[:] = kept

        for name in filenames:
            full = root_path / name
            if full.is_symlink():
                continue
            rel = _posix_relpath(full, repo_dir)
            if _matches_any(rel, patterns):
                continue
            yield rel, full


def _plan_pull(
    *,
    dest_dir: Path,
    remote: dict[str, RemoteFile],
    patterns: list[str],
    prune: bool,
) -> tuple[list[RemoteFile], list[Path]]:
    to_download: list[RemoteFile] = []
    for rpath, rf in remote.items():
        if _matches_any(rpath, patterns):
            continue
        try:
            local_path = _safe_join(dest_dir, rpath)
        except ValueError:
            continue
        if local_path.is_file():
            if rf.sha256:
                if _sha256_file(local_path) == rf.sha256:
                    continue
            else:
                if local_path.stat().st_size == rf.size_bytes:
                    continue
        to_download.append(rf)

    to_delete: list[Path] = []
    if prune:
        remote_paths = set(remote.keys())
        for rel, full in _iter_local_files(dest_dir, patterns):
            if rel not in remote_paths:
                to_delete.append(full)
    return to_download, to_delete


def _plan_push(
    *,
    src_dir: Path,
    remote: dict[str, RemoteFile],
    patterns: list[str],
    prune: bool,
) -> tuple[list[tuple[str, Path]], list[str]]:
    to_upload: list[tuple[str, Path]] = []
    local_paths: set[str] = set()
    for rel, full in _iter_local_files(src_dir, patterns):
        local_paths.add(rel)
        rf = remote.get(rel)
        if rf and rf.sha256:
            if _sha256_file(full) == rf.sha256:
                continue
        elif rf and rf.size_bytes:
            if full.stat().st_size == rf.size_bytes:
                continue
        to_upload.append((rel, full))

    to_delete: list[str] = []
    if prune:
        for rpath in remote.keys():
            if rpath not in local_paths and not _matches_any(rpath, patterns):
                to_delete.append(rpath)
    return to_upload, to_delete


def _download_file(url: str, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=None) as resp:
        if not resp.ok:
            raise RuntimeError(f"download failed: {resp.status_code}")
        with tempfile.NamedTemporaryFile(delete=False, dir=str(dest_path.parent), prefix=".benoss.", suffix=".tmp") as tmp:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    tmp.write(chunk)
            tmp_path = Path(tmp.name)
    os.replace(tmp_path, dest_path)


def _cmd_init(args) -> None:
    repo_dir = Path(args.dir).resolve()
    repo_dir.mkdir(parents=True, exist_ok=True)
    base_url = _normalize_base_url(args.base_url or "")
    if not base_url:
        _die("missing --base-url")
    if not args.project:
        _die("missing --project")
    cfg_path = _save_repo_config(repo_dir, base_url, int(args.project))
    print(str(cfg_path))


def _get_base_and_project(args) -> tuple[str, int, Path]:
    repo_dir = Path(args.dir).resolve()
    cfg = _load_repo_config(repo_dir)
    base_url = _normalize_base_url(args.base_url or cfg.get("base_url") or os.environ.get("BENOSS_BASE_URL") or "")
    project_id = int(args.project or cfg.get("project_id") or 0)
    if not base_url:
        _die("missing base url (use --base-url or benoss-sync init)")
    if not project_id:
        _die("missing project id (use --project or benoss-sync init)")
    return base_url, project_id, repo_dir


def _get_creds(args) -> tuple[str, str]:
    username = (args.username or os.environ.get("BENOSS_USERNAME") or "").strip()
    password = args.password or os.environ.get("BENOSS_PASSWORD") or ""
    if not username:
        username = input("Username: ").strip()
    if not password:
        password = getpass("Password: ")
    if not username or not password:
        _die("missing credentials")
    return username, password


def _cmd_status(args) -> None:
    base_url, project_id, repo_dir = _get_base_and_project(args)
    username, password = _get_creds(args)
    patterns = _load_ignore_patterns(repo_dir)

    client = BenossClient(base_url, username, password)
    client.login()
    project, remote = client.get_project(project_id)

    to_download, to_delete_local = _plan_pull(dest_dir=repo_dir, remote=remote, patterns=patterns, prune=args.prune)
    to_upload, to_delete_remote = _plan_push(src_dir=repo_dir, remote=remote, patterns=patterns, prune=args.prune)

    title = project.get("title") or f"Project #{project_id}"
    owner = (project.get("owner") or {}).get("username") or ""
    print(f"{title}  @{'%s' % owner if owner else ''}".strip())
    print(f"Remote files: {len(remote)}")
    print(f"To download:  {len(to_download)}")
    print(f"To upload:    {len(to_upload)}")
    if args.prune:
        print(f"To delete local:  {len(to_delete_local)}")
        print(f"To delete remote: {len(to_delete_remote)}")


def _cmd_pull(args) -> None:
    base_url, project_id, repo_dir = _get_base_and_project(args)
    username, password = _get_creds(args)
    patterns = _load_ignore_patterns(repo_dir)

    client = BenossClient(base_url, username, password)
    client.login()
    _, remote = client.get_project(project_id)

    to_download, to_delete_local = _plan_pull(dest_dir=repo_dir, remote=remote, patterns=patterns, prune=args.prune)
    print(f"pull: {len(to_download)} download(s){' + prune' if args.prune else ''}")
    for rf in to_download:
        dest_path = _safe_join(repo_dir, rf.path)
        if args.dry_run:
            print(f"DRY  get  {rf.path}")
            continue
        print(f"get  {rf.path}")
        _download_file(rf.url, dest_path)

    if args.prune:
        for full in to_delete_local:
            rel = _posix_relpath(full, repo_dir)
            if args.dry_run:
                print(f"DRY  rm   {rel}")
                continue
            print(f"rm   {rel}")
            try:
                full.unlink()
            except IsADirectoryError:
                # Shouldn't happen (we only iterate files), but keep it safe.
                pass


def _cmd_push(args) -> None:
    base_url, project_id, repo_dir = _get_base_and_project(args)
    username, password = _get_creds(args)
    patterns = _load_ignore_patterns(repo_dir)

    client = BenossClient(base_url, username, password)
    client.login()
    project, remote = client.get_project(project_id)
    if not (project.get("can_edit") is True):
        _die("cannot push: you are not the project owner")

    to_upload, to_delete_remote = _plan_push(src_dir=repo_dir, remote=remote, patterns=patterns, prune=args.prune)
    print(f"push: {len(to_upload)} upload(s){' + prune' if args.prune else ''}")
    for rel, full in to_upload:
        if args.dry_run:
            print(f"DRY  put  {rel}")
            continue
        print(f"put  {rel}")
        client.upload_file(project_id, rel, full)

    if args.prune:
        for rel in to_delete_remote:
            if args.dry_run:
                print(f"DRY  del  {rel}")
                continue
            print(f"del  {rel}")
            client.delete_remote(project_id, rel)


def _cmd_propose(args) -> None:
    base_url, project_id, repo_dir = _get_base_and_project(args)
    username, password = _get_creds(args)
    patterns = _load_ignore_patterns(repo_dir)

    client = BenossClient(base_url, username, password)
    client.login()
    project, remote = client.get_project(project_id)
    if project.get("can_edit") is True:
        _die("you are the project owner; use benoss-sync push")

    to_upload, _ = _plan_push(src_dir=repo_dir, remote=remote, patterns=patterns, prune=False)
    if not to_upload:
        print("propose: no changes to upload")
        return

    pr_id = client.create_push_request(project_id, message=args.message or "")
    print(f"push-request #{pr_id}: {len(to_upload)} file(s)")
    for rel, full in to_upload:
        if args.dry_run:
            print(f"DRY  put  {rel}")
            continue
        print(f"put  {rel}")
        client.upload_push_request_file(pr_id, rel, full)
    print("done (owner can approve in Control Room)")


def _cmd_clone(args) -> None:
    dest = Path(args.dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        _die(f"dest not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)

    base_url = _normalize_base_url(args.base_url or os.environ.get("BENOSS_BASE_URL") or "")
    if not base_url:
        _die("missing --base-url")
    if not args.project:
        _die("missing --project")
    project_id = int(args.project)

    cfg_path = _save_repo_config(dest, base_url, project_id)
    print(f"init {cfg_path}")

    # pull into the folder
    pull_args = argparse.Namespace(
        base_url=base_url,
        project=project_id,
        dir=str(dest),
        username=args.username,
        password=args.password,
        prune=False,
        dry_run=False,
    )
    _cmd_pull(pull_args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="benoss-sync",
        description="Incremental sync between a Benoss project (OSS) and a local folder.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--base-url", default="", help="Benoss site base URL, e.g. http://127.0.0.1:5002")
    common.add_argument("--project", type=int, default=0, help="Project id")
    common.add_argument("--dir", default=".", help="Local folder (repo root)")
    common.add_argument("--username", default="", help="Login username (or BENOSS_USERNAME)")
    common.add_argument("--password", default="", help="Login password (or BENOSS_PASSWORD)")

    init = sub.add_parser("init", parents=[common], help="Write .benoss/config.json in --dir")
    init.set_defaults(func=_cmd_init)

    status = sub.add_parser("status", parents=[common], help="Show what would be pulled/pushed")
    status.add_argument("--prune", action="store_true", help="Also show deletes for mirror mode")
    status.set_defaults(func=_cmd_status)

    pull = sub.add_parser("pull", parents=[common], help="Download changed files from remote into --dir")
    pull.add_argument("--prune", action="store_true", help="Delete local files not present on remote")
    pull.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    pull.set_defaults(func=_cmd_pull)

    push = sub.add_parser("push", parents=[common], help="Upload changed files from --dir to remote")
    push.add_argument("--prune", action="store_true", help="Delete remote files not present locally")
    push.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    push.set_defaults(func=_cmd_push)

    propose = sub.add_parser("propose", parents=[common], help="Create a push request (PR-like) to someone else's project")
    propose.add_argument("--message", default="", help="Message for the project owner")
    propose.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    propose.set_defaults(func=_cmd_propose)

    clone = sub.add_parser("clone", parents=[common], help="Clone a project into a new local folder")
    clone.add_argument("--dest", required=True, help="Destination folder")
    clone.set_defaults(func=_cmd_clone)

    return p


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        _die(str(exc), code=1)


if __name__ == "__main__":
    main()
