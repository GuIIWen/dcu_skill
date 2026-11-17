#!/usr/bin/env python3
"""Preview or execute an exact Harbor task subset without shell interpolation."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from prepare_images import load_ids, selection_hash


def build_command(args: argparse.Namespace) -> tuple[list[str], dict]:
    repo = args.repo.resolve(strict=True)
    tasks = repo / "v2" / "tasks"
    ids = load_ids(tasks, args.ids)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.job):
        raise ValueError("Use a simple, unique job name")
    if args.concurrency < 1:
        raise ValueError("Concurrency must be positive")
    output = args.output.resolve()
    if (output / args.job).exists():
        raise ValueError("Job directory already exists; choose a new name or use Harbor's resume flow")
    command = [args.harbor_bin, "run", "-p", str(tasks)]
    for item in ids:
        command += ["-i", item]
    command += ["-e", args.environment, "-n", str(args.concurrency), "-a", args.agent]
    if args.model:
        command += ["-m", args.model]
    elif args.agent not in {"oracle", "nop"}:
        raise ValueError("A model is required for this agent")
    if args.base_url:
        url = urlsplit(args.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("Base URL must be http(s), without credentials/query/fragment")
        command += ["--base-url", args.base_url]
    for host in args.allow_agent_host:
        if not host or re.search(r"[\s/]", host):
            raise ValueError("--allow-agent-host takes a host/IP, not a URL")
        command += ["--allow-agent-host", host]
    for item in args.ak:
        key, sep, value = item.partition("=")
        if not sep or not key or not value:
            raise ValueError("--ak must be key=value")
        if re.search(r"api[_-]?key|authorization|password|secret|access[_-]?token|auth[_-]?token", key, re.I):
            raise ValueError("Pass credentials via the selected integration's environment, not --ak")
        command += ["--ak", item]
    if args.mounts_json:
        mounts = json.loads(args.mounts_json)
        if not isinstance(mounts, list):
            raise ValueError("--mounts-json must contain a JSON list")
        for mount in mounts:
            if (not isinstance(mount, dict) or mount.get("type") != "bind"
                    or mount.get("read_only") is not True
                    or not isinstance(mount.get("source"), str)
                    or not isinstance(mount.get("target"), str)):
                raise ValueError("Helper supports read-only bind mounts with source and target")
        command += ["--mounts", json.dumps(mounts, separators=(",", ":"))]
    command += ["-o", str(output), "--job-name", args.job]
    summary = {"task_count": len(ids), "selection_sha256": selection_hash(ids),
               "job_directory": str(output / args.job), "cwd": str(repo),
               "argv": command, "execute": args.execute}
    return command, summary


def verify_flags(command: list[str], harbor: str, env: dict) -> None:
    result = subprocess.run([harbor, "run", "--help"], capture_output=True, text=True,
                            encoding="utf-8", timeout=45, env=env, check=True)
    help_text = re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)
    needed = set()
    index = 2
    while index < len(command):
        needed.add(command[index])
        index += 2
    missing = sorted(flag for flag in needed if not re.search(
        r"(?<![\w-])" + re.escape(flag) + r"(?![\w-])", help_text))
    if missing:
        raise ValueError(f"Installed Harbor does not advertise flags {missing}; adapt to its help")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--ids", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--agent", required=True)
    parser.add_argument("--model")
    parser.add_argument("--environment", default="docker")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--base-url")
    parser.add_argument("--allow-agent-host", action="append", default=[])
    parser.add_argument("--ak", action="append", default=[])
    parser.add_argument("--mounts-json")
    parser.add_argument("--harbor-bin", default="harbor")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        command, summary = build_command(args)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print("Shell display (POSIX quoting):", shlex.join(command))
        if not args.execute:
            return 0
        env = dict(os.environ)
        tooling = str(args.repo.resolve() / "v2" / "tooling")
        env["PYTHONPATH"] = tooling + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        verify_flags(command, args.harbor_bin, env)
        args.output.resolve().mkdir(parents=True, exist_ok=True)
        return subprocess.run(command, cwd=args.repo.resolve(), env=env).returncode
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
