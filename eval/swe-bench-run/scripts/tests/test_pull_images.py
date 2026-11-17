"""Run with python -m unittest discover -s scripts/tests -v (no Docker/network needed)."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pull_images as pull


def info(image_id="sha256:abc", arch="amd64", os_name="linux"):
    return {"Id": image_id, "Architecture": arch, "Os": os_name,
            "RepoDigests": ["mirror.invalid/team/image@sha256:" + "a" * 64]}


class FakeDocker:
    def __init__(self):
        self.images = {}
        self.calls = []
        self.failures = {}
        self.arches = {}
        self.inspect_error = None
        self.daemon_error = False
        self.corrupt_tag = False
        self.active = self.max_active = 0
        self.barrier = None
        self.lock = threading.Lock()

    def __call__(self, argv, **kwargs):
        args = argv[1:]
        if args[:1] == ["--context"]:
            args = args[2:]
        with self.lock:
            self.calls.append(args)
        result = lambda code=0, out="", err="": subprocess.CompletedProcess(argv, code, out, err)
        if args == ["context", "show"]:
            return result(out="default\n")
        if args[:1] == ["info"]:
            return result(1, err="Cannot connect to Docker daemon") if self.daemon_error else result(out="daemon-test\n")
        if args[:2] == ["image", "inspect"]:
            if self.inspect_error:
                return result(1, err=self.inspect_error)
            with self.lock:
                value = self.images.get(args[2])
            return result(out=json.dumps([value])) if value else result(1, err="Error: No such image: " + args[2])
        if args[:2] == ["image", "pull"]:
            image = args[-1]
            with self.lock:
                self.active += 1
                self.max_active = max(self.active, self.max_active)
                remaining = self.failures.get(image, 0)
                self.failures[image] = max(0, remaining - 1)
            try:
                if self.barrier:
                    self.barrier.wait(timeout=5)
                else:
                    time.sleep(0.002)
                kwargs["stdout"].write("fake pull log\n")
                if remaining:
                    if remaining == 99:
                        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                    return result(1)
                with self.lock:
                    self.images[image] = info("sha256:" + image.encode().hex(), self.arches.get(image, "amd64"))
                return result()
            finally:
                with self.lock:
                    self.active -= 1
        if args[:2] == ["image", "tag"]:
            image_id, target = args[2:]
            with self.lock:
                value = next(value for value in self.images.values() if value["Id"] == image_id)
                self.images[target] = dict(value) if not self.corrupt_tag else info("sha256:wrong")
            return result()
        raise AssertionError(args)

    def pulls(self):
        return [call[-1] for call in self.calls if call[:2] == ["image", "pull"]]


class ImagePullTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "images.json"
        self.output = self.root / "result.json"
        self.docker = FakeDocker()

    def write_manifest(self, *images):
        self.manifest.write_text(json.dumps({"tasks": [{"task_id": f"task-{i}", "image": image}
                                                       for i, image in enumerate(images)]}), encoding="utf-8")

    def run_cli(self, *extra):
        argv = ["--manifest", str(self.manifest), "--output", str(self.output),
                "--retry-delay", "0", *extra]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = pull.main(argv, runner=self.docker)
        report = json.loads(self.output.read_text(encoding="utf-8")) if self.output.exists() else None
        return code, report

    def test_preview_maps_without_invoking_docker_and_deduplicates(self):
        self.write_manifest("ghcr.io/team/image:one", "ghcr.io/team/image:one")
        code, report = self.run_cli("--registry-map", "ghcr.io=mirror.invalid/ghcr.io")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(len(report["images"]), 1)
        self.assertEqual(report["images"][0]["pull_image"], "mirror.invalid/ghcr.io/team/image:one")

    def test_mapping_registry_port_and_dockerhub_normalization(self):
        mappings = pull.registry_maps(["docker.io=localhost:5000/cache", "ghcr.io=mirror.invalid"])
        plan = pull.build_plan({"tasks": [{"task_id": "x", "image": "ubuntu:24.04"}]},
                               {"docker.io": mappings["docker.io"]})
        self.assertEqual(plan[0]["image"], "ubuntu:24.04")
        self.assertEqual(plan[0]["pull_image"], "localhost:5000/cache/library/ubuntu:24.04")

    def test_invalid_and_unused_mappings_fail_before_docker(self):
        self.write_manifest("ghcr.io/team/image:one")
        for mapping in ("https://ghcr.io=mirror.invalid", "ghcr.io=https://mirror.invalid", "ghcr.io=mirror.invalid/../x",
                        "ghcr.io=mirror.invalid:99999", "docker.io=mirror.invalid"):
            with self.subTest(mapping=mapping):
                code, _ = self.run_cli("--registry-map", mapping, "--execute")
                self.assertEqual(code, 1)
        self.assertEqual(self.docker.calls, [])

    def test_digest_canonical_supported_but_mirror_rewrite_rejected(self):
        image = "ghcr.io/team/image@sha256:" + "a" * 64
        self.write_manifest(image)
        code, report = self.run_cli()
        self.assertEqual((code, report["images"][0]["pull_image"]), (0, image))
        code, _ = self.run_cli("--registry-map", "ghcr.io=mirror.invalid", "--execute")
        self.assertEqual(code, 1)
        self.assertEqual(self.docker.calls, [])

    def test_existing_correct_platform_skips_pull(self):
        image = "ghcr.io/team/image:one"
        self.write_manifest(image)
        self.docker.images[image] = info()
        code, report = self.run_cli("--execute")
        self.assertEqual(code, 0)
        self.assertEqual(report["summary"]["skipped"], 1)
        self.assertEqual(self.docker.pulls(), [])

    def test_wrong_platform_is_replaced(self):
        image = "ghcr.io/team/image:one"
        self.write_manifest(image)
        self.docker.images[image] = info(arch="arm64")
        code, report = self.run_cli("--execute")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.pulls(), [image])
        self.assertEqual(report["images"][0]["local_image"]["architecture"], "amd64")

    def test_mirror_pull_retags_by_inspected_id(self):
        canonical = "ghcr.io/team/image:one"
        mirror = "mirror.invalid/ghcr.io/team/image:one"
        self.write_manifest(canonical)
        code, report = self.run_cli("--registry-map", "ghcr.io=mirror.invalid/ghcr.io", "--execute")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.pulls(), [mirror])
        tag = next(call for call in self.docker.calls if call[:2] == ["image", "tag"])
        self.assertEqual(tag[2:], [self.docker.images[mirror]["Id"], canonical])
        self.assertEqual(report["images"][0]["source_image"]["image_id"], report["images"][0]["local_image"]["image_id"])

    def test_completed_mirror_pull_recovers_without_redownload(self):
        canonical = "ghcr.io/team/image:one"
        mirror = "mirror.invalid/team/image:one"
        self.write_manifest(canonical)
        self.docker.images[mirror] = info()
        code, report = self.run_cli("--registry-map", "ghcr.io=mirror.invalid", "--execute")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.pulls(), [])
        self.assertEqual(report["images"][0]["status"], "success")
        self.assertIn(canonical, self.docker.images)

    def test_retry_succeeds_with_per_attempt_logs(self):
        image = "ghcr.io/team/image:one"
        self.write_manifest(image)
        self.docker.failures[image] = 1
        code, report = self.run_cli("--retries", "1", "--execute")
        self.assertEqual(code, 0)
        attempts = report["images"][0]["attempts"]
        self.assertEqual([a["status"] for a in attempts], ["failed", "success"])
        self.assertTrue(all(Path(a["log"]).read_text(encoding="utf-8") == "fake pull log\n" for a in attempts))

    def test_exhaustion_keeps_other_successes_and_rerun_only_pulls_missing(self):
        good, bad = "ghcr.io/team/good:one", "ghcr.io/team/bad:one"
        self.write_manifest(good, bad)
        self.docker.failures[bad] = 5
        code, first = self.run_cli("--retries", "1", "--execute")
        self.assertEqual(code, 2)
        self.assertEqual(first["summary"], {"success": 1, "skipped": 0, "failed": 1})
        self.assertEqual(self.docker.pulls().count(bad), 2)
        self.docker.failures[bad] = 0
        self.docker.calls.clear()
        code, second = self.run_cli("--retries", "1", "--execute")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.pulls(), [bad])
        self.assertEqual(second["summary"], {"success": 1, "skipped": 1, "failed": 0})
        self.assertEqual(json.loads(Path(second["previous_report"]).read_text(encoding="utf-8")), first)

    def test_pull_timeout_is_bounded_and_reported(self):
        image = "ghcr.io/team/image:one"
        self.write_manifest(image)
        self.docker.failures[image] = 99
        code, report = self.run_cli("--retries", "0", "--timeout", "1", "--execute")
        self.assertEqual(code, 2)
        self.assertEqual(len(self.docker.pulls()), 1)
        self.assertIn("timed out", report["images"][0]["error"])

    def test_old_success_report_does_not_skip_a_now_missing_image(self):
        image = "ghcr.io/team/image:one"
        self.write_manifest(image)
        self.assertEqual(self.run_cli("--execute")[0], 0)
        self.docker.images.clear()
        self.docker.calls.clear()
        code, report = self.run_cli("--execute")
        self.assertEqual(code, 0)
        self.assertEqual(self.docker.pulls(), [image])
        self.assertEqual(report["summary"]["skipped"], 0)

    def test_progress_is_on_disk_before_pull_completes(self):
        self.write_manifest("ghcr.io/team/image:one")
        original_runner = self.docker
        observed = []

        def observe(argv, **kwargs):
            if "pull" in argv:
                current = json.loads(self.output.read_text(encoding="utf-8"))
                observed.append(current["images"][0]["status"])
            return original_runner(argv, **kwargs)

        self.docker = observe
        code, _ = self.run_cli("--execute")
        self.assertEqual(code, 0)
        self.assertEqual(observed, ["running"])

    def test_daemon_failure_does_not_become_missing_image(self):
        self.write_manifest("ghcr.io/team/image:one")
        self.docker.daemon_error = True
        code, report = self.run_cli("--execute")
        self.assertEqual(code, 1)
        self.assertIn("daemon", report["error"])
        self.assertEqual(self.docker.pulls(), [])

    def test_inspect_permission_error_does_not_trigger_pull(self):
        self.write_manifest("ghcr.io/team/image:one")
        self.docker.inspect_error = "permission denied connecting to socket"
        code, report = self.run_cli("--execute")
        self.assertEqual(code, 2)
        self.assertEqual(self.docker.pulls(), [])
        self.assertIn("permission denied", report["images"][0]["error"])

    def test_wrong_download_platform_does_not_retag(self):
        canonical = "ghcr.io/team/image:one"
        mirror = "mirror.invalid/team/image:one"
        self.write_manifest(canonical)
        self.docker.arches[mirror] = "arm64"
        code, report = self.run_cli("--registry-map", "ghcr.io=mirror.invalid", "--execute")
        self.assertEqual(code, 2)
        self.assertNotIn(canonical, self.docker.images)
        self.assertEqual(report["summary"]["failed"], 1)

    def test_retag_id_mismatch_is_failure(self):
        self.write_manifest("ghcr.io/team/image:one")
        self.docker.corrupt_tag = True
        code, report = self.run_cli("--registry-map", "ghcr.io=mirror.invalid", "--execute")
        self.assertEqual(code, 2)
        self.assertIn("platform/ID", report["images"][0]["error"])

    def test_concurrency_limit_is_applied(self):
        self.write_manifest(*(f"ghcr.io/team/image:{i}" for i in range(4)))
        self.docker.barrier = threading.Barrier(2)
        code, report = self.run_cli("--concurrency", "2", "--execute")
        self.assertEqual(code, 0)
        self.assertEqual(report["summary"]["success"], 4)
        self.assertEqual(self.docker.max_active, 2)

    def test_input_cannot_be_overwritten(self):
        self.write_manifest("ghcr.io/team/image:one")
        original = self.manifest.read_bytes()
        code, _ = self.run_cli("--output", str(self.manifest))
        self.assertEqual(code, 1)
        self.assertEqual(original, self.manifest.read_bytes())

    def test_invalid_inputs_and_collisions_fail_before_docker(self):
        cases = [
            ({"tasks": []}, []),
            ({"tasks": [{"task_id": "x", "image": "ghcr.io/team/image"}]}, []),
            ({"tasks": [{"task_id": "x", "image": "ghcr.io/team/image:one"}] * 2}, []),
            ({"tasks": [{"task_id": "a", "image": "ghcr.io/team/image:one"},
                        {"task_id": "b", "image": "mirror.invalid/team/image:one"}]},
             ["--registry-map", "ghcr.io=mirror.invalid"]),
        ]
        for manifest, args in cases:
            self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
            with self.subTest(manifest=manifest):
                self.assertEqual(self.run_cli(*args, "--execute")[0], 1)
        self.assertEqual(self.docker.calls, [])

    def test_real_cli_reads_prepare_images_output_without_docker(self):
        tasks = self.root / "tasks"
        for name in ("task_a", "task_b"):
            directory = tasks / name
            directory.mkdir(parents=True)
            (directory / "task.toml").write_text('[environment]\ndocker_image="ghcr.io/team/image:one"\n', encoding="utf-8")
        ids = self.root / "ids.txt"
        ids.write_text("task_a\ntask_b\n", encoding="utf-8")
        scripts = Path(pull.__file__).parent
        generated = subprocess.run([sys.executable, str(scripts / "prepare_images.py"), "--tasks-root", str(tasks),
                                    "--ids", str(ids), "--output", str(self.manifest)], capture_output=True, text=True)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        result = subprocess.run([sys.executable, str(scripts / "pull_images.py"), "--manifest", str(self.manifest),
                                 "--output", str(self.output), "--docker-bin", "nonexistent-docker-for-preview",
                                 "--registry-map", "ghcr.io=mirror.invalid/ghcr.io"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)["images"]), 1)


if __name__ == "__main__":
    unittest.main()
