# VM Lab module

A portable QEMU manager. Everything runs in the sidecar; the daemon only
provides the module backend and WebSocket bridge.

- `backend/server.py` owns VM metadata, QEMU process lifecycle, ports, images,
  seeds, SSH, and the console/terminal bridges.
- `data/` is module-private (0700). VM metadata, unattended files, seed data,
  and images are 0600/0700. Never write runtime state into the git checkout.
- Never signal a PID solely because `meta.json` says it is running. Validate the
  process start time and QEMU command line (or QMP on Windows). `qemu.pid` is a
  hint, not an identity.
- Reserve ports through `reserve_vm_port()`/`allocate_port()`; do not call
  `free_port()` directly in new start paths.
- Remote image URLs must be HTTPS, are size-capped, and may declare `sha256`.
  Do not add an unverified download path.
- QMP over TCP is Windows-only and unauthenticated. Do not expose it beyond
  loopback; document any future alternative.
- Do not default Windows passwords to a known value. Generate one when the
  caller omits it and show it once in the create job log.
- The console and terminal are nested module frames. `ui/app.js` relays the
  Orqella SDK messages for those two frames and appends `context.ws_token` as
  `__orqella_ws` to their URLs. Keep that relay scoped to those frames.
- Use `ORQELLA_HOST_HOME` for host paths; `HOME` is the module home.
- Load `/assets/theme.css` and `/assets/module.css` before module CSS; use theme
  tokens and core primitives.
- Add parser/lifecycle tests under `tests/`. Run
  `python3 -m unittest discover -s tests -p 'test_*.py'`,
  `node --check` for edited UI JS, and `orqella module check .` before publishing.
