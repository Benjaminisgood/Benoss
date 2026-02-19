from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from getpass import getpass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


CONFIG_DIR = ".benoss"
CONFIG_FILE = "config.json"
CONFIG_VERSION = 3


def _die(message: str, code: int = 2) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _normalize_base_url(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    return text.rstrip("/") + "/"


def _config_path(cwd: Path) -> Path:
    return cwd / CONFIG_DIR / CONFIG_FILE


def _read_config(cwd: Path) -> dict[str, Any]:
    path = _config_path(cwd)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except Exception as exc:
        raise ValueError(f"invalid config: {path}") from exc

    if int(data.get("version") or 0) != CONFIG_VERSION:
        raise ValueError(f"unsupported config version: {data.get('version')}")
    return data


def _write_config(cwd: Path, payload: dict[str, Any]) -> Path:
    path = _config_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": CONFIG_VERSION,
        "base_url": _normalize_base_url(payload.get("base_url") or ""),
        "default_tag": str(payload.get("default_tag") or "").strip(),
    }
    path.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", "utf-8")
    return path


@dataclass
class RuntimeConfig:
    base_url: str
    username: str
    password: str
    default_tag: str


class BenossClient:
    def __init__(self, base_url: str, username: str, password: str, *, timeout: int = 120):
        self.base_url = _normalize_base_url(base_url)
        if not self.base_url:
            raise ValueError("missing base_url")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session = requests.Session()

    def login(self) -> None:
        url = urljoin(self.base_url, "login")
        response = self.session.post(
            url,
            data={
                "username": self.username,
                "password": self.password,
                "remember": "on",
            },
            allow_redirects=False,
            timeout=self.timeout,
        )
        if response.status_code in (302, 303):
            return
        if response.status_code == 200:
            raise RuntimeError("login failed: invalid credentials")
        raise RuntimeError(f"login failed: http {response.status_code}")

    def _json(self, method: str, path: str, **kwargs):
        url = urljoin(self.base_url, path.lstrip("/"))
        response = self.session.request(method, url, timeout=self.timeout, **kwargs)
        if response.status_code in (401, 403):
            raise RuntimeError("not authorized")
        if not response.ok:
            message = f"request failed: {response.status_code}"
            try:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("error"):
                    message = str(payload.get("error"))
            except Exception:
                pass
            raise RuntimeError(message)
        return response.json()

    def pull_records(
        self,
        *,
        tag: str = "",
        day: str = "",
        per: int = 500,
        content_source: str = "summary",
    ) -> list[dict[str, Any]]:
        source = str(content_source or "summary").strip().lower()
        if source not in {"summary", "full"}:
            source = "summary"
        params = {
            "include_content": 1,
            "per": int(per),
            "content_source": source,
        }
        if tag:
            params["tag"] = tag
        if day:
            params["day"] = day

        payload = self._json("GET", "/api/pull", params=params)
        return payload.get("items") or []

    def push_record(
        self,
        *,
        text: str = "",
        file_path: Path | None = None,
        visibility: str = "private",
        tags: str = "",
    ) -> dict[str, Any]:
        url = urljoin(self.base_url, "api/push")

        form_data: dict[str, Any] = {
            "visibility": visibility,
        }
        if text:
            form_data["text"] = text
        if tags:
            form_data["tags"] = tags

        if file_path is None:
            response = self.session.post(url, data=form_data, timeout=None)
        else:
            with file_path.open("rb") as stream:
                files = {"file": (file_path.name, stream)}
                response = self.session.post(url, data=form_data, files=files, timeout=None)

        if response.status_code in (401, 403):
            raise RuntimeError("push forbidden")
        if not response.ok:
            message = f"push failed: {response.status_code}"
            try:
                payload = response.json()
                if isinstance(payload, dict) and payload.get("error"):
                    message = str(payload.get("error"))
            except Exception:
                pass
            raise RuntimeError(message)
        return response.json()

    def download(self, url_or_path: str, target: Path) -> None:
        raw = str(url_or_path or "").strip()
        if not raw:
            raise RuntimeError("missing download url")
        if raw.startswith("http://") or raw.startswith("https://"):
            url = raw
        else:
            url = urljoin(self.base_url, raw.lstrip("/"))

        response = self.session.get(url, stream=True, timeout=None)
        if not response.ok:
            raise RuntimeError(f"download failed: {response.status_code}")

        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with tmp.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
        tmp.replace(target)


def _load_runtime(args: argparse.Namespace, cwd: Path) -> RuntimeConfig:
    cfg = _read_config(cwd)

    base_url = _normalize_base_url(args.base_url or cfg.get("base_url") or "")
    if not base_url:
        _die("missing base_url (pass --base-url or run init)")

    username = (args.username or "").strip() or input("username: ").strip()
    if not username:
        _die("missing username")

    password = args.password or getpass("password: ")
    if not password:
        _die("missing password")

    default_tag = str(args.tag or cfg.get("default_tag") or "").strip()

    return RuntimeConfig(
        base_url=base_url,
        username=username,
        password=password,
        default_tag=default_tag,
    )


