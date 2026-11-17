#!/usr/bin/env python3
"""Preview or pull a prepared image manifest, with registry mapping and resumable checks."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def registry(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?", value):
        raise ValueError(f"Expected a registry host[:port], without scheme or credentials: {value!r}")
    if value != "localhost" and "." not in value and ":" not in value:
        raise ValueError(f"Registry must contain a dot/port, or be localhost: {value!r}")
    if ":" in value and not 1 <= int(value.rsplit(":", 1)[1]) <= 65535:
        raise ValueError(f"Invalid registry port: {value!r}")
    return value


def repository(value: str) -> str:
    component = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    if not re.fullmatch(component + r"(?:/" + component + r")*", value):
        raise ValueError(f"Invalid repository path: {value!r}")
    return value


def image_parts(image: str) -> tuple[str, str, str]:
    if not isinstance(image, str) or not image or any(c in image for c in " \t\r\n?#\\"):
        raise ValueError("Image references must be concrete names without URL parameters")
    name, at, digest = image.partition("@")
    suffix = ""
    if at:
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise ValueError(f"Invalid sha256 image digest: {image!r}")
        suffix = "@" + digest
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = name.rsplit(":", 1)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise ValueError(f"Invalid image tag: {image!r}")
        suffix = ":" + tag + suffix
    elif not at:
        raise ValueError(f"Image requires an explicit tag or sha256 digest: {image!r}")
    first, slash, rest = name.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        host, repo = registry(first), repository(rest)
    else:
        host, repo = "docker.io", repository(name)
        if "/" not in repo:
            repo = "library/" + repo
    return host, repo, suffix


def registry_maps(values: list[str]) -> dict[str, str]:
    mappings = {}
    for value in values:
        source, equal, target = value.partition("=")
        if not equal:
            raise ValueError("--registry-map must be SOURCE=DEST[/PREFIX]")
        registry(source)
        host, slash, prefix = target.partition("/")
        registry(host)
        if slash:
            repository(prefix)
        if source in mappings:
            raise ValueError(f"Duplicate registry mapping: {source}")
        mappings[source] = target
    return mappings


def build_plan(manifest: dict, mappings: dict[str, str]) -> list[dict]:
    if not isinstance(manifest, dict) or not isinstance(manifest.get("tasks"), list) or not manifest["tasks"]:
        raise ValueError("Manifest must contain a nonempty tasks list from prepare_images.py")
    task_images = {}
    for row in manifest["tasks"]:
        if not isinstance(row, dict) or not isinstance(row.get("task_id"), str) or not row["task_id"]:
            raise ValueError("Each manifest task needs task_id and image")
        image = row.get("image")
        image_parts(image)
        if row["task_id"] in task_images:
            raise ValueError(f"Duplicate task ID: {row['task_id']}")
        task_images[row["task_id"]] = image
    used = set()
    plan = []
    for image in sorted(set(task_images.values())):
        host, repo, suffix = image_parts(image)
        pull_ref = image
        if host in mappings:
            used.add(host)
            pull_ref = mappings[host] + "/" + repo + suffix
        if pull_ref != image and "@" in image:
            raise ValueError("Registry mapping of digest references cannot restore the canonical name via Docker tag; "
                             "use the canonical registry with a daemon proxy instead")
        plan.append({"image": image, "pull_image": pull_ref, "status": "pending", "attempts": []})
    unused = set(mappings) - used
    if unused:
        raise ValueError(f"Registry mapping matches no selected image: {sorted(unused)}")
    pull_names = [row["pull_image"] for row in plan]
    if len(set(pull_names)) != len(pull_names):
        raise ValueError("Multiple canonical images map to the same pull reference")
    canonical = set(task_images.values())
    if any(row["pull_image"] != row["image"] and row["pull_image"] in canonical for row in plan):
        raise ValueError("A mapped pull reference overlaps another selected canonical image")
    return plan


class DockerError(RuntimeError):
    pass


class Docker:
    def __init__(self, binary: str, context: str | None, runner=None):
        self.command = [binary] + (["--context", context] if context else [])
        self.context = context
        self.runner = runner or subprocess.run
        self.env = dict(os.environ)

    def run(self, args, **kwargs):
        return self.runner(self.command + args, env=self.env, **kwargs)

    def text(self, args, *, missing_ok=False):
        result = self.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        if result.returncode:
            error = result.stderr.strip()
            if missing_ok and re.search(r"no such (?:image|object)\s*:", error, re.I):
                return None
            raise DockerError(error or f"Docker exited {result.returncode}")
        return result.stdout.strip()

    def preflight(self):
        # Keep the existing DOCKER_HOST/DOCKER_CONTEXT behavior unless explicitly overridden.
        context = self.context or self.text(["context", "show"])
        daemon_id = self.text(["info", "--format", "{{.ID}}"])
        if not daemon_id:
            raise DockerError("Docker daemon returned an empty ID")
        return {"context": context, "daemon_id": daemon_id,
                "context_override": self.context is not None}

    def inspect(self, image):
        output = self.text(["image", "inspect", image], missing_ok=True)
        if output is None:
            return None
        rows = json.loads(output)
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or not rows[0].get("Id"):
            raise DockerError(f"Malformed Docker inspect result for {image}")
        info = rows[0]
        return {"image_id": info["Id"], "repo_digests": info.get("RepoDigests") or [],
                "os": info.get("Os"), "architecture": info.get("Architecture"),
                "variant": info.get("Variant", "")}

    def pull(self, image, platform, timeout, log):
        with log.open("w", encoding="utf-8") as output:
            result = self.run(["image", "pull", "--platform", platform, image],
                              stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
        if result.returncode:
            raise DockerError(f"Pull exited {result.returncode}; see {log}")

    def tag(self, image_id, target):
        # Use the inspected ID so a mutable mirror tag cannot change the source of retagging.
        self.text(["image", "tag", image_id, target])


def matches_platform(info, platform):
    parts = platform.split("/")
    return (info is not None and info["os"] == parts[0] and info["architecture"] == parts[1]
            and (len(parts) == 2 or info["variant"] == parts[2]))


def pull_one(plan, docker, args, logs, record):
    row = copy.deepcopy(plan)
    row["started_utc"] = utc_now()
    row["status"] = "running"
    record(row)
    try:
        local = docker.inspect(row["image"])
        if matches_platform(local, args.platform):
            row.update(status="skipped", local_image=local)
        else:
            if local is not None:
                row["previous_local_image"] = local
            source = None
            # A prior mirror pull can already be complete even if the process stopped before retagging.
            if row["pull_image"] != row["image"]:
                source = docker.inspect(row["pull_image"])
            if not matches_platform(source, args.platform):
                for attempt in range(1, args.retries + 2):
                    log = logs / (hashlib.sha256(row["image"].encode()).hexdigest()[:20] + f"-{attempt}.log")
                    detail = {"number": attempt, "started_utc": utc_now(), "log": str(log)}
                    started = time.monotonic()
                    try:
                        docker.pull(row["pull_image"], args.platform, args.timeout, log)
                        detail["status"] = "success"
                    except (DockerError, subprocess.TimeoutExpired) as exc:
                        detail.update(status="failed", error=str(exc))
                    detail["seconds"] = round(time.monotonic() - started, 3)
                    row["attempts"].append(detail)
                    record(row)
                    if detail["status"] == "success":
                        break
                    if attempt == args.retries + 1:
                        raise DockerError(detail["error"])
                    time.sleep(min(args.retry_delay * attempt, 30))
                source = docker.inspect(row["pull_image"])
            if not matches_platform(source, args.platform):
                raise DockerError(f"Pulled image is absent or does not match {args.platform}")
            row["source_image"] = source
            if row["pull_image"] != row["image"]:
                docker.tag(source["image_id"], row["image"])
            local = docker.inspect(row["image"])
            if not matches_platform(local, args.platform) or local["image_id"] != source["image_id"]:
                raise DockerError("Canonical image platform/ID does not match the inspected pull source")
            row.update(status="success", local_image=local)
    except (OSError, ValueError, DockerError, subprocess.SubprocessError) as exc:
        row.update(status="failed", error=str(exc))
    row["finished_utc"] = utc_now()
    record(row)
    return row


def write_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def main(argv=None, *, runner=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="JSON generated by prepare_images.py")
    parser.add_argument("--output", type=Path, required=True, help="Per-image progress/result JSON")
    parser.add_argument("--registry-map", action="append", default=[], metavar="SOURCE=DEST[/PREFIX]",
                        help="Replace a registry with a mirror host and optional path prefix; repeatable")
    parser.add_argument("--platform", default="linux/amd64")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--retries", type=int, default=2, help="Additional pull attempts per image (default: 2)")
    parser.add_argument("--retry-delay", type=float, default=2, help="Retry delay in seconds, capped at 30 per wait")
    parser.add_argument("--timeout", type=float, default=1800, help="Seconds per docker pull client attempt")
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--docker-context", help="Explicit context; otherwise honor the existing Docker environment")
    parser.add_argument("--execute", action="store_true", help="Pull/tag images; otherwise only write a plan")
    args = parser.parse_args(argv)
    try:
        if args.concurrency < 1 or args.retries < 0:
            raise ValueError("Concurrency must be positive and retries nonnegative")
        if (not math.isfinite(args.timeout) or args.timeout <= 0
                or not math.isfinite(args.retry_delay) or args.retry_delay < 0):
            raise ValueError("Timeout must be positive and retry delay nonnegative (both finite)")
        if not re.fullmatch(r"linux/[a-z0-9_]+(?:/[a-z0-9_]+)?", args.platform):
            raise ValueError("Use a Linux platform such as linux/amd64 or linux/arm64/v8")
        args.manifest = args.manifest.resolve(strict=True)
        args.output = args.output.resolve()
        if args.manifest == args.output:
            raise ValueError("Output must not overwrite the input manifest")
        raw = args.manifest.read_bytes()
        mappings = registry_maps(args.registry_map)
        plan = build_plan(json.loads(raw), mappings)
        report = {"schema_version": 1, "created_utc": utc_now(), "mode": "execute" if args.execute else "preview",
                  "manifest": str(args.manifest), "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                  "platform": args.platform, "registry_maps": mappings,
                  "mirror_digest_verification": "not performed; validate mirror mapping/content before use",
                  "images": copy.deepcopy(plan)}
        if args.output.exists():
            history = args.output.parent / (args.output.stem + ".history")
            history.mkdir(parents=True, exist_ok=True)
            previous = history / (uuid.uuid4().hex + ".json")
            previous.write_bytes(args.output.read_bytes())
            report["previous_report"] = str(previous)
        if not args.execute:
            write_report(args.output, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        docker = Docker(args.docker_bin, args.docker_context, runner)
        try:
            report["docker"] = docker.preflight()
        except (OSError, ValueError, DockerError, subprocess.SubprocessError) as exc:
            report["error"] = str(exc)
            write_report(args.output, report)
            raise
        logs = args.output.parent / (args.output.stem + ".logs") / uuid.uuid4().hex[:12]
        logs.mkdir(parents=True)
        report["logs"] = str(logs)
        write_report(args.output, report)
        lock = threading.Lock()
        indices = {row["image"]: index for index, row in enumerate(plan)}

        def record(row):
            with lock:
                report["images"][indices[row["image"]]] = copy.deepcopy(row)
                write_report(args.output, report)

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = [pool.submit(pull_one, row, docker, args, logs, record) for row in plan]
            for future in as_completed(futures):
                row = future.result()
                print(json.dumps({"image": row["image"], "status": row["status"]}), flush=True)
        counts = {status: sum(row["status"] == status for row in report["images"])
                  for status in ("success", "skipped", "failed")}
        report.update(finished_utc=utc_now(), summary=counts)
        write_report(args.output, report)
        print(json.dumps({"report": str(args.output), **counts}))
        return 2 if counts["failed"] else 0
    except (OSError, ValueError, DockerError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
