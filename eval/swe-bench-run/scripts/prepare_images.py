#!/usr/bin/env python3
"""Create exact SWE-bench Pro V2 image manifests; optionally inspect local Docker."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path


def load_ids(tasks_root: Path, ids_file: Path) -> list[str]:
    root = tasks_root.resolve(strict=True)
    ids: list[str] = []
    for line in ids_file.read_text(encoding="utf-8-sig").splitlines():
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", item) or item in {".", ".."}:
            raise ValueError(f"Expected exact task directory ID, got {item!r}")
        if item in ids:
            raise ValueError(f"Duplicate task ID: {item}")
        task = (root / item).resolve(strict=True)
        if task.parent != root or not (task / "task.toml").is_file():
            raise ValueError(f"Task must be a direct child with task.toml: {item}")
        ids.append(item)
    if not ids:
        raise ValueError("Empty task selection; refusing a possible full-dataset run")
    return ids


def image_for(task: Path) -> str:
    with (task / "task.toml").open("rb") as stream:
        config = tomllib.load(stream)
    configured = config.get("environment", {}).get("docker_image")
    dockerfile = task / "environment" / "Dockerfile"
    bases: list[str] = []
    if dockerfile.exists():
        for line in dockerfile.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", line, re.I)
            if match:
                bases.append(match.group(1))
    if configured:
        image = str(configured)
        if len(bases) == 1 and "$" not in bases[0] and bases[0] != image:
            raise ValueError(f"{task.name}: docker_image conflicts with Dockerfile FROM")
    elif len(bases) == 1:
        image = bases[0]
    else:
        raise ValueError(f"{task.name}: cannot resolve one image; inspect build stages/ARGs")
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]*", image)
            or "$" in image or image.lower() == "scratch"):
        raise ValueError(f"{task.name}: image must be a concrete reference: {image!r}")
    last_component = image.rsplit("/", 1)[-1]
    if "@sha256:" not in image and ":" not in last_component:
        raise ValueError(f"{task.name}: unpinned image reference: {image}")
    return image


def selection_hash(ids: list[str]) -> str:
    return hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest()


def inspect_image(image: str, expected_arch: str, docker: str) -> dict:
    result = subprocess.run([docker, "image", "inspect", image], capture_output=True,
                            text=True, encoding="utf-8", timeout=60)
    if result.returncode:
        raise RuntimeError(f"Docker inspect failed for {image}: {result.stderr.strip()}")
    info = json.loads(result.stdout)[0]
    architecture = info.get("Architecture")
    os_name = info.get("Os")
    return {"image_id": info.get("Id"), "repo_digests": info.get("RepoDigests", []),
            "architecture": architecture, "os": os_name,
            "status": "present" if architecture == expected_arch and os_name == "linux"
            else "platform-mismatch"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-root", type=Path, required=True)
    parser.add_argument("--ids", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--images-out", type=Path)
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--expected-arch", default="amd64")
    parser.add_argument("--docker-bin", default="docker")
    args = parser.parse_args()
    try:
        ids = load_ids(args.tasks_root, args.ids)
        rows = [{"task_id": item, "image": image_for(args.tasks_root / item)} for item in ids]
        unique_images = sorted({row["image"] for row in rows})
        local: dict[str, dict] = {}
        if args.inspect:
            context = subprocess.run([args.docker_bin, "context", "show"], capture_output=True,
                                     text=True, timeout=30, check=True).stdout.strip()
            # A daemon/auth failure is not automatically classified as image absence.
            for image in unique_images:
                try:
                    local[image] = inspect_image(image, args.expected_arch, args.docker_bin)
                except RuntimeError as exc:
                    local[image] = {"status": "inspect-error", "detail": str(exc)}
        else:
            context = None
        manifest = {"created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "tasks_root": str(args.tasks_root.resolve()), "ids_file": str(args.ids.resolve()),
                    "ids_file_sha256": hashlib.sha256(args.ids.read_bytes()).hexdigest(),
                    "selection_sha256": selection_hash(ids), "task_count": len(ids),
                    "unique_image_count": len(unique_images), "tasks": rows,
                    "docker_context": context, "local_images": local}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.images_out:
            args.images_out.parent.mkdir(parents=True, exist_ok=True)
            args.images_out.write_text("\n".join(unique_images) + "\n", encoding="utf-8")
        bad = sum(value["status"] != "present" for value in local.values())
        print(json.dumps({"task_count": len(ids), "unique_images": len(unique_images),
                          "manifest": str(args.output), "local_image_problems": bad}))
        return 2 if bad else 0
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
