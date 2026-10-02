"""Offline regression tests. Run: python3 -m unittest discover -s tests -v."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]

# All infrastructure commands are replaced, including read-only Docker/IP calls.
MOCK_COMMAND = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
with open(os.environ["MOCK_LOG"], "a") as log:
    log.write(json.dumps([command, *sys.argv[1:]]) + "\\n")
if command == "ip":
    if os.environ.get("MOCK_IP_FAIL"):
        sys.exit(1)
    print(json.dumps([{"addr_info": [{"local": address} for address in
        os.environ["MOCK_ADDRESSES"].split(",") if address]}]))
elif command == "docker":
    if os.environ.get("MOCK_DOCKER_FAIL"):
        sys.exit(1)
    if sys.argv[1] == "ps":
        print(os.environ.get("MOCK_CONTAINERS", ""))
'''


class ArtifactServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.env = dict(os.environ)
        self.env.pop("PORT", None)
        self.env.update(
            PATH=f"{self.bin}:{os.environ['PATH']}",
            MOCK_LOG=str(self.log),
            MOCK_ADDRESSES="10.0.10.1,10.0.20.1",
            MOCK_CONTAINERS="",
        )
        for command in ("docker", "ip", "terraform", "ssh-keygen"):
            path = self.bin / command
            path.write_text(MOCK_COMMAND)
            path.chmod(0o755)

    def calls(self, command):
        if not self.log.exists():
            return []
        return [
            args[1:]
            for line in self.log.read_text().splitlines()
            if (args := json.loads(line))[0] == command
        ]

    def deployment(self, prefix="hvst", url="http://10.0.10.1:8787"):
        root = self.root / prefix
        artifacts = root / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        for name in ("run-server.sh", "server-endpoint.py"):
            shutil.copy(ROOT / "artifacts" / name, artifacts / name)
        (root / "config.yaml").write_text(json.dumps({
            "provider": {"domain_prefix": prefix},
            "artifact_server_url": url,
        }))
        return root

    def start(self, root):
        return subprocess.run(
            ["bash", str(root / "artifacts/run-server.sh")],
            cwd=root, env=self.env, capture_output=True, text=True,
        )

    def test_two_deployments_share_port_on_different_addresses(self):
        for prefix, address in (("hvst-a", "10.0.10.1"), ("hvst-b", "10.0.20.1")):
            url = f"http://{address}:8787"
            root = self.deployment(prefix, url)
            before = (root / "config.yaml").read_text()
            result = self.start(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"Artifacts server is running at {url}", result.stdout)
            self.assertEqual((root / "config.yaml").read_text(), before)
        runs = [args for args in self.calls("docker") if args[0] == "run"]
        self.assertEqual(len(runs), 2)
        self.assertEqual([args[args.index("-p") + 1] for args in runs], [
            "10.0.10.1:8787:80", "10.0.20.1:8787:80",
        ])
        self.assertEqual([args[args.index("--name") + 1] for args in runs], [
            "hvst-a-artifacts-server", "hvst-b-artifacts-server",
        ])

    def test_custom_default_and_matching_legacy_ports(self):
        for url, override, expected in (
            ("http://10.0.10.1:9898", "9898", "9898"),
            ("http://10.0.10.1/", "80", "80"),
            ("http://10.0.10.1", "", "80"),
        ):
            with self.subTest(url=url):
                self.env["PORT"] = override
                result = self.start(self.deployment(url=url))
                self.assertEqual(result.returncode, 0, result.stderr)
                run = [args for args in self.calls("docker") if args[0] == "run"][-1]
                self.assertEqual(run[run.index("-p") + 1], f"10.0.10.1:{expected}:80")

    def test_only_exact_container_name_is_replaced(self):
        root = self.deployment(prefix="hvst.a")
        self.env["MOCK_CONTAINERS"] = "hvstXa-artifacts-server\nhvst.a-artifacts-server-old"
        self.assertEqual(self.start(root).returncode, 0)
        self.assertFalse([args for args in self.calls("docker") if args[0] in ("stop", "rm")])
        self.env["MOCK_CONTAINERS"] += "\nhvst.a-artifacts-server"
        self.assertEqual(self.start(root).returncode, 0)
        self.assertEqual(
            [args for args in self.calls("docker") if args[0] in ("stop", "rm")],
            [["stop", "hvst.a-artifacts-server"], ["rm", "hvst.a-artifacts-server"]],
        )

    def test_invalid_urls_do_not_touch_docker(self):
        for url in (
            "", "null", "https://10.0.10.1:8787", "http://example.com:8787",
            "http://[::1]:8787", "http://0.0.0.0:8787", "http://224.0.0.1:8787",
            "http://255.255.255.255:8787", "http://user:pass@10.0.10.1:8787",
            "http://10.0.10.1:0", "http://10.0.10.1:65536", "http://10.0.10.1:-1",
            "http://10.0.10.1:abc", "http://10.0.10.1:", "http://10.0.10.1/isos",
            "http://10.0.10.1?", "http://10.0.10.1#", " http://10.0.10.1",
            "http://10.0.\n10.1",
        ):
            with self.subTest(url=url):
                result = self.start(self.deployment(url=url))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Error resolving artifact_server_url", result.stderr)
                self.assertEqual(self.calls("docker"), [])

    def test_missing_address_does_not_touch_docker(self):
        self.env["MOCK_ADDRESSES"] = "10.0.20.1"
        result = self.start(self.deployment())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task networks-up", result.stderr)
        self.assertEqual(self.calls("docker"), [])

    def test_ip_command_failure_does_not_touch_docker(self):
        self.env["MOCK_IP_FAIL"] = "1"
        result = self.start(self.deployment())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Error resolving artifact_server_url", result.stderr)
        self.assertEqual(self.calls("docker"), [])

    def test_conflicting_legacy_port_does_not_touch_docker(self):
        self.env["PORT"] = "9898"
        result = self.start(self.deployment())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Update artifact_server_url", result.stderr)
        self.assertEqual(self.calls("docker"), [])

    def test_docker_listing_failure_stops_startup(self):
        self.env["MOCK_DOCKER_FAIL"] = "1"
        result = self.start(self.deployment())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls("docker"), [["ps", "-a", "--format", "{{.Names}}"]])

    def test_network_bootstrap_targets_only_networks(self):
        root = self.deployment()
        shutil.copy(ROOT / "Taskfile.yml", root / "Taskfile.yml")
        (root / "op").mkdir()
        (root / "op/Taskfile.yml").write_text("version: '3'\ntasks: {}\n")
        (root / "terraform").mkdir()
        result = subprocess.run(
            ["task", "networks-up"], cwd=root, env=self.env,
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("terraform"), [
            [f"-chdir={root}/terraform", "init"],
            [f"-chdir={root}/terraform", "apply", "--auto-approve",
             "-target=libvirt_network.hvst_libvirt",
             "-target=libvirt_network.hvst_mgmt",
             "-target=libvirt_network.hvst_data"],
        ])
        self.assertEqual(len(self.calls("ssh-keygen")), 1)
        self.assertEqual(self.calls("docker"), [])

    def test_network_planning_preserves_url_ports(self):
        config = self.root / "config.yaml"
        config.write_text((ROOT / "config.yaml.sample").read_text().replace(":8787", ":9898"))
        original = yaml.safe_load(config.read_text())
        original["tests"]["upgrade"]["iso_url"] = "http://10.0.10.1:9898/isos/upgrade.iso"
        config.write_text(yaml.safe_dump(original))
        result = subprocess.run(
            [str(ROOT / "scripts/plan-networks.sh"),
             "--nat", "hvst-b-nat,192.168.124.0/24",
             "--mgmt", "hvst-b-mgmt,10.0.20.0/24",
             "--data", "hvst-b-data,10.0.21.0/24", str(config)],
            cwd=self.root, env=self.env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        updated = yaml.safe_load(config.read_text())
        self.assertEqual(updated["artifact_server_url"], "http://10.0.20.1:9898")
        for field in ("iso", "kernel", "ramdisk", "rootfs"):
            self.assertTrue(updated[f"harvester_{field}_url"].startswith("http://10.0.20.1:9898/"))
        self.assertTrue(updated["tests"]["upgrade"]["iso_url"].startswith("http://10.0.20.1:9898/"))
        self.assertEqual(self.calls("terraform"), [])


if __name__ == "__main__":
    unittest.main()
