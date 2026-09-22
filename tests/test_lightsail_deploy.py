"""Exercise production deployment orchestration without SSH, Docker, or Git mutations."""

import gzip
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
PREVIOUS = "a" * 40
REVISION = "b" * 40
SERVICES = ["backend", "frontend", "ingest", "py"]
URLS = [
    "http://127.0.0.1:3000/",
    "http://127.0.0.1:3001/api/health",
    "http://127.0.0.1:3003/health",
    "http://127.0.0.1:3004/openapi.json",
    "https://ml-ops.dev.theseusrobotics.ch/",
    "https://api.ml-ops.dev.theseusrobotics.ch/api/health",
    "https://ingest.ml-ops.dev.theseusrobotics.ch/health",
    "https://py.ml-ops.dev.theseusrobotics.ch/openapi.json",
]

# Each fake executable records its argument boundaries. Unknown operations fail,
# so production network/container actions cannot accidentally run in a test.
MOCK = r'''#!/bin/bash
set -u
cmd=${0##*/}
{ printf '%s\0' "$cmd" "$@"; printf '\n'; } >> "$TEST_ROOT/calls"
case "$cmd" in
  git)
    case "$1 $2" in
      'status --porcelain')
        [[ $CASE == status_error ]] && exit 128
        [[ $CASE == dirty ]] && printf ' M source.py\n';;
      'submodule foreach')
        [[ $CASE == submodule_status_error ]] && exit 128
        [[ $CASE == dirty_submodule ]] && printf ' M child.py\n';;
      'fetch --no-tags') [[ $CASE == fetch_failure ]] && exit 1;;
      'rev-parse HEAD') cat "$TEST_ROOT/head";;
      'rev-parse FETCH_HEAD')
        [[ $CASE == revision_error ]] && exit 128
        if [[ $CASE == stale ]]; then printf '%s\n' "$PREVIOUS"; else printf '%s\n' "$REVISION"; fi;;
      'show '*)
        [[ $CASE == show_failure ]] && exit 1
        printf '%s\n' '#!/bin/bash' 'printf "%s\n" "$1" > "$TEST_ROOT/executed"';;
      'checkout --detach')
        [[ $CASE == source_recovery_failure && $3 == "$PREVIOUS" ]] && exit 1
        printf '%s\n' "$3" > "$TEST_ROOT/head";;
      'submodule sync') :;;
      'submodule update')
        [[ $CASE == submodule_failure && $(cat "$TEST_ROOT/head") == "$REVISION" ]] && exit 1;;
      *) exit 96;;
    esac;;
  sudo)
    [[ $1 == -n && $2 == docker ]] || exit 97
    shift 2
    case "$1" in
      compose)
        phase=$(cat "$TEST_ROOT/up_count")
        for ((i=2;i<=$#;i++)); do
          case "${!i}" in
            ps)
              [[ $CASE == missing_service && ${!#} == ingest ]] && exit 0
              printf 'container-%s\n' "${!#}"; exit 0;;
            config) [[ $CASE == config_failure ]] && exit 1; exit 0;;
            build)
              [[ $CASE == build_failure && ${!#} == "$FAIL_SERVICE" ]] && exit 1
              exit 0;;
            up)
              printf '%s\n' "$((phase+1))" > "$TEST_ROOT/up_count"
              [[ $CASE == rollback_up_failure ]] && exit 1
              [[ $CASE == source_recovery_failure || $CASE == up_failure && $phase == 0 ]] && exit 1
              if [[ $CASE == termination && $phase == 0 ]]; then kill -TERM "$PPID"; fi
              exit 0;;
          esac
        done
        exit 96;;
      inspect) printf 'sha256:old-%s\n' "${!#}";;
      image)
        if [[ $2 == ls ]]; then printf 'old-image\n';
        elif [[ $2 != tag && $2 != rm ]]; then exit 96; fi;;
      exec)
        [[ $CASE == dump_failure ]] && exit 1
        printf '%s\n' 'postgres backup mock';;
      *) exit 96;;
    esac;;
  df)
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted\n'
    if [[ $CASE == disk_low ]]; then printf 'disk 16000000 12000000 4000000 75%% /\n'; else printf 'disk 16000000 2000000 14000000 12%% /\n'; fi;;
  curl)
    phase=$(cat "$TEST_ROOT/up_count")
    if [[ $CASE == health_failure && $phase == 1 && ${!#} == "$FAIL_URL" ]]; then exit 22; fi
    if [[ $CASE == rollback_health_failure ]]; then
      [[ $phase == 1 && ${!#} == http://127.0.0.1:3000/ ]] && exit 22
      [[ $phase == 2 && ${!#} == "$FAIL_URL" ]] && exit 22
    fi;;
  flock) [[ $CASE == lock_failure ]] && exit 1;;
  sleep) :;;
  *) exit 96;;
esac
exit 0
'''


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mlop-deploy-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.server = self.repo / "server"
        self.state = self.root / "state"
        self.state.mkdir()
        for service in ["web", "ingest", "py"]:
            (self.server / service).mkdir(parents=True)
        self.env_marker = self.root / "env-was-sourced"
        (self.server / ".env").write_text(
            f'NEVER_OUTPUT_SECRET=SECRET_TEST_VALUE\nUNSAFE=$(touch "{self.env_marker}")\n'
        )
        (self.server / "docker-compose.yml").write_text("services: {}\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for command in ["git", "sudo", "df", "curl", "flock", "sleep"]:
            executable = self.bin / command
            executable.write_text(MOCK)
            executable.chmod(0o700)
        self.bash = shutil.which("bash")
        self.assertIsNotNone(self.bash, "These shell orchestration tests require bash")

    def run_script(self, script_name, case="success", command=None, **extra):
        # Reset per-run state, allowing independent subtests within one fixture.
        for name in ["calls", "executed"]:
            (self.root / name).unlink(missing_ok=True)
        (self.state / "deployed-sha").unlink(missing_ok=True)
        (self.root / "head").write_text(PREVIOUS)
        (self.root / "up_count").write_text("0")
        source = (REPO / "scripts" / script_name).read_text()
        self.assertIn("repo=/opt/mlop", source)
        self.assertIn("state=/home/ubuntu/.local/state/mlop-deploy", source)
        script = self.root / script_name
        script.write_text(source.replace("repo=/opt/mlop", f"repo={self.repo}").replace(
            "state=/home/ubuntu/.local/state/mlop-deploy", f"state={self.state}"
        ))
        environment = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TEST_ROOT": str(self.root), "CASE": case,
            "PREVIOUS": PREVIOUS, "REVISION": REVISION,
            "FAIL_URL": URLS[1], "FAIL_SERVICE": "ingest",
            "SSH_ORIGINAL_COMMAND": "deploy " + REVISION if command is None else command,
            **extra,
        }
        result = subprocess.run(
            [self.bash, str(script), REVISION], env=environment,
            text=True, capture_output=True, timeout=30,
        )
        call_file = self.root / "calls"
        self.calls = [
            [part.decode() for part in row.split(b"\0")[:-1]]
            for row in call_file.read_bytes().splitlines()
        ] if call_file.exists() else []
        self.output = result.stdout + result.stderr
        self.assertNotIn("SECRET_TEST_VALUE", self.output)
        self.assertFalse(self.env_marker.exists(), ".env must never be sourced as shell code")
        self.assertNotIn(result.returncode, [96, 97], self.output)
        for call in self.calls:
            self.assertFalse(any(value in call for value in ["down", "prune", "volume"]), call)
        self.ups = [call for call in self.calls if call[:4] == ["sudo", "-n", "docker", "compose"] and "up" in call]
        for call in self.ups:
            self.assertEqual(call[call.index("--project-name") + 1], "server")
            self.assertEqual(call[call.index("--project-directory") + 1], str(self.server))
            self.assertEqual(call[call.index("--env-file") + 1], str(self.server / ".env"))
            self.assertEqual(call[call.index("up"):], ["up", "-d", "--no-deps", "--no-build", "--pull", "never", *SERVICES])
        return result

    def assert_restored(self, result, replacement=False):
        self.assertNotEqual(result.returncode, 0, self.output)
        self.assertEqual((self.root / "head").read_text().strip(), PREVIOUS)
        self.assertFalse((self.state / "deployed-sha").exists())
        self.assertEqual(len(self.ups), 2 if replacement else 0, self.calls)
        if replacement:
            self.assertTrue(any(value.endswith("/images.yml") for value in self.ups[1]), self.ups[1])
            self.assertFalse(any(value.endswith("/images.yml") for value in self.ups[0]), self.ups[0])

    def test_success_saves_private_backup_and_deploys_exact_sha(self):
        result = self.run_script("deploy-lightsail.sh")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual((self.state / "deployed-sha").read_text().strip(), REVISION)
        self.assertEqual(len(self.ups), 1)
        builds = [call[-1] for call in self.calls if call[:4] == ["sudo", "-n", "docker", "compose"] and "build" in call]
        self.assertEqual(builds, SERVICES)
        probes = [call[-1] for call in self.calls if call[0] == "curl"]
        self.assertEqual(probes, URLS)
        release, = (self.state / "releases").iterdir()
        self.assertEqual((release / "previous-sha").read_text().strip(), PREVIOUS)
        self.assertEqual(gzip.decompress((release / "postgres.sql.gz").read_bytes()), b"postgres backup mock\n")
        for path in [release, release / "postgres.sql.gz", self.server / ".env"]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0, str(path))
        images = (release / "images.yml").read_text()
        for service in SERVICES:
            self.assertIn(f"image: mlop-rollback/{service}:", images)
            self.assertTrue(any(call[:5] == ["sudo", "-n", "docker", "image", "tag"] and call[5] == f"sha256:old-container-{service}" for call in self.calls))
        dump_index = next(i for i, call in enumerate(self.calls) if call[:4] == ["sudo", "-n", "docker", "exec"])
        checkout_index = next(i for i, call in enumerate(self.calls) if call[:2] == ["git", "checkout"])
        self.assertLess(dump_index, checkout_index)

    def test_pre_replacement_failures_never_restart_apps(self):
        for case in ["disk_low", "missing_service", "dump_failure", "submodule_failure", "config_failure"]:
            with self.subTest(case=case):
                self.assert_restored(self.run_script("deploy-lightsail.sh", case))
        for service in SERVICES:
            with self.subTest(build=service):
                self.assert_restored(self.run_script("deploy-lightsail.sh", "build_failure", FAIL_SERVICE=service))

    def test_real_or_symlinked_env_in_build_context_is_rejected(self):
        secret = self.server / "web" / ".env.production"
        for symlink in [False, True]:
            with self.subTest(symlink=symlink):
                if symlink:
                    secret.symlink_to(self.server / ".env")
                else:
                    secret.write_text("OTHER_SECRET=value\n")
                try:
                    self.assert_restored(self.run_script("deploy-lightsail.sh"))
                    self.assertIn("non-example .env file", self.output)
                    self.assertFalse(any("build" in call for call in self.calls))
                finally:
                    secret.unlink()

    def test_partial_replacement_and_termination_restore_old_images(self):
        for case in ["up_failure", "termination"]:
            with self.subTest(case=case):
                result = self.run_script("deploy-lightsail.sh", case)
                self.assert_restored(result, replacement=True)
                self.assertIn("Previous application images restored.", self.output)
                if case == "termination":
                    self.assertEqual(result.returncode, 143)

    def test_each_deployment_probe_failure_rolls_back(self):
        for url in URLS:
            with self.subTest(url=url):
                self.assert_restored(self.run_script("deploy-lightsail.sh", "health_failure", FAIL_URL=url), replacement=True)
                self.assertIn("Previous application images restored.", self.output)

    def test_each_recovery_probe_failure_never_reports_success(self):
        for url in URLS:
            with self.subTest(url=url):
                self.assert_restored(self.run_script("deploy-lightsail.sh", "rollback_health_failure", FAIL_URL=url), replacement=True)
                self.assertNotIn("Previous application images restored.", self.output)
                self.assertIn("Automatic application recovery failed", self.output)

    def test_recovery_up_failure_reports_operator_action(self):
        self.assert_restored(self.run_script("deploy-lightsail.sh", "rollback_up_failure"), replacement=True)
        self.assertIn("operator intervention required", self.output)
        self.assertNotIn("Previous application images restored.", self.output)

    def test_source_recovery_failure_does_not_start_mixed_release(self):
        result = self.run_script("deploy-lightsail.sh", "source_recovery_failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.ups), 1)
        self.assertIn("Source recovery failed", self.output)
        self.assertNotIn("Previous application images restored.", self.output)

    def test_dispatch_accepts_only_current_main_under_lock(self):
        result = self.run_script("lightsail-dispatch.sh")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertEqual((self.root / "executed").read_text().strip(), REVISION)
        self.assertEqual(self.calls[0][:3], ["flock", "-w", "7200"])
        self.assertIn(["git", "fetch", "--no-tags", "https://github.com/Theseus-Robotics/mlop.git", "main"], self.calls)
        self.assertFalse([path for path in self.state.glob("deploy.*") if path.name != "deploy.lock"], "temporary deployment script must be removed")

    def test_dispatch_rejects_invalid_commands_before_git(self):
        for command in ["", "deploy", "deploy " + REVISION + "; id", "deploy " + REVISION + " extra", "deploy " + "B" * 40, "deploy " + REVISION[:-1]]:
            with self.subTest(command=command):
                result = self.run_script("lightsail-dispatch.sh", command=command)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(self.calls, [])
                self.assertFalse((self.root / "executed").exists())

    def test_dispatch_fails_closed_on_dirty_or_unreadable_source(self):
        for case in ["dirty", "dirty_submodule", "status_error", "submodule_status_error", "lock_failure"]:
            with self.subTest(case=case):
                result = self.run_script("lightsail-dispatch.sh", case)
                self.assertNotEqual(result.returncode, 0, self.output)
                self.assertFalse((self.root / "executed").exists())
                self.assertFalse(any(call[:2] == ["git", "fetch"] for call in self.calls))

    def test_dispatch_never_executes_stale_or_unavailable_revision(self):
        for case in ["stale", "fetch_failure", "show_failure", "revision_error"]:
            with self.subTest(case=case):
                result = self.run_script("lightsail-dispatch.sh", case)
                self.assertEqual(result.returncode == 0, case == "stale", self.output)
                self.assertFalse((self.root / "executed").exists())

    def test_success_retains_three_recovery_snapshots(self):
        releases = self.state / "releases"
        releases.mkdir()
        older = [releases / (f"2026010{day}T000000Z-" + "a" * 12) for day in range(1, 5)]
        for path in older:
            path.mkdir()
            (path / "postgres.sql.gz").write_text("old backup")
        unrelated = releases / "operator-backup"
        unrelated.mkdir()
        result = self.run_script("deploy-lightsail.sh")
        self.assertEqual(result.returncode, 0, self.output)
        self.assertFalse(older[0].exists())
        self.assertFalse(older[1].exists())
        self.assertTrue(older[2].exists())
        self.assertTrue(older[3].exists())
        self.assertTrue(unrelated.exists())
        removed = [call[-1] for call in self.calls if call[:5] == ["sudo", "-n", "docker", "image", "rm"]]
        self.assertEqual(set(removed), {f"mlop-rollback/{service}:{release.name}" for service in SERVICES for release in older[:2]})


if __name__ == "__main__":
    unittest.main()