def cmd_init(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    base_url = _normalize_base_url(args.base_url)
    if not base_url:
        _die("missing --base-url")

    path = _write_config(
        cwd,
        {
            "base_url": base_url,
            "default_tag": (args.default_tag or "").strip(),
        },
    )
    print(f"saved: {path}")
    print(f"base_url: {base_url}")
    if args.default_tag:
        print(f"default_tag: {args.default_tag}")
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    output_dir = Path(args.output).resolve()

    runtime = _load_runtime(args, cwd)
    client = BenossClient(runtime.base_url, runtime.username, runtime.password)
    client.login()

    items = client.pull_records(
        tag=runtime.default_tag,
        day=(args.day or ""),
        per=args.per,
        content_source=args.content_source,
    )
    if not items:
        print("no records")
        return 0

    for item in items:
        record_id = int(item.get("id") or 0)
        if not record_id:
            continue

        record_dir = output_dir / f"record_{record_id}"
        record_dir.mkdir(parents=True, exist_ok=True)

        (record_dir / "record.json").write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", "utf-8")

        content = item.get("content") or {}
        kind = content.get("kind")
        if kind == "text":
            (record_dir / "content.txt").write_text(str(content.get("text") or ""), "utf-8")
        elif kind == "file":
            filename = str(content.get("filename") or "file.bin")
            safe_name = filename.replace("/", "_").replace("\\", "_")
            source = content.get("blob_url") or content.get("signed_url")
            if source:
                client.download(str(source), record_dir / safe_name)

    print(f"pulled records: {len(items)}")
    print(f"output: {output_dir}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    runtime = _load_runtime(args, cwd)
    client = BenossClient(runtime.base_url, runtime.username, runtime.password)
    client.login()

    items = client.pull_records(tag=runtime.default_tag, day=(args.day or ""), per=args.per)
    public_count = sum(1 for item in items if item.get("visibility") == "public")
    private_count = len(items) - public_count

    print(f"records: {len(items)}")
    print(f"public: {public_count}")
    print(f"private: {private_count}")
    if runtime.default_tag:
        print(f"tag filter: {runtime.default_tag}")
    if args.day:
        print(f"day filter: {args.day}")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).resolve()
    runtime = _load_runtime(args, cwd)

    text = (args.text or "").strip()
    file_path = Path(args.file).resolve() if args.file else None

    if not text and file_path is None:
        _die("push requires --text or --file")
    if file_path is not None and not file_path.exists():
        _die(f"file not found: {file_path}")

    visibility = (args.visibility or "private").strip().lower()
    if visibility not in {"public", "private"}:
        _die("invalid --visibility, choose public/private")

    tags = (args.tags or runtime.default_tag or "").strip()

    client = BenossClient(runtime.base_url, runtime.username, runtime.password)
    client.login()
    payload = client.push_record(
        text=text,
        file_path=file_path,
        visibility=visibility,
        tags=tags,
    )

    record = payload.get("record") or {}
    print(f"pushed record: {record.get('id')}")
    print(f"visibility: {record.get('visibility')}")
    if tags:
        print(f"tags: {tags}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benoss-sync",
        description="Benoss record sync helper (tag-driven push/pull).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cwd", default=".", help="workspace directory (default: .)")
    common.add_argument("--base-url", help="server base url")
    common.add_argument("--username", help="login username")
    common.add_argument("--password", help="login password")
    common.add_argument("--tag", help="single tag filter (default uses config default_tag)")

    init_parser = subparsers.add_parser("init", help="write .benoss/config.json")
    init_parser.add_argument("--cwd", default=".")
    init_parser.add_argument("--base-url", required=True)
    init_parser.add_argument("--default-tag", default="", help="optional default tag scope")
    init_parser.set_defaults(func=cmd_init)

    status_parser = subparsers.add_parser("status", parents=[common], help="show remote record counts")
    status_parser.add_argument("--day", default="", help="YYYY-MM-DD")
    status_parser.add_argument("--per", type=int, default=500)
    status_parser.set_defaults(func=cmd_status)

    pull_parser = subparsers.add_parser("pull", parents=[common], help="pull records")
    pull_parser.add_argument("--day", default="", help="YYYY-MM-DD")
    pull_parser.add_argument("--per", type=int, default=500)
    pull_parser.add_argument(
        "--content-source",
        default="summary",
        choices=["summary", "full"],
        help="text content mode: summary (DB overview) or full (full text from object storage)",
    )
    pull_parser.add_argument("--output", default="./pulled_records", help="output directory")
    pull_parser.set_defaults(func=cmd_pull)

    push_parser = subparsers.add_parser("push", parents=[common], help="push one record")
    push_parser.add_argument("--text", help="text content")
    push_parser.add_argument("--file", help="file path to upload")
    push_parser.add_argument("--visibility", default="private", choices=["public", "private"])
    push_parser.add_argument("--tags", default="", help="comma-separated tags")
    push_parser.set_defaults(func=cmd_push)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        _die(str(exc), code=1)


if __name__ == "__main__":
    raise SystemExit(main())
