"""Portable VM Lab sidecar.

The module manages QEMU machines without libvirt or a host-specific service
manager. Linux cloud images use cloud-init over QEMU's user network; installer
ISOs and existing disks are supported through the catalog. The web UI gets a
VNC console and an SSH terminal through the Orqella module websocket bridge.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import uuid as uuidlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

TOKEN = os.environ.get("ORQELLA_MODULE_TOKEN", "")
CONFIG = json.loads(os.environ.get("ORQELLA_MODULE_CONFIG") or "{}")
ROOT = Path.cwd()
DATA = ROOT / "data"
VMS = DATA / "vms"
IMAGES = DATA / "images"
CATALOG_FILE = DATA / "catalog.json"
RECIPES = DATA / "recipes"
AGENT_GUIDE = DATA / "AGENT.md"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
POWERDOWN_GRACE_SECS = 5
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_CATALOG = [
    {
        "id": "ubuntu-24.04",
        "name": "Ubuntu 24.04 LTS (cloud)",
        "os": "linux",
        "arch": "x86_64",
        "kind": "cloud",
        "url": "https://cloud-images.ubuntu.com/releases/24.04/release/ubuntu-24.04-server-cloudimg-amd64.img",
        "file": "ubuntu-24.04-server-cloudimg-amd64.img",
        "user": "ubuntu",
        "cpus": 2,
        "memory_mb": 4096,
        "disk_gb": 20,
        "uefi": False,
    },
    {
        "id": "debian-12",
        "name": "Debian 12 (cloud)",
        "os": "linux",
        "arch": "x86_64",
        "kind": "cloud",
        "url": "https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        "file": "debian-12-genericcloud-amd64.qcow2",
        "user": "debian",
        "cpus": 2,
        "memory_mb": 4096,
        "disk_gb": 20,
        "uefi": False,
    },
    {
        "id": "custom-linux-iso",
        "name": "Linux ISO (manual install)",
        "os": "linux",
        "arch": "x86_64",
        "kind": "iso",
        "path": "",
        "user": "",
        "cpus": 2,
        "memory_mb": 4096,
        "disk_gb": 40,
        "uefi": False,
        "requires_path": True,
    },
    {
        "id": "windows-11",
        "name": "Windows 11 ISO (UEFI)",
        "os": "windows",
        "arch": "x86_64",
        "kind": "iso",
        "path": "",
        "user": "",
        "cpus": 4,
        "memory_mb": 8192,
        "disk_gb": 64,
        "uefi": True,
        "requires_path": True,
        "note": "UEFI CD boot is under investigation for the current evaluation ISO. Use windows-11-bios for the validated unattended path.",
    },
    {
        "id": "windows-11-bios",
        "name": "Windows 11 ISO (BIOS compatibility)",
        "os": "windows",
        "arch": "x86_64",
        "kind": "iso",
        "path": "",
        "user": "",
        "cpus": 4,
        "memory_mb": 8192,
        "disk_gb": 64,
        "uefi": False,
        "requires_path": True,
        "note": "Validated Windows 11 evaluation install path with unattended setup. Uses BIOS/SeaBIOS and Windows 11 compatibility bypasses.",
    },
    {
        "id": "macos",
        "name": "macOS prepared disk (macOS host only)",
        "os": "macos",
        "arch": "aarch64",
        "kind": "disk",
        "path": "",
        "user": "",
        "cpus": 4,
        "memory_mb": 8192,
        "disk_gb": 64,
        "uefi": True,
        "requires_path": True,
        "host_platform": "Darwin",
        "note": "QEMU macOS guests require a prepared macOS disk and macOS host. Automatic macOS install is not implemented.",
    },
    {
        "id": "custom-disk",
        "name": "Existing qcow2/raw disk",
        "os": "linux",
        "arch": "x86_64",
        "kind": "disk",
        "path": "",
        "user": "",
        "cpus": 2,
        "memory_mb": 4096,
        "disk_gb": 40,
        "uefi": False,
        "requires_path": True,
    },
]

DATA.mkdir(parents=True, exist_ok=True)
VMS.mkdir(parents=True, exist_ok=True)
IMAGES.mkdir(parents=True, exist_ok=True)
RECIPES.mkdir(parents=True, exist_ok=True)
if not CATALOG_FILE.exists():
    CATALOG_FILE.write_text(json.dumps(DEFAULT_CATALOG, indent=2) + "\n", encoding="utf-8")

LOCK = threading.Lock()
JOBS = {}
SEEDS = {}
PROCESSES = {}



AGENT_GUIDE_TEXT = """# VM Lab recipe guide

You are working inside the VM Lab module data directory. Create reusable VM
recipes as JSON files in `recipes/`. Do not edit the module source.

## Recipe schema

```json
{
  "id": "my-vm",
  "name": "My VM",
  "description": "What this VM is for.",
  "image": "ubuntu-24.04",
  "cpus": 4,
  "memory_mb": 8192,
  "display": "gui",
  "disk_gb": 30,
  "cloud_init": "#cloud-config\\npackages: [git, curl]\\n",
  "provision": ["sudo apt-get update", "sudo apt-get install -y ripgrep"]
}
```

`image` may be an existing catalog id, or an object:

```json
{
  "id": "my-custom-image",
  "name": "Custom Linux image",
  "os": "linux",
  "arch": "x86_64",
  "kind": "cloud",
  "url": "https://example.invalid/image.qcow2",
  "file": "image.qcow2",
  "path": "",
  "user": "ubuntu",
  "disk_gb": 30,
  "uefi": false
}
```

- `kind` is `cloud`, `iso`, or `disk`.
- `path` is relative to this data directory or absolute.
- `url` downloads into `images/<image-id>/<file>` on first create.
- `user` is the SSH user in Linux cloud images.
- `cloud_init` is extra cloud-config text appended to the generated user-data.
  It only applies to cloud images.
- `provision` commands run over SSH after a cloud VM boots. The cloud user has
  passwordless sudo. Keep them idempotent.
- `display` is `gui` or `headless`.
- macOS guests are only valid on a macOS host and require a prepared disk.

