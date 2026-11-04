from __future__ import annotations

import base64
import json
import os
import shutil
import socket
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
        self.assertIn("-display", args)

    def test_cloud_args_enable_seed_network(self) -> None:
        if not has_qemu():
            self.skipTest("QEMU is not installed")
        name = "cloud-test"
        (server.VMS / name).mkdir()
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
            "seed_port": 18080,
            "uefi": False,
        }
        args = server.qemu_args(name, meta)
        self.assertTrue(any("nocloud-net" in value for value in args))

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
        self.assertIn(b"AUTOUN~1XML", floppy.read_bytes())

    def test_ssh_command_targets_the_guest_user(self) -> None:
        name = "ssh-test"
        (server.VMS / name).mkdir()
        meta = {"name": name, "guest_user": "ubuntu", "ssh_port": 22000}
        command = server.ssh_command(meta)
        self.assertIn("ubuntu@127.0.0.1", command)
        self.assertIn("-p 22000", command)


if __name__ == "__main__":
    unittest.main()
