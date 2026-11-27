import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from scripts import deploy_direct as deploy


class DirectDeploymentTest(unittest.TestCase):
    def test_upload_keeps_site_files_private_and_preserves_compose(self):
        for modern in (False, True):
            files = {path.relative_to(deploy.ROOT).as_posix() for path in deploy.collect_files(modern)}
            self.assertTrue({"app/main.py", "web/index.html", "config.yaml", "requirements.txt"} <= files)
            self.assertEqual("Dockerfile" in files, modern)
            self.assertEqual(".dockerignore" in files, modern)
            self.assertTrue({"docker-compose.yml", "config.local.yaml", ".env"}.isdisjoint(files))

    def test_maps_wildcard_and_specific_published_addresses(self):
        self.assertEqual(deploy.published_urls({
            "8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "80"}, {"HostIp": "::", "HostPort": "80"}],
            "8001/tcp": None, "80/tcp": [{"HostIp": "10.0.0.12", "HostPort": "8045"}],
        }), ["http://127.0.0.1:80", "http://[::1]:80", "http://10.0.0.12:8045"])
        with patch.object(deploy, "HOST", "release.example"):
            wildcard = {"web": [{"HostIp": "0.0.0.0", "HostPort": "80"}]}
            self.assertEqual(deploy.client_address(wildcard, "http://127.0.0.1:80"), "http://release.example/")
            loopback = {"web": [{"HostIp": "127.0.0.1", "HostPort": "80"}]}
            self.assertIsNone(deploy.client_address(loopback, "http://127.0.0.1:80"))

    def test_http_200_from_another_app_is_not_a_success(self):
        api = json.dumps({"portal": "search", "authenticated": False})
        with patch.object(deploy, "inspect", return_value={"80/tcp": [{"HostIp": "", "HostPort": "9120"}]}), \
                patch.object(deploy, "run", side_effect=[api, "unrelated homepage", api, "<title>企业知识库</title>"]) as run, \
                patch.object(deploy.time, "sleep"):
            self.assertEqual(deploy.health(Mock()), "http://127.0.0.1:9120")
            self.assertEqual(run.call_count, 4)

    def test_legacy_failure_preserves_or_restores_working_files_and_runtime(self):
        for failure, expected in (("prepare", ["ready", "prepare"]),
                                  ("health", ["ready", "prepare", "publish", "activate", "health", "restore", "rollback"])):
            with self.subTest(failure=failure):
                steps = []
                mount = {"Type": "bind", "Destination": "/app", "Source": deploy.REMOTE}
                def inspect(client, path):
                    return {".Mounts": [mount], ".Image": "sha256:image"}[path]
                def record(name):
                    steps.append(name)
                    if name == failure:
                        raise deploy.DeployError(name)
                def simulate(client, label, *args):
                    if args[0] == "mktemp":
                        return "/tmp/knowbase-upload.12345678"
                    if args[0] == "docker":
                        record(args[-3])
                    if args[0] == "bash":
                        record(args[2])
                        if args[2] == "ready":   # 依赖探测：就绪时输出 ready，跳过安装包上传
                            return "ready"
                    return ""
                with patch.object(deploy, "inspect", side_effect=inspect), patch.object(deploy, "upload"), \
                        patch.object(deploy, "run", side_effect=simulate), \
                        patch.object(deploy, "health", side_effect=lambda client: record("health")):
                    with self.assertRaises(deploy.DeployError):
                        deploy.deploy(MagicMock())
                self.assertEqual(steps, expected)

    def test_refuses_to_recreate_container_with_unmounted_attachments(self):
        labels = {"com.docker.compose.project": "site", "com.docker.compose.service": "ag-kb",
                  "com.docker.compose.project.working_dir": deploy.REMOTE,
                  "com.docker.compose.project.config_files": deploy.REMOTE + "/docker-compose.yml"}
        config = json.dumps([deploy.REMOTE, "Dockerfile", "image:today", {}])
        def simulate(client, step, *command):
            if command[0] == "bash":
                return config
            return "already-saved.pdf" if command[-1] == "/app/data/attachments" else ""
        with patch.object(deploy, "inspect", return_value=labels), patch.object(deploy, "run", side_effect=simulate):
            with self.assertRaisesRegex(deploy.DeployError, "attachments"):
                deploy.compose_plan(Mock(), [], "sha256:image")

    def test_parent_volume_and_changed_volume(self):
        a = {"/app/data": ("bind", "/shared/data")}
        b = {"/app/data/attachments": ("bind", "/shared/data/attachments")}
        self.assertEqual(deploy.volume_key(a, "/app/data/attachments"), deploy.volume_key(b, "/app/data/attachments"))
        self.assertIsNone(deploy.volume_key(b, "/app/data/images"))
        self.assertNotEqual(deploy.volume_key(a, "/app/data/attachments"),
                            deploy.volume_key({"/app/data": ("bind", "/new/data")}, "/app/data/attachments"))

    def test_staged_publish_can_restore_old_files_after_partial_activation(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage, root = Path(tmp) / "release", Path(tmp) / "site"
            source = stage / "source"
            source.mkdir(parents=True)
            root.mkdir()
            for filename, content in (("config.yaml", "new"), ("new.py", "additional")):
                (source / filename).write_text(content)
            (root / "config.yaml").write_text("old")
            (root / "config.local.yaml").write_text("keep")
            subprocess.run([sys.executable, "-c", deploy.PUBLISH, "publish", str(stage), str(root)], check=True)
            self.assertEqual((root / "config.yaml").read_text(), "new")
            subprocess.run([sys.executable, "-c", deploy.PUBLISH, "restore", str(stage), str(root)], check=True)
            self.assertEqual((root / "config.yaml").read_text(), "old")
            self.assertFalse((root / "new.py").exists())
            self.assertEqual((root / "config.local.yaml").read_text(), "keep")