Do not invent fields. Validate that any local image path exists. You may
download images into `images/` and reference them from a recipe. After writing
a recipe, tell the user its display name.
"""


def ensure_agent_guide():
    RECIPES.mkdir(parents=True, exist_ok=True)
    if not AGENT_GUIDE.exists():
        AGENT_GUIDE.write_text(AGENT_GUIDE_TEXT, encoding="utf-8")


def load_recipes():
    ensure_agent_guide()
    recipes = []
    for path in sorted(RECIPES.glob("*.json")):
        try:
            recipe = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(recipe, dict):
            continue
        recipe = dict(recipe)
        recipe.setdefault("id", path.stem)
        if not NAME_RE.match(str(recipe.get("id") or "")):
            continue
        if not recipe.get("name") or not recipe.get("image"):
            continue
        recipes.append(recipe)
    return recipes


def recipe_by_id(recipe_id):
    for recipe in load_recipes():
        if str(recipe.get("id")) == str(recipe_id):
            return recipe
    raise ValueError(f"unknown VM recipe: {recipe_id}")


def recipe_image(recipe):
    raw = recipe.get("image")
    if isinstance(raw, str):
        return catalog_item(raw)
    if not isinstance(raw, dict):
        raise ValueError("recipe image must be a catalog id or object")
    item = dict(raw)
    if item.get("id"):
        try:
            item = {**catalog_item(str(item["id"])), **item}
        except ValueError:
            pass
    item.setdefault("id", str(recipe.get("id") or "custom"))
    item.setdefault("name", str(recipe.get("name") or item["id"]))
    item.setdefault("os", "linux")
    item.setdefault("arch", host_arch())
    item.setdefault("kind", "cloud")
    item.setdefault("cpus", recipe.get("cpus") or 2)
    item.setdefault("memory_mb", recipe.get("memory_mb") or 4096)
    item.setdefault("disk_gb", recipe.get("disk_gb") or 20)
    return item


def recipe_view(recipe):
    image = None
    try:
        image = recipe_image(recipe)
    except Exception:
        pass
    return {
        "id": recipe.get("id"),
        "name": recipe.get("name"),
        "description": recipe.get("description") or "",
        "image": (image or {}).get("name") or (image or {}).get("id") or str(recipe.get("image")),
        "os": (image or {}).get("os") or recipe.get("os") or "linux",
        "cpus": recipe.get("cpus") or (image or {}).get("cpus"),
        "memory_mb": recipe.get("memory_mb") or (image or {}).get("memory_mb"),
        "display": recipe.get("display") or "gui",
        "disk_gb": recipe.get("disk_gb") or (image or {}).get("disk_gb"),
        "has_cloud_init": bool(recipe.get("cloud_init")),
        "provision_count": len(recipe.get("provision") or []),
    }


def host_os():
    system = platform.system()
    return "windows" if system == "Windows" else "macos" if system == "Darwin" else "linux"


def host_arch():
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "aarch64"
    return "x86_64"


def config_value(key, default=None):
    value = CONFIG.get(key)
    return default if value in (None, "") else value


def find_binary(value, names):
    if value:
        path = Path(str(value)).expanduser()
        if path.is_file():
            return str(path)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def qemu_img():
    return find_binary(config_value("qemu_img_path"), ["qemu-img"])


def qemu_for(arch):
    names = ["qemu-system-aarch64"] if arch == "aarch64" else ["qemu-system-x86_64"]
    return find_binary(config_value("qemu_path"), names)


def find_uefi():
    if host_os() == "windows":
        roots = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "qemu" / "share"]
    elif host_os() == "macos":
        roots = [Path("/opt/homebrew/share/qemu"), Path("/usr/local/share/qemu")]
    else:
        roots = [Path("/usr/share/edk2/x64"), Path("/usr/share/OVMF"), Path("/usr/share/qemu")]
    candidates = [
        ("edk2-x86_64-code.fd", "edk2-i386-vars.fd"),
        ("OVMF_CODE.4m.fd", "OVMF_VARS.4m.fd"),
        ("OVMF_CODE.fd", "OVMF_VARS.fd"),
        ("ovmf-code.fd", "ovmf-vars.fd"),
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for code_name, vars_name in candidates:
            code = root / code_name
            vars_path = root / vars_name
            if code.is_file() and vars_path.is_file():
                return code, vars_path
    return None, None


def acceleration(guest_arch):
    host = host_os()
    arch = host_arch()
    if host == "linux" and Path("/dev/kvm").exists() and arch == guest_arch:
        return ["-accel", "kvm", "-cpu", "host"]
    if host == "macos" and arch == guest_arch:
        return ["-accel", "hvf", "-cpu", "host"]
    if host == "windows" and arch == guest_arch:
        return ["-accel", "whpx"]
    return ["-accel", "tcg"]


def capabilities():
    qemu = qemu_for(host_arch())
    img = qemu_img()
    ssh = shutil.which("ssh")
    keygen = shutil.which("ssh-keygen")
    code, vars_path = find_uefi()
    return {
        "platform": host_os(),
        "arch": host_arch(),
        "qemu": qemu,
        "qemu_img": img,
        "ssh": ssh,
        "ssh_keygen": keygen,
        "uefi": bool(code and vars_path),
        "uefi_code": str(code) if code else None,
        "macos_guest_supported": host_os() == "macos",
        "ready": bool(qemu and img and keygen),
        "missing": [name for name, value in (("qemu-system", qemu), ("qemu-img", img), ("ssh-keygen", keygen)) if not value],
    }


def load_catalog():
    try:
        data = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        items = [item for item in data if isinstance(item, dict) and item.get("id")]
    except (OSError, ValueError):
        items = list(DEFAULT_CATALOG)
    known = {str(item.get("id")) for item in items}
    for item in DEFAULT_CATALOG:
        if str(item.get("id")) not in known:
            items.append(item)
    return items


def catalog_item(image_id):
    for item in load_catalog():
        if str(item.get("id")) == image_id:
            return item
    raise ValueError(f"unknown image: {image_id}")


def catalog_view():
    host = host_os()
    result = []
    for item in load_catalog():
        entry = dict(item)
        entry["available"] = not item.get("requires_path") or bool(item.get("path"))
        entry["host_compatible"] = not item.get("host_platform") or item.get("host_platform") == platform.system()
        if item.get("kind") == "cloud" and item.get("url") and item.get("file"):
            path = IMAGES / str(item["id"]) / str(item["file"])
            entry["cached"] = path.is_file()
            entry["cache_path"] = str(path)
        else:
            path = Path(str(item.get("path") or "")).expanduser() if item.get("path") else None
            entry["cached"] = bool(path and path.is_file())
        if item.get("os") == "macos" and host != "macos":
            entry["host_compatible"] = False
        result.append(entry)
    return result


def vm_dir(name):
    if not NAME_RE.match(name or ""):
        raise ValueError("invalid VM name")
    return VMS / name


def meta_path(name):
    return vm_dir(name) / "meta.json"


def read_meta(name):
    path = meta_path(name)
    if not path.is_file():
        raise ValueError(f"unknown VM: {name}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_meta(name, meta):
    path = meta_path(name)
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            status = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
            ctypes.windll.kernel32.CloseHandle(handle)
            return status == 0x102
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def running(meta):
    return pid_alive(meta.get("pid"))


def free_port(start):
    port = int(start)
    while port < 65535:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                port += 1
    raise RuntimeError("no free port")


def qemu_img_info(path):
    tool = qemu_img()
    if not tool:
        raise RuntimeError("qemu-img is not installed")
    result = subprocess.run([tool, "info", "--output=json", str(path)], capture_output=True, text=True, timeout=20)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "qemu-img info failed").strip())
    return json.loads(result.stdout)


def image_source(item, job_log):
    path_value = str(item.get("path") or "").strip()
    if path_value:
        path = Path(path_value).expanduser()
        if not path.is_absolute():
            path = DATA / path
        if not path.is_file():
            raise RuntimeError(f"image path does not exist: {path}")
        return path
    if item.get("url"):
        target = IMAGES / str(item["id"]) / str(item.get("file") or "image.qcow2")
        if target.is_file() and target.stat().st_size > 0:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        job_log(f"Downloading {item.get('name') or item['id']}…")
        request = urllib.request.Request(str(item["url"]), headers={"User-Agent": "Orqella-VM-Lab/0.1"})
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if total:
                    job_log(f"{target.name}: {done * 100 // total}%")
        return target
    raise RuntimeError("this catalog image needs a local path or URL")


def start_seed(name, meta):
    seed_dir = vm_dir(name) / "seed"
    user_data = seed_dir / "user-data"
    meta_data = seed_dir / "meta-data"
    if not user_data.is_file():
        return None
    port = int(meta.get("seed_port") or 0) or free_port(18080)
    meta["seed_port"] = port
    write_meta(name, meta)

    class SeedHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            filename = self.path.split("?", 1)[0].lstrip("/")
            path = seed_dir / filename
            if filename not in {"user-data", "meta-data", "vendor-data"} or not path.is_file():
                self.send_response(404)
                self.end_headers()
                return
            payload = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/yaml")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", port), SeedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    SEEDS[name] = (server, thread)
    return port


def stop_seed(name):
    entry = SEEDS.pop(name, None)
    if entry:
        server, _ = entry
        try:
            server.shutdown()
            server.server_close()
        except OSError:
            pass


def qmp(port, command, timeout=5):
    with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as sock:
        stream = sock.makefile("rwb", buffering=0)
        greeting = stream.readline()
        if not greeting:
            raise RuntimeError("QMP did not greet")
        stream.write(json.dumps({"execute": "qmp_capabilities"}).encode() + b"\n")
        stream.readline()
        stream.write(json.dumps({"execute": command}).encode() + b"\n")
        return stream.readline().decode(errors="replace")


def stop_vm(name, force=False):
    meta = read_meta(name)
    process = PROCESSES.get(name)

    def alive():
        # Popen.poll() reaps an exited child. os.kill(pid, 0) keeps returning
        # success for a zombie, which made every stop wait out the full grace
        # period after QEMU had already exited.
        if process is not None:
            return process.poll() is None
        return pid_alive(meta.get("pid"))

    if not alive():
        PROCESSES.pop(name, None)
        stop_seed(name)
        meta["pid"] = None
        write_meta(name, meta)
        return {"ok": True, "name": name, "state": "stopped"}
    if not force:
        requested = False
        try:
            qmp(meta.get("qmp_port"), "system_powerdown")
            requested = True
        except Exception:
            pass
        if requested:
            deadline = time.time() + POWERDOWN_GRACE_SECS
            while time.time() < deadline:
                if not alive():
                    break
                time.sleep(0.4)
    if alive():
        try:
            qmp(meta.get("qmp_port"), "quit")
        except Exception:
            pass
        deadline = time.time() + 2
        while time.time() < deadline:
            if not alive():
                break
            time.sleep(0.1)
    if alive():
        try:
            os.kill(int(meta["pid"]), 9 if os.name != "nt" else 15)
        except OSError:
            pass
        for _ in range(20):
            if not alive():
                break
            time.sleep(0.1)
    process = PROCESSES.pop(name, None)
    if process is not None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
    stop_seed(name)
    meta["pid"] = None
    write_meta(name, meta)
    return {"ok": True, "name": name, "state": "stopped"}


def qemu_args(name, meta):
    qemu = qemu_for(meta.get("arch") or host_arch())
    if not qemu:
        raise RuntimeError("QEMU is not installed for this guest architecture")
    disk = vm_dir(name) / "disk.qcow2"
    args = [qemu, "-name", f"VM Lab: {name}"]
    machine = "q35" if (meta.get("arch") or "x86_64") == "x86_64" else "virt"
    args += ["-machine", machine]
    args += acceleration(meta.get("arch") or host_arch())
    windows = meta.get("os") == "windows"
    if windows:
        # Windows Setup does not include virtio-blk or virtio-net drivers.
        disk_args = ["-drive", f"file={disk},if=none,id=disk0,format=qcow2", "-device", "ide-hd,drive=disk0"]
        net_device = "e1000"
    else:
        disk_args = ["-drive", f"file={disk},if=virtio,format=qcow2"]
        net_device = "virtio-net-pci"
    args += [
        "-smp", str(int(meta.get("cpus") or 2)),
        "-m", str(int(meta.get("memory_mb") or 4096)),
        *disk_args,
        "-netdev", f"user,id=net0,hostfwd=tcp:127.0.0.1:{meta['ssh_port']}-:22",
        "-device", f"{net_device},netdev=net0",
        "-vnc", f"127.0.0.1:{int(meta['vnc_port']) - 5900}",
        "-qmp", f"tcp:127.0.0.1:{meta['qmp_port']},server=on,wait=off",
        "-pidfile", str(vm_dir(name) / "qemu.pid"),
        "-device", "qemu-xhci",
        "-device", "usb-tablet",
    ]
    if meta.get("display") == "headless":
        args += ["-display", "none"]
    if meta.get("uefi"):
        code, vars_template = find_uefi()
        if not code or not vars_template:
            raise RuntimeError("UEFI firmware was requested but OVMF/edk2 was not found")
        vars_path = vm_dir(name) / "OVMF_VARS.fd"
        if not vars_path.is_file():
            shutil.copy2(vars_template, vars_path)
        args += [
            "-drive", f"if=pflash,format=raw,readonly=on,file={code}",
            "-drive", f"if=pflash,format=raw,file={vars_path}",
        ]
    if meta.get("kind") == "iso" and meta.get("iso") and not meta.get("installed"):
        args += ["-cdrom", str(meta["iso"]), "-boot", "order=d"]
    if meta.get("unattend") and (vm_dir(name) / "unattend" / "autounattend.img").is_file():
        args += ["-drive", f"file={vm_dir(name) / 'unattend' / 'autounattend.img'},if=floppy,format=raw"]
    if meta.get("kind") == "cloud" and meta.get("seed_port"):
        args += ["-smbios", f"type=1,serial=ds=nocloud-net;s=http://10.0.2.2:{meta['seed_port']}/"]
    return args


def start_vm(name, force_display=None):
    meta = read_meta(name)
    if running(meta):
        return {"ok": True, "name": name, "state": "running"}
    if not qemu_img():
        raise RuntimeError("qemu-img is not installed")
    base = meta.get("base_image")
    if meta.get("kind") != "iso" and base and not Path(str(base)).is_file():
        raise RuntimeError(f"base image is missing: {base}")
    meta["ssh_port"] = int(meta.get("ssh_port") or free_port(22000))
    meta["vnc_port"] = int(meta.get("vnc_port") or free_port(5910))
    meta["qmp_port"] = int(meta.get("qmp_port") or free_port(44400))
    if force_display:
        meta["display"] = force_display
    if meta.get("kind") == "cloud":
        start_seed(name, meta)
    args = qemu_args(name, meta)
    log_path = vm_dir(name) / "qemu.log"
    log = log_path.open("ab")
    env = os.environ.copy()
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        args,
        cwd=str(vm_dir(name)),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=(os.name != "nt"),
        creationflags=creationflags,
    )
    PROCESSES[name] = process
    meta["pid"] = process.pid
    meta["started_at"] = int(time.time())
    write_meta(name, meta)
    time.sleep(1.2)
    if process.poll() is not None:
        stop_seed(name)
        tail = log_path.read_text(errors="replace")[-3000:] if log_path.exists() else ""
        raise RuntimeError(f"QEMU exited during startup\n{tail}")
    return {"ok": True, "name": name, "state": "running", "pid": process.pid}


def make_key(name):
    key = vm_dir(name) / "id_ed25519"
    if key.is_file():
        return key
    result = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"orqella-vm:{name}", "-f", str(key)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "ssh-keygen failed").strip())
    return key


def write_seed(name, image, key, cloud_init_extra=""):
    seed = vm_dir(name) / "seed"
    seed.mkdir(exist_ok=True)
    public = (key.with_suffix(".pub")).read_text(encoding="utf-8").strip() if key.with_suffix(".pub").is_file() else ""
    user = str(image.get("user") or "ubuntu")
    meta = f"instance-id: {uuidlib.uuid4()}\nlocal-hostname: {name}\n"
    user_data = {
        "#cloud-config": "",
        "users": [{
            "name": user,
            "sudo": "ALL=(ALL) NOPASSWD:ALL",
            "shell": "/bin/bash",
            "ssh_authorized_keys": [public] if public else [],
        }],
        "ssh_pwauth": False,
        "disable_root": True,
        "package_update": False,
    }
    user_data_text = "#cloud-config\n" + "\n".join(
        f"{k}: {json.dumps(v)}" if k != "#cloud-config" else "#cloud-config"
        for k, v in user_data.items()
    ) + "\n"
    extra = str(cloud_init_extra or "").strip()
    if extra:
        if extra.startswith("#cloud-config"):
            extra = extra.split("\n", 1)[1] if "\n" in extra else ""
        user_data_text += "\n" + extra + "\n"
    (seed / "meta-data").write_text(meta, encoding="utf-8")
    (seed / "user-data").write_text(user_data_text, encoding="utf-8")



def _fat12_set(fat: bytearray, cluster: int, value: int) -> None:
    offset = cluster + cluster // 2
    if cluster & 1:
        fat[offset] = (fat[offset] & 0x0F) | ((value << 4) & 0xF0)
        fat[offset + 1] = (value >> 4) & 0xFF
    else:
        fat[offset] = value & 0xFF
        fat[offset + 1] = (fat[offset + 1] & 0xF0) | ((value >> 8) & 0x0F)


def _fat_lfn_entries(name: str, short: bytes) -> list[bytes]:
    checksum = 0
    for byte in short:
        checksum = ((checksum & 1) << 7) + (checksum >> 1) + byte
        checksum &= 0xFF
    raw = name.encode("utf-16le") + b"\x00\x00"
    while len(raw) % 26:
        raw += b"\xff"
    chunks = [raw[index:index + 26] for index in range(0, len(raw), 26)]
    entries = []
    for order in range(len(chunks), 0, -1):
        chunk = chunks[order - 1]
        entry = bytearray(32)
        entry[0] = order | (0x40 if order == len(chunks) else 0)
        entry[1:11] = chunk[0:10]
        entry[11] = 0x0F
        entry[13] = checksum
        entry[14:26] = chunk[10:22]
        entry[28:32] = chunk[22:26]
        entries.append(bytes(entry))
    return entries


def _fat_short_name(name: str) -> bytes:
    base, _, ext = name.upper().partition(".")
    base = re.sub(r"[^A-Z0-9]", "", base)[:8]
    ext = re.sub(r"[^A-Z0-9]", "", ext)[:3]
    return (base[:6] + "~1").ljust(8)[:8].encode("ascii") + ext.ljust(3).encode("ascii")


def make_floppy(path: Path, filename: str, content: bytes) -> None:
    """Create a 1.44 MB FAT12 floppy containing one file."""
    image = bytearray(1440 * 1024)
    sector = 512
    total_sectors = 2880
    fat_sectors = 9
    root_sectors = 14
    root_sector = 1 + 2 * fat_sectors
    data_sector = root_sector + root_sectors
    clusters = max(1, (len(content) + sector - 1) // sector)
    if 2 + clusters > 2847:
        raise ValueError("unattend file is too large for a floppy image")
    boot = bytearray(sector)
    boot[0:3] = b"\xEB\x3C\x90"
    boot[3:11] = b"MSWIN4.1"
    struct.pack_into("<H", boot, 11, sector)
    boot[13] = 1
    struct.pack_into("<H", boot, 14, 1)
    boot[16] = 2
    struct.pack_into("<H", boot, 17, 224)
    struct.pack_into("<H", boot, 19, total_sectors)
    boot[21] = 0xF0
    struct.pack_into("<H", boot, 22, fat_sectors)
    struct.pack_into("<H", boot, 24, 18)
    struct.pack_into("<H", boot, 26, 2)
    boot[36] = 0x00
    boot[38] = 0x29
    struct.pack_into("<I", boot, 39, 0x4F524745)
    boot[43:54] = b"ORQELLA    "
    boot[54:62] = b"FAT12   "
    boot[510:512] = b"\x55\xAA"
    image[0:sector] = boot
    fat = bytearray(fat_sectors * sector)
    fat[0:3] = b"\xF0\xFF\xFF"
    for cluster in range(2, 2 + clusters):
        _fat12_set(fat, cluster, 0x0FFF if cluster == 1 + clusters else cluster + 1)
    image[sector:sector + len(fat)] = fat
    fat2 = (1 + fat_sectors) * sector
    image[fat2:fat2 + len(fat)] = fat
    short = _fat_short_name(filename)
    root = bytearray(root_sectors * sector)
    entries = _fat_lfn_entries(filename, short)
    offset = 0
    for entry in entries:
        root[offset:offset + 32] = entry
        offset += 32
    short_entry = bytearray(32)
    short_entry[0:11] = short
    short_entry[11] = 0x20
    struct.pack_into("<H", short_entry, 26, 2)
    struct.pack_into("<I", short_entry, 28, len(content))
    root[offset:offset + 32] = short_entry
    image[root_sector * sector:(root_sector + root_sectors) * sector] = root
    data_offset = data_sector * sector
    image[data_offset:data_offset + len(content)] = content
    path.write_bytes(image)


def windows_unattend_xml(name: str, uefi: bool, user: str, password: str) -> str:
    from xml.sax.saxutils import escape

    if uefi:
        partitions = '''<CreatePartition wcm:action="add"><Order>1</Order><Type>EFI</Type><Size>100</Size></CreatePartition>
          <CreatePartition wcm:action="add"><Order>2</Order><Type>MSR</Type><Size>16</Size></CreatePartition>
          <CreatePartition wcm:action="add"><Order>3</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>'''
        modify = '''<ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>FAT32</Format><Label>System</Label></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>3</Order><PartitionID>3</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>'''
        install_partition = 3
    else:
        partitions = '''<CreatePartition wcm:action="add"><Order>1</Order><Type>Primary</Type><Size>500</Size></CreatePartition>
          <CreatePartition wcm:action="add"><Order>2</Order><Type>Primary</Type><Extend>true</Extend></CreatePartition>'''
        modify = '''<ModifyPartition wcm:action="add"><Order>1</Order><PartitionID>1</PartitionID><Format>NTFS</Format><Label>System</Label><Active>true</Active></ModifyPartition>
            <ModifyPartition wcm:action="add"><Order>2</Order><PartitionID>2</PartitionID><Format>NTFS</Format><Label>Windows</Label><Letter>C</Letter></ModifyPartition>'''
        install_partition = 2
    computer = re.sub(r"[^A-Za-z0-9-]", "-", name)[:15] or "ORQELLA-WIN"
    user = escape(user)
    password = escape(password)
    ssh = escape(
        'powershell -NoProfile -ExecutionPolicy Bypass -Command "'
        'Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; '
        'Set-Service sshd -StartupType Automatic; Start-Service sshd; '
        'New-NetFirewallRule -Name OrqellaSSH -DisplayName OrqellaSSH -Enabled True '
        '-Direction Inbound -Protocol TCP -Action Allow -LocalPort 22"'
    )
    return f'''<?xml version="1.0" encoding="utf-8"?>
<unattend xmlns="urn:schemas-microsoft-com:unattend">
  <settings pass="windowsPE">
    <component name="Microsoft-Windows-International-Core-WinPE" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <SetupUILanguage><UILanguage>en-US</UILanguage></SetupUILanguage>
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <RunSynchronous>
        <RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassTPMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>2</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassSecureBootCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>3</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassRAMCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
        <RunSynchronousCommand wcm:action="add"><Order>4</Order><Path>reg add HKLM\\SYSTEM\\Setup\\LabConfig /v BypassCPUCheck /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand>
      </RunSynchronous>
      <DiskConfiguration>
        <Disk wcm:action="add"><DiskID>0</DiskID><WillWipeDisk>true</WillWipeDisk>
          <CreatePartitions>{partitions}</CreatePartitions>
          <ModifyPartitions>{modify}</ModifyPartitions>
        </Disk>
      </DiskConfiguration>
      <ImageInstall><OSImage><InstallFrom><MetaData wcm:action="add"><Key>/IMAGE/INDEX</Key><Value>1</Value></MetaData></InstallFrom><InstallTo><DiskID>0</DiskID><PartitionID>{install_partition}</PartitionID></InstallTo></OSImage></ImageInstall>
      <UserData><AcceptEula>true</AcceptEula><FullName>Orqella</FullName><Organization>Orqella</Organization></UserData>
    </component>
  </settings>
  <settings pass="specialize">
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <ComputerName>{computer}</ComputerName><TimeZone>UTC</TimeZone>
    </component>
    <component name="Microsoft-Windows-Deployment" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <RunSynchronous><RunSynchronousCommand wcm:action="add"><Order>1</Order><Path>reg add HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\OOBE /v BypassNRO /t REG_DWORD /d 1 /f</Path></RunSynchronousCommand></RunSynchronous>
    </component>
  </settings>
  <settings pass="oobeSystem">
    <component name="Microsoft-Windows-International-Core" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <InputLocale>en-US</InputLocale><SystemLocale>en-US</SystemLocale><UILanguage>en-US</UILanguage><UserLocale>en-US</UserLocale>
    </component>
    <component name="Microsoft-Windows-Shell-Setup" processorArchitecture="amd64" publicKeyToken="31bf3856ad364e35" language="neutral" versionScope="nonSxS" xmlns:wcm="http://schemas.microsoft.com/WMIConfig/2002/State" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <OOBE><HideEULAPage>true</HideEULAPage><HideLocalAccountScreen>true</HideLocalAccountScreen><HideOEMRegistrationScreen>true</HideOEMRegistrationScreen><HideOnlineAccountScreens>true</HideOnlineAccountScreens><HideWirelessSetupInOOBE>true</HideWirelessSetupInOOBE><NetworkLocation>Work</NetworkLocation><ProtectYourPC>3</ProtectYourPC></OOBE>
      <UserAccounts><LocalAccounts><LocalAccount wcm:action="add"><Password><Value>{password}</Value><PlainText>true</PlainText></Password><Description>Orqella</Description><DisplayName>{user}</DisplayName><Group>Administrators</Group><Name>{user}</Name></LocalAccount></LocalAccounts></UserAccounts>
      <AutoLogon><Password><Value>{password}</Value><PlainText>true</PlainText></Password><Enabled>true</Enabled><LogonCount>5</LogonCount><Username>{user}</Username></AutoLogon>
      <FirstLogonCommands><SynchronousCommand wcm:action="add"><Order>1</Order><CommandLine>{ssh}</CommandLine><Description>Enable OpenSSH</Description></SynchronousCommand></FirstLogonCommands>
    </component>
  </settings>
</unattend>
'''


def write_windows_unattend(name: str, meta: dict, user: str, password: str) -> None:
    directory = vm_dir(name) / "unattend"
    directory.mkdir(parents=True, exist_ok=True)
    xml = windows_unattend_xml(name, bool(meta.get("uefi")), user, password)
    (directory / "autounattend.xml").write_text(xml, encoding="utf-8")
    make_floppy(directory / "autounattend.img", "AUTOUNATTEND.XML", xml.encode("utf-8"))

def prepare_disk(name, image, base_path, job_log):
    tool = qemu_img()
    disk = vm_dir(name) / "disk.qcow2"
    if image.get("kind") == "iso":
        job_log(f"Creating {image.get('disk_gb', 40)} GB disk…")
        subprocess.run([tool, "create", "-f", "qcow2", str(disk), f"{int(image.get('disk_gb') or 40)}G"], check=True, capture_output=True)
        return
    if not base_path:
        raise RuntimeError("this image has no base disk")
    fmt = str(image.get("format") or "")
    if not fmt:
        try:
            fmt = str(qemu_img_info(base_path).get("format") or "qcow2")
        except Exception:
            fmt = "qcow2"
    job_log(f"Creating overlay from {Path(base_path).name}…")
    subprocess.run([tool, "create", "-f", "qcow2", "-b", str(base_path), "-F", fmt, str(disk)], check=True, capture_output=True)



def ssh_base(meta):
    directory = vm_dir(str(meta.get("name")))
    return [
        shutil.which("ssh") or "ssh", "-i", str(directory / "id_ed25519"),
        "-p", str(meta.get("ssh_port")),
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={directory / 'known_hosts'}",
        f"{meta.get('guest_user')}@127.0.0.1",
    ]


def run_provision(meta, commands, log):
    if not commands:
        return
    if not meta.get("guest_user") or not shutil.which("ssh"):
        log("Provisioning skipped: this image has no SSH user.")
        return
    deadline = time.time() + 180
    while time.time() < deadline:
        result = subprocess.run([*ssh_base(meta), "true"], capture_output=True, text=True, timeout=15)
        if result.returncode == 0:
            break
        time.sleep(2)
    else:
        log("Provisioning skipped: SSH did not become ready.")
        return
    for command in commands:
        command = str(command)
        log(f"Provision: {command}")
        result = subprocess.run([*ssh_base(meta), command], capture_output=True, text=True, timeout=300)
        output = (result.stdout or "") + (result.stderr or "")
        if output.strip():
            log(output.strip()[-2000:])
        if result.returncode != 0:
            raise RuntimeError(f"provision command failed: {command}")


def create_vm(name, payload, job):
    if not NAME_RE.match(name or ""):
        raise ValueError("invalid VM name")
    if (VMS / name).exists():
        raise ValueError(f"VM already exists: {name}")
    recipe = None
    recipe_id = str(payload.get("recipe") or "").strip()
    if recipe_id:
        recipe = recipe_by_id(recipe_id)
        image = recipe_image(recipe)
    else:
        image = catalog_item(str(payload.get("image") or ""))
    if recipe:
        image = {
            **image,
            "cpus": recipe.get("cpus") or image.get("cpus"),
            "memory_mb": recipe.get("memory_mb") or image.get("memory_mb"),
            "disk_gb": recipe.get("disk_gb") or image.get("disk_gb"),
        }
    if image.get("os") == "macos" and host_os() != "macos":
        raise ValueError("macOS guests can only be created on a macOS host")
    if image.get("host_platform") and image.get("host_platform") != platform.system():
        raise ValueError(f"this image requires a {image['host_platform']} host")
    cpus = int(payload.get("cpus") or (recipe or {}).get("cpus") or image.get("cpus") or config_value("default_cpus", 2))
    memory = int(payload.get("memory_mb") or (recipe or {}).get("memory_mb") or image.get("memory_mb") or config_value("default_memory_mb", 4096))
    if not 1 <= cpus <= 64 or not 512 <= memory <= 262144:
        raise ValueError("invalid CPU or memory value")
    display = str(payload.get("display") or (recipe or {}).get("display") or config_value("default_display", "gui"))
    if display not in {"gui", "headless"}:
        raise ValueError("display must be gui or headless")
    path_override = str(payload.get("path") or "").strip()
    if not recipe and path_override and image.get("kind") in {"iso", "disk"}:
        image = dict(image)
        image["path"] = path_override
    unattend = bool(payload.get("unattend"))
    unattend_user = str(payload.get("windows_user") or "orqella").strip() or "orqella"
    unattend_password = str(payload.get("windows_password") or "orqella")
    job["status"] = "running"
    job["log"] = ""

    def log(line):
        with LOCK:
            job["log"] = (job.get("log", "") + str(line) + "\n")[-12000:]

    try:
        base_path = image_source(image, log)
        directory = vm_dir(name)
        directory.mkdir(parents=True)
        key = make_key(name)
        prepare_disk(name, image, base_path, log)
        ssh_port = free_port(22000)
        vnc_port = free_port(5910)
        meta = {
            "name": name,
            "image": image.get("id"),
            "image_name": image.get("name"),
            "os": image.get("os") or "linux",
            "arch": image.get("arch") or host_arch(),
            "kind": image.get("kind") or "disk",
            "base_image": str(base_path) if image.get("kind") != "iso" else None,
            "iso": str(base_path) if image.get("kind") == "iso" else None,
            "guest_user": image.get("user") or "",
            "cpus": cpus,
            "memory_mb": memory,
            "display": display,
            "recipe": recipe_id or None,
            "recipe_name": recipe.get("name") if recipe else None,
            "disk_gb": int((recipe or {}).get("disk_gb") or image.get("disk_gb") or 40),
            "uefi": bool(image.get("uefi")),
            "ssh_port": ssh_port,
            "vnc_port": vnc_port,
            "qmp_port": free_port(44400),
            "uuid": str(uuidlib.uuid4()),
            "created": int(time.time()),
            "pid": None,
            "installed": image.get("kind") != "iso",
            "unattend": False,
            "unattend_user": "",
            "unattend_password": "",
        }
        if unattend:
            meta["unattend"] = True
            meta["unattend_user"] = unattend_user
            meta["unattend_password"] = unattend_password
            if not meta.get("guest_user"):
                meta["guest_user"] = unattend_user
            write_windows_unattend(name, meta, unattend_user, unattend_password)
            log(f"Unattended install configured for {unattend_user} (password: {unattend_password}).")
        write_meta(name, meta)
        if meta["kind"] == "iso":
            log("Installer ISO attached. Install through the web console, then use Finish install before rebooting.")
        if meta["kind"] == "cloud":
            write_seed(name, image, key, (recipe or {}).get("cloud_init") or "")
        if payload.get("start", True):
            log("Starting QEMU…")
            start_vm(name)
            run_provision(meta, (recipe or {}).get("provision") or [], log)
        with LOCK:
            job["status"] = "succeeded"
            job["vm"] = name
        log("VM ready.")
    except Exception as exc:
        with LOCK:
            job["status"] = "failed"
            job["error"] = str(exc)
        log(f"Failed: {exc}")


def list_vms():
    result = []
    for directory in sorted(VMS.iterdir()) if VMS.is_dir() else []:
        if not directory.is_dir():
            continue
        try:
            meta = read_meta(directory.name)
        except Exception:
            continue
        alive = running(meta)
        disk = directory / "disk.qcow2"
        result.append({
            "name": meta.get("name") or directory.name,
            "image": meta.get("image_name") or meta.get("image"),
            "os": meta.get("os"),
            "arch": meta.get("arch"),
            "kind": meta.get("kind"),
            "display": meta.get("display"),
            "cpus": meta.get("cpus"),
            "memory_mb": meta.get("memory_mb"),
            "running": alive,
            "installed": bool(meta.get("installed", True)),
            "unattend": bool(meta.get("unattend", False)),
            "unattend_user": meta.get("unattend_user") or "",
            "uuid": meta.get("uuid"),
            "created": meta.get("created"),
            "ssh_port": meta.get("ssh_port"),
            "vnc_port": meta.get("vnc_port"),
            "ssh_command": ssh_command(meta) if alive else "",
            "disk_bytes": disk.stat().st_size if disk.is_file() else 0,
        })
    return result


def ssh_command(meta):
    if not meta.get("guest_user"):
        return ""
    directory = vm_dir(str(meta.get("name")))
    return (
        f"ssh -i {directory / 'id_ed25519'} -p {meta.get('ssh_port')} "
        f"-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new "
        f"-o UserKnownHostsFile={directory / 'known_hosts'} "
        f"{meta.get('guest_user')}@127.0.0.1"
    )


def action_vm(name, action):
    meta = read_meta(name)
    if action == "start":
        return start_vm(name)
    if action == "stop":
        return stop_vm(name)
    if action == "force-stop":
        return stop_vm(name, force=True)
    if action == "destroy":
        stop_vm(name, force=True)
        shutil.rmtree(vm_dir(name), ignore_errors=True)
        return {"ok": True, "name": name, "state": "destroyed"}
    if action == "reset":
        stop_vm(name, force=True)
        disk = vm_dir(name) / "disk.qcow2"
        if disk.exists():
            disk.unlink()
        if meta.get("kind") == "iso":
            subprocess.run([qemu_img(), "create", "-f", "qcow2", str(disk), f"{int(meta.get('disk_gb') or 40)}G"], check=True, capture_output=True)
        else:
            image = catalog_item(str(meta.get("image")))
            prepare_disk(name, image, meta.get("base_image"), lambda line: None)
        meta["uuid"] = str(uuidlib.uuid4())
        meta["installed"] = meta.get("kind") != "iso"
        write_meta(name, meta)
        return start_vm(name)
    if action == "eject":
        stop_vm(name)
        meta["installed"] = True
        meta["unattend"] = False
        write_meta(name, meta)
        return start_vm(name)
    raise ValueError(f"unknown action: {action}")


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_frame(opcode, payload):
    header = bytearray([0x80 | opcode])
    size = len(payload)
    if size < 126:
        header.append(size)
    elif size < 65536:
        header.append(126)
        header += struct.pack("!H", size)
    else:
        header.append(127)
        header += struct.pack("!Q", size)
    return bytes(header) + payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send_json(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self):
        if self.headers.get("X-Orqella-Module-Token") == TOKEN:
            return True
        self.send_json(401, {"error": "unauthorized"})
        return False

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/ws/console", "/ws/terminal"}:
            self.handle_ws(parsed)
            return
        if not self.authorized():
            return
        try:
            if parsed.path == "/health":
                return self.send_json(200, {"ok": True})
            if parsed.path == "/api/status":
                caps = capabilities()
                vms = list_vms()
                return self.send_json(200, {
                    **caps,
                    "data_root": str(DATA),
                    "counts": {
                        "total": len(vms),
                        "running": sum(1 for vm in vms if vm["running"]),
                    },
                })
            if parsed.path == "/api/images":
                return self.send_json(200, {"images": catalog_view(), "platform": host_os()})
            if parsed.path == "/api/recipes":
                return self.send_json(200, {"recipes": [recipe_view(recipe) for recipe in load_recipes()]})
            if parsed.path == "/api/vms":
                return self.send_json(200, {"vms": list_vms()})
            if parsed.path.startswith("/api/jobs/"):
                job = JOBS.get(parsed.path.rsplit("/", 1)[-1])
                return self.send_json(200, job) if job else self.send_json(404, {"error": "job not found"})
            return self.send_json(404, {"error": "not found"})
        except Exception as exc:
            return self.send_json(500, {"error": str(exc)})

    def do_POST(self):
        if not self.authorized():
            return
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/api/vms/create":
                name = str(payload.get("name") or "").strip()
                if not NAME_RE.match(name):
                    raise ValueError("invalid VM name")
                job_id = uuidlib.uuid4().hex
                job = {"id": job_id, "status": "queued", "log": "", "name": name}
                with LOCK:
                    JOBS[job_id] = job
                threading.Thread(target=create_vm, args=(name, payload, job), daemon=True).start()
                return self.send_json(202, job)
            if parsed.path == "/api/vms/action":
                name = str(payload.get("name") or "").strip()
                action = str(payload.get("action") or "").strip()
                return self.send_json(200, action_vm(name, action))
            return self.send_json(404, {"error": "not found"})
        except Exception as exc:
            return self.send_json(400, {"error": str(exc)})

    def handle_ws(self, parsed):
        if not self.authorized():
            return
        name = (parse_qs(parsed.query).get("name") or [""])[0]
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self.send_json(400, {"error": "websocket handshake required"})
        try:
            meta = read_meta(name)
        except Exception as exc:
            return self.send_json(404, {"error": str(exc)})
        if not running(meta):
            return self.send_json(503, {"error": "VM is not running"})
        if parsed.path == "/ws/console":
            port = int(meta.get("vnc_port") or 0)
            if not port:
                return self.send_json(503, {"error": "VM has no VNC console"})
            upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
            self.websocket_handshake(key)
            self.bridge_vnc(upstream)
        elif parsed.path == "/ws/terminal":
            self.websocket_handshake(key)
            self.bridge_terminal(meta)
        else:
            return self.send_json(404, {"error": "not found"})

    def websocket_handshake(self, key):
        self.close_connection = False
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", ws_accept(key))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True

    def ws_read(self):
        head = self.rfile.read(2)
        if len(head) < 2:
            return None, b""
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        size = head[1] & 0x7F
        if size == 126:
            size = struct.unpack("!H", self.rfile.read(2))[0]
        elif size == 127:
            size = struct.unpack("!Q", self.rfile.read(8))[0]
        if size > 4 * 1024 * 1024:
            raise ValueError("websocket frame too large")
        mask = self.rfile.read(4) if masked else b""
        data = self.rfile.read(size)
        if masked:
            data = bytes(byte ^ mask[i % 4] for i, byte in enumerate(data))
        return opcode, data

    def bridge_vnc(self, upstream):
        lock = threading.Lock()

        def send(opcode, payload):
            with lock:
                self.wfile.write(ws_frame(opcode, payload))
                self.wfile.flush()

        def downstream():
            try:
                while True:
                    data = upstream.recv(65536)
                    if not data:
                        break
                    send(0x2, data)
            except OSError:
                pass
            finally:
                try:
                    send(0x8, b"")
                except OSError:
                    pass

        thread = threading.Thread(target=downstream, daemon=True)
        thread.start()
        try:
            while True:
                opcode, data = self.ws_read()
                if opcode is None or opcode == 0x8:
                    break
                if opcode == 0x9:
                    send(0xA, data)
                elif opcode in {0x1, 0x2}:
                    upstream.sendall(data)
        except OSError:
            pass
        finally:
            try:
                upstream.shutdown(socket.SHUT_RDWR)
                upstream.close()
            except OSError:
                pass

    def bridge_terminal(self, meta):
        ssh = shutil.which("ssh")
        if not ssh or not meta.get("guest_user"):
            self.wfile.write(ws_frame(0x1, b"SSH terminal is not available for this VM.\r\n"))
            self.wfile.write(ws_frame(0x8, b""))
            return
        command = [
            ssh, "-tt",
            "-i", str(vm_dir(str(meta.get("name"))) / "id_ed25519"),
            "-p", str(meta.get("ssh_port")),
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={vm_dir(str(meta.get('name'))) / 'known_hosts'}",
            f"{meta.get('guest_user')}@127.0.0.1",
        ]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        lock = threading.Lock()

        def send(payload):
            with lock:
                self.wfile.write(ws_frame(0x2, payload))
                self.wfile.flush()

        def downstream():
            try:
                while True:
                    data = process.stdout.read(4096)
                    if not data:
                        break
                    send(data)
            except OSError:
                pass
            finally:
                try:
                    self.wfile.write(ws_frame(0x8, b""))
                    self.wfile.flush()
                except OSError:
                    pass

        threading.Thread(target=downstream, daemon=True).start()
        try:
            while True:
                opcode, data = self.ws_read()
                if opcode is None or opcode == 0x8:
                    break
                if opcode == 0x9:
                    with lock:
                        self.wfile.write(ws_frame(0xA, data))
                        self.wfile.flush()
                elif opcode in {0x1, 0x2} and process.stdin:
                    process.stdin.write(data)
                    process.stdin.flush()
        except OSError:
            pass
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()


def main():
    ensure_agent_guide()
    port = int(os.environ["ORQELLA_MODULE_PORT"])
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
