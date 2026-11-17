from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# server.py treats the current directory as the installed module root.
_TEMP_ROOT = tempfile.TemporaryDirectory()
os.chdir(_TEMP_ROOT.name)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import server  # noqa: E402


def has_qemu() -> bool:
    return bool(server.qemu_for(server.host_arch()) and server.qemu_img())


class VmLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(dir=_TEMP_ROOT.name))
        server.VMS = self.root / "vms"
        server.IMAGES = self.root / "images"
        server.RECIPES = self.root / "recipes"
        server.CATALOG_FILE = self.root / "catalog.json"
        server.VMS.mkdir()
        server.IMAGES.mkdir()
        server.RECIPES.mkdir()
        server.CATALOG_FILE.write_text(json.dumps(server.DEFAULT_CATALOG), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_catalog_exposes_platform_compatibility(self) -> None:
        view = {item["id"]: item for item in server.catalog_view()}
        self.assertTrue(view["ubuntu-24.04"]["host_compatible"])
        if server.host_os() != "macos":
            self.assertFalse(view["macos"]["host_compatible"])

    def test_invalid_vm_names_are_rejected(self) -> None:
        for name in ("", "../escape", ".hidden", "has space"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    server.vm_dir(name)

    def test_recipe_view_exposes_provisioning(self) -> None:
        view = server.recipe_view({
            "id": "demo",
            "name": "Demo",
            "image": "ubuntu-24.04",
            "cloud_init": "#cloud-config\n",
            "provision": ["true", "true"],
        })
        self.assertEqual(view["id"], "demo")
        self.assertTrue(view["has_cloud_init"])
        self.assertEqual(view["provision_count"], 2)

    def test_iso_args_attach_installer_and_vnc(self) -> None:
        if not has_qemu():
            self.skipTest("QEMU is not installed")
        name = "iso-test"
        (server.VMS / name).mkdir()
        iso = self.root / "installer.iso"
        iso.write_bytes(b"not a real iso")
        meta = {
            "name": name,
            "arch": "x86_64",
            "kind": "iso",
            "iso": str(iso),
            "installed": False,
            "cpus": 2,
            "memory_mb": 1024,
            "display": "headless",
            "ssh_port": server.free_port(22100),
            "vnc_port": server.free_port(5920),
            "qmp_port": server.free_port(44500),
            "uefi": False,
        }
        args = server.qemu_args(name, meta)
        self.assertIn("-cdrom", args)
        self.assertIn(str(iso), args)
        self.assertIn("-vnc", args)
        self.assertIn("password=on", args[args.index("-vnc") + 1])
        self.assertIn("-qmp", args)
        self.assertTrue(args[args.index("-qmp") + 1].startswith("unix:"))
        self.assertIn("-display", args)

    def test_udp_forward_args(self) -> None:
        if not has_qemu():
            self.skipTest("QEMU is not installed")
        name = "udp-test"
        (server.VMS / name).mkdir()
        seed = server.VMS / name / "seed"
        seed.mkdir()
        (seed / "seed.img").write_bytes(b"\0" * 1024)
        meta = {
            "name": name,
            "arch": "x86_64",
            "kind": "cloud",
            "cpus": 2,
            "memory_mb": 1024,
            "display": "headless",
            "ssh_port": server.free_port(22300),
            "vnc_port": server.free_port(5940),
            "qmp_port": server.free_port(44700),
            "allow_lan_forwards": True,
            "udp_forwards": [
                {"host": "192.0.2.10", "host_port": 7842, "guest_port": 7842}
            ],
            "uefi": False,
        }
        args = server.qemu_args(name, meta)
        netdev = args[args.index("-netdev") + 1]
        self.assertIn("hostfwd=udp:192.0.2.10:7842-:7842", netdev)

    def test_cloud_args_enable_seed_network(self) -> None:
        if not has_qemu():
            self.skipTest("QEMU is not installed")
        name = "cloud-test"
        (server.VMS / name).mkdir()
        seed = server.VMS / name / "seed"
        seed.mkdir()
        (seed / "seed.img").write_bytes(b"\0" * 1024)
        meta = {
            "name": name,
            "arch": "x86_64",
            "kind": "cloud",
            "cpus": 2,
            "memory_mb": 1024,
            "display": "headless",
            "ssh_port": server.free_port(22200),
            "vnc_port": server.free_port(5930),
            "qmp_port": server.free_port(44600),
            "uefi": False,
        }
        args = server.qemu_args(name, meta)
        self.assertTrue(any("seed.img" in value for value in args))

    def test_unattend_floppy_and_xml(self) -> None:
        xml = server.windows_unattend_xml("win-test", False, "orqella", "orqella")
        self.assertIn("<DiskID>0</DiskID>", xml)
        self.assertIn("BypassTPMCheck", xml)
        self.assertIn("<Name>orqella</Name>", xml)
        image = self.root / "unattend.img"
        server.make_floppy(image, "AUTOUNATTEND.XML", xml.encode())
        self.assertEqual(image.stat().st_size, 1440 * 1024)
        raw = image.read_bytes()
        self.assertIn(b"AUTOUN~1XML", raw)
        self.assertIn(b"<unattend", raw)

    def test_windows_guest_uses_ide_and_e1000(self) -> None:
        if not has_qemu():
            self.skipTest("QEMU is not installed")
        name = "windows-test"
        (server.VMS / name).mkdir()
        meta = {
            "name": name,
            "os": "windows",
            "arch": "x86_64",
            "kind": "disk",
            "cpus": 2,
            "memory_mb": 1024,
            "display": "headless",
            "ssh_port": server.free_port(22300),
            "vnc_port": server.free_port(5940),
            "qmp_port": server.free_port(44700),
            "uefi": False,
        }
        args = server.qemu_args(name, meta)
        self.assertTrue(any("ide-hd" in arg for arg in args))
        self.assertTrue(any("e1000" in arg for arg in args))
        self.assertTrue(any("usb-tablet" in arg for arg in args))
        self.assertFalse(any("virtio-blk" in arg for arg in args))
        self.assertFalse(any("virtio-net-pci" in arg for arg in args))

    def test_vm_creation_can_write_an_unattend_floppy(self) -> None:
        if not has_qemu():
            self.skipTest("QEMU is not installed")
        raw = self.root / "base.img"
        raw.write_bytes(b"\0" * 65536)
        job = {}
        server.create_vm("unattend-test", {
            "image": "custom-disk",
            "path": str(raw),
            "start": False,
            "unattend": True,
            "cpus": 1,
            "memory_mb": 512,
            "display": "headless",
        }, job)
        self.assertEqual(job.get("status"), "succeeded", job)
        meta = server.read_meta("unattend-test")
        self.assertTrue(meta.get("unattend"))
        floppy = server.vm_dir("unattend-test") / "unattend" / "autounattend.img"
        self.assertTrue(floppy.is_file())
        self.assertEqual(floppy.stat().st_size, 1440 * 1024)
        self.assertEqual(floppy.stat().st_mode & 0o777, 0o600)
        self.assertIn(b"AUTOUN~1XML", floppy.read_bytes())
        self.assertNotEqual(meta.get("unattend_password"), "orqella")
        self.assertEqual(server.meta_path("unattend-test").stat().st_mode & 0o777, 0o600)
        xml = server.vm_dir("unattend-test") / "unattend" / "autounattend.xml"
        self.assertEqual(xml.stat().st_mode & 0o777, 0o600)

    def test_ssh_command_targets_the_guest_user(self) -> None:
        name = "ssh-test"
        (server.VMS / name).mkdir()
        meta = {"name": name, "guest_user": "ubuntu", "ssh_port": 22000}
        command = server.ssh_command(meta)
        self.assertIn("-l ubuntu", command)
        self.assertIn("127.0.0.1", command)
        self.assertIn("-p 22000", command)
        self.assertEqual(
            server.ssh_command({"name": name, "guest_user": "-oProxyCommand=id", "ssh_port": 22}),
            "",
        )


class VmLabEdgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(dir=_TEMP_ROOT.name))
        server.VMS = self.root / "vms"
        server.IMAGES = self.root / "images"
        server.RECIPES = self.root / "recipes"
        server.DATA = self.root / "data"
        server.CATALOG_FILE = self.root / "catalog.json"
        for directory in (server.VMS, server.IMAGES, server.RECIPES, server.DATA):
            directory.mkdir()
        server.CATALOG_FILE.write_text(json.dumps(server.DEFAULT_CATALOG), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_recipes_load_valid_entries_and_skip_bad_ones(self) -> None:
        (server.RECIPES / "demo.json").write_text(
            json.dumps(
                {
                    "name": "Demo",
                    "image": "ubuntu-24.04",
                    "cloud_init": "#cloud-config\n",
                    "provision": ["true"],
                }
            ),
            encoding="utf-8",
        )
        (server.RECIPES / "bad-name.json").write_text(
            json.dumps({"id": "../escape", "name": "Bad", "image": "ubuntu-24.04"}),
            encoding="utf-8",
        )
        (server.RECIPES / "invalid.json").write_text("{not json", encoding="utf-8")
        (server.RECIPES / "missing-image.json").write_text(
            json.dumps({"id": "missing", "name": "Missing"}), encoding="utf-8"
        )
        recipes = server.load_recipes()
        self.assertEqual([recipe["id"] for recipe in recipes], ["demo"])
        self.assertEqual(server.recipe_by_id("demo")["name"], "Demo")
        with self.assertRaises(ValueError):
            server.recipe_by_id("unknown")

    def test_recipe_images_can_be_catalog_ids_or_custom_objects(self) -> None:
        image = server.recipe_image({"image": "ubuntu-24.04"})
        self.assertEqual(image["id"], "ubuntu-24.04")
        custom = server.recipe_image(
            {
                "id": "recipe-x",
                "name": "Recipe X",
                "image": {"path": "/tmp/disk.img", "os": "windows"},
            }
        )
        self.assertEqual(custom["id"], "recipe-x")
        self.assertEqual(custom["os"], "windows")
        self.assertEqual(custom["cpus"], 2)
        with self.assertRaises(ValueError):
            server.recipe_image({"image": 42})

    def test_meta_round_trips_and_missing_vms_are_rejected(self) -> None:
        server.vm_dir("meta-test").mkdir()
        server.write_meta("meta-test", {"name": "meta-test", "cpus": 4})
        self.assertEqual(server.read_meta("meta-test")["cpus"], 4)
        with self.assertRaises(ValueError):
            server.read_meta("missing")
        with self.assertRaises(ValueError):
            server.read_meta("../escape")

    def test_free_port_skips_a_bound_port(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            occupied = sock.getsockname()[1]
            self.assertNotEqual(server.free_port(occupied), occupied)
            self.assertGreater(server.free_port(occupied), occupied)

    def test_catalog_overlays_defaults_and_reports_cache_state(self) -> None:
        server.CATALOG_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual(
            {item["id"] for item in server.load_catalog()},
            {item["id"] for item in server.DEFAULT_CATALOG},
        )
        server.CATALOG_FILE.write_text(
            json.dumps([{"id": "custom", "name": "Custom", "kind": "disk"}]),
            encoding="utf-8",
        )
        ids = {item["id"] for item in server.load_catalog()}
        self.assertIn("custom", ids)
        self.assertIn("ubuntu-24.04", ids)
        view = {item["id"]: item for item in server.catalog_view()}
        self.assertIn("cached", view["custom"])
        self.assertFalse(view["custom"]["cached"])

    def test_image_source_resolves_relative_paths_and_rejects_missing_files(self) -> None:
        image = server.DATA / "disk.img"
        image.write_bytes(b"disk")
        self.assertEqual(
            server.image_source({"id": "local", "path": "disk.img"}, print),
            image,
        )
        with self.assertRaises(RuntimeError):
            server.image_source({"id": "missing", "path": "missing.img"}, print)
        with self.assertRaises(RuntimeError):
            server.image_source({"id": "empty"}, print)

    def test_actions_reject_unknown_and_destroy_removes_the_directory(self) -> None:
        server.vm_dir("action-test").mkdir()
        server.write_meta("action-test", {"name": "action-test"})
        with self.assertRaises(ValueError):
            server.action_vm("action-test", "explode")
        result = server.action_vm("action-test", "destroy")
        self.assertTrue(result["ok"])
        self.assertFalse(server.vm_dir("action-test").exists())

    def test_list_vms_skips_invalid_directories(self) -> None:
        server.vm_dir("good").mkdir()
        server.write_meta(
            "good",
            {
                "name": "good",
                "image": "ubuntu-24.04",
                "os": "linux",
                "arch": "x86_64",
                "kind": "cloud",
                "display": "headless",
                "cpus": 2,
                "memory_mb": 1024,
                "ssh_port": 2222,
                "vnc_port": 5920,
            },
        )
        (server.VMS / "bad").mkdir()
        (server.VMS / "not-a-dir").write_text("x", encoding="utf-8")
        vms = server.list_vms()
        self.assertEqual([vm["name"] for vm in vms], ["good"])
        self.assertFalse(vms[0]["running"])
        self.assertEqual(vms[0]["disk_bytes"], 0)

    def test_websocket_handshake_and_frame_encoding(self) -> None:
        import io

        self.assertEqual(
            server.ws_accept("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )
        self.assertEqual(server.ws_frame(1, b"hi")[:2], bytes([0x81, 2]))
        self.assertEqual(server.ws_frame(2, b"x" * 65536)[:2], bytes([0x82, 127]))

        handler = server.Handler.__new__(server.Handler)
        handler.rfile = io.BytesIO(bytes([0x82, 0xFE, 0x00]))
        self.assertEqual(handler.ws_read(), (None, b""))

    def test_vnc_stream_drops_the_connect_timeout(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            upstream = server.connect_vnc_stream(listener.getsockname()[1])
            try:
                self.assertIsNone(upstream.gettimeout())
            finally:
                upstream.close()
        finally:
            listener.close()

    def test_image_source_rejects_traversal_and_cleans_partial_download(self) -> None:
        with self.assertRaises(ValueError):
            server.image_source({"id": "../escape", "url": "https://example.invalid/a.img"}, print)

        class BrokenResponse:
            headers = {"Content-Length": "100"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _n):
                raise OSError("network dropped")

        with __import__("unittest.mock").mock.patch.object(
            server.urllib.request, "urlopen", return_value=BrokenResponse()
        ):
            with self.assertRaises(OSError):
                server.image_source(
                    {"id": "ubuntu-24.04", "url": "https://example.invalid/a.img", "file": "a.img"},
                    print,
                )
        self.assertFalse((server.IMAGES / "ubuntu-24.04" / "a.img").exists())
        self.assertEqual(list((server.IMAGES / "ubuntu-24.04").glob("*.part")), [])

    def test_qemu_args_validates_udp_forwards(self) -> None:
        server.vm_dir("udp-bad").mkdir()
        with self.assertRaises(ValueError):
            server.qemu_args(
                "udp-bad",
                {
                    "arch": "x86_64",
                    "ssh_port": 22000,
                    "vnc_port": 5910,
                    "qmp_port": 44400,
                    "udp_forwards": [{"host": "127.0.0.1,evil=1", "host_port": 7841, "guest_port": 7841}],
                },
            )

    def test_udp_forwards_require_lan_opt_in(self) -> None:
        if not has_qemu():
            self.skipTest("QEMU is not installed")
        server.vm_dir("udp-lan").mkdir()
        meta = {
            "arch": "x86_64",
            "ssh_port": server.free_port(22800),
            "vnc_port": server.free_port(5980),
            "qmp_port": server.free_port(44800),
            "udp_forwards": [{"host": "192.0.2.10", "host_port": 7842, "guest_port": 7842}],
        }
        with self.assertRaises(ValueError):
            server.qemu_args("udp-lan", meta)
        meta["allow_lan_forwards"] = True
        args = server.qemu_args("udp-lan", meta)
        netdev = args[args.index("-netdev") + 1]
        self.assertIn("hostfwd=udp:192.0.2.10:7842-:7842", netdev)

    def test_metadata_is_private_and_atomic(self) -> None:
        server.vm_dir("meta-private").mkdir()
        server.write_meta("meta-private", {"name": "meta-private", "vnc_password": "secret"})
        path = server.meta_path("meta-private")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(server.read_meta("meta-private")["vnc_password"], "secret")

    def test_process_identity_rejects_non_qemu(self) -> None:
        start = server._process_start_time(os.getpid())
        if not start:
            self.skipTest("process start time is unavailable on this host")
        self.assertTrue(server.pid_alive(os.getpid()))
        self.assertFalse(server.expected_process({"pid": os.getpid(), "pid_start": start, "name": "demo"}))

    def test_port_allocator_reserves_unique_ports(self) -> None:
        first = server.allocate_port(23000, "port-a")
        second = server.allocate_port(23000, "port-b")
        try:
            self.assertNotEqual(first, second)
            self.assertTrue(server.claim_port(first, "port-a"))
            self.assertFalse(server.claim_port(first, "port-b"))
        finally:
            server.release_vm_ports("port-a")
            server.release_vm_ports("port-b")

    def test_image_source_verifies_optional_sha256(self) -> None:
        image = server.DATA / "hashed.img"
        image.write_bytes(b"disk-image")
        import hashlib
        digest = hashlib.sha256(b"disk-image").hexdigest()
        self.assertEqual(
            server.image_source({"id": "hashed", "path": "hashed.img", "sha256": digest}, print),
            image,
        )
        with self.assertRaises(RuntimeError):
            server.image_source({"id": "hashed", "path": "hashed.img", "sha256": "0" * 64}, print)

    def test_qmp_target_uses_a_unix_socket(self) -> None:
        if server.qmp_socket_supported():
            target = server.qmp_target("qmp-test", {"qmp_port": 44444})
            self.assertTrue(str(target).endswith("qmp.sock"))

    def test_nested_console_frames_relay_the_module_sdk(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app = (root / "ui" / "app.js").read_text(encoding="utf-8")
        console = (root / "ui" / "console.js").read_text(encoding="utf-8")
        self.assertIn("__orqella_ws", app)
        self.assertIn("orqella:request", app)
        self.assertIn("orqella:init", app)
        self.assertIn("nestedFrameUrl", app)
        self.assertIn("nestCall", console)
        self.assertNotIn("window.prompt", console)

    def test_floppy_image_passes_fat_check_when_available(self) -> None:
        tool = shutil.which("fsck.fat")
        if not tool:
            self.skipTest("fsck.fat is not installed")
        xml = server.windows_unattend_xml("floppy-test", False, "orqella", "secret")
        image = self.root / "check.img"
        server.make_floppy(image, "AUTOUNATTEND.XML", xml.encode())
        result = subprocess.run([tool, "-n", str(image)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
