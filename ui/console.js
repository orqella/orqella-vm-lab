// Minimal noVNC wrapper for the VM Lab module.
// The actual RFB client is vendored under ./novnc/core (noVNC 1.7.0, MPL-2.0).

import RFB from "./novnc/core/rfb.js";

const source = (() => {
  try { return new URL(window.__ORQELLA_MODULE_URL || location.href, "http://module.invalid"); }
  catch (_) { return new URL("/", "http://module.invalid"); }
})();
const sourcePath = source.pathname;
const params = source.searchParams;
const vm = (params.get("name") || params.get("vm") || "").trim();
const wsToken = (() => {
  const match = location.hash.match(/[#&]__orqella_ws=([^&]+)/);
  if (!match) return "";
  try { return decodeURIComponent(match[1]); } catch (_) { return ""; }
})();
const screen = document.getElementById("screen");
const overlay = document.getElementById("overlay");
const overlayTitle = document.getElementById("overlayTitle");
const overlayText = document.getElementById("overlayText");
const spinner = document.getElementById("spinner");
const stateEl = document.getElementById("state");
const nameEl = document.getElementById("name");
const reconnectBtn = document.getElementById("reconnect");
const fullscreenBtn = document.getElementById("fullscreen");
const keyboardBtn = document.getElementById("keyboard");
const backspaceBtn = document.getElementById("backspace");
const enterBtn = document.getElementById("enter");
const tabBtn = document.getElementById("tab");
const escBtn = document.getElementById("esc");
const ctrlBtn = document.getElementById("ctrl");
const altBtn = document.getElementById("alt");
const shiftBtn = document.getElementById("shift");
const superBtn = document.getElementById("super");
const kbd = document.getElementById("kbd");

let rfb = null;
let composing = false;
let stopped = false;
let retryTimer = null;
let retryDelay = 1200;
let everConnected = false;
let outageTimer = null;
const OUTAGE_OVERLAY_MS = 6000;
let passwordResolve = null;
let nestSequence = 0;
const nestPending = new Map();

window.addEventListener("message", (event) => {
  if (event.source !== parent) return;
  const message = event.data;
  if (!message || message.type !== "orqella:response" || !message.id) return;
  const pending = nestPending.get(message.id);
  if (!pending) return;
  nestPending.delete(message.id);
  clearTimeout(pending.timer);
  if (message.ok) pending.resolve(message.result);
  else pending.reject(new Error(message.error || "request failed"));
});

function nestCall(method, params = {}) {
  const id = `n${++nestSequence}`;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      nestPending.delete(id);
      reject(new Error("VM Lab page did not answer"));
    }, 15000);
    nestPending.set(id, { resolve, reject, timer });
    parent.postMessage({ type: "orqella:request", id, method, params }, "*");
  });
}

function askVncPassword() {
  return new Promise((resolve) => {
    passwordResolve = resolve;
    const dialog = document.getElementById("passwordRequest");
    const input = document.getElementById("vncPasswordInput");
    dialog.hidden = false;
    input.value = "";
    input.focus();
  });
}

function finishPassword(value) {
  if (!passwordResolve) return;
  const resolve = passwordResolve;
  passwordResolve = null;
  document.getElementById("passwordRequest").hidden = true;
  document.getElementById("vncPasswordInput").value = "";
  resolve(value);
}

document.getElementById("vncPasswordCancel").addEventListener("click", () => finishPassword(null));
document.getElementById("vncPasswordOk").addEventListener("click", () => finishPassword(document.getElementById("vncPasswordInput").value));
document.getElementById("vncPasswordInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    finishPassword(document.getElementById("vncPasswordInput").value);
  }
  if (event.key === "Escape") {
    event.preventDefault();
    finishPassword(null);
  }
});

function setState(text) {
  stateEl.textContent = text;
}

function showOverlay(title, text, spinning = false) {
  overlay.hidden = false;
  overlayTitle.textContent = title;
  overlayText.textContent = text;
  spinner.style.display = spinning ? "" : "none";
}

function hideOverlay() {
  overlay.hidden = true;
}

function showFatal(title, text) {
  stopped = true;
  clearOutageOverlay();
  setState("offline");
  showOverlay(title, text, false);
}

function clearOutageOverlay() {
  if (outageTimer) {
    clearTimeout(outageTimer);
    outageTimer = null;
  }
}

// A transient transport drop should not replace the last framebuffer with a
// full-screen failure card. Keep showing the guest and only cover it if the
// outage persists long enough that the user needs an explanation.
function scheduleOutageOverlay(title, text) {
  clearOutageOverlay();
  outageTimer = setTimeout(() => {
    outageTimer = null;
    if (!stopped) showOverlay(title, text, true);
  }, OUTAGE_OVERLAY_MS);
}

async function loadVncPassword() {
  try {
    const result = await nestCall("backend.request", { method: "GET", path: "/api/vms" });
    const info = (result.vms || []).find((item) => item.name === vm);
    return info?.vnc_password || "";
  } catch (_) {
    return "";
  }
}

function connect() {
  if (!vm) {
    showFatal("No VM selected", "Open this console from a VM card in VM Lab.");
    return;
  }
  if (rfb) {
    stopped = true;
    try { rfb.disconnect(); } catch (_) {}
    rfb = null;
  }
  clearTimeout(retryTimer);
  stopped = false;
  retryDelay = 1200;
  clearModifiers();
  setState(everConnected ? "reconnecting…" : "connecting…");
  if (!everConnected) {
    showOverlay("Connecting to " + vm + "…", "Opening the framebuffer.", true);
  }

  const protocol = window.__ORQELLA_MODULE_PROTOCOL || location.protocol;
  const host = window.__ORQELLA_MODULE_HOST || location.host;
  const scheme = protocol === "https:" ? "wss" : "ws";
  const base = sourcePath.replace(/\/(?:ui\/)?console\.html$/, "/");
  const url = new URL(`${scheme}://${host}${base}ws/console?name=${encodeURIComponent(vm)}`);
  if (wsToken) url.searchParams.set("__orqella_ws", wsToken);

  try {
    rfb = new RFB(screen, url.toString());
  } catch (err) {
    showFatal("Could not start the VNC client", String(err && err.message || err));
    return;
  }

  const themeBg = getComputedStyle(document.documentElement).getPropertyValue("--bg-inset").trim();
  rfb.background = themeBg || "#0e0e14";
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.clipViewport = false;
  rfb.viewOnly = false;
  rfb.focusOnClick = true;
  rfb.showDotCursor = true;

  rfb.addEventListener("connect", () => {
    everConnected = true;
    clearOutageOverlay();
    retryDelay = 1200;
    setState("connected");
    hideOverlay();
    nameEl.textContent = vm;
    document.title = `${vm} — VM console`;
    try { rfb.focus(); } catch (_) {}
  });

  rfb.addEventListener("desktopname", (e) => {
    if (e.detail && e.detail.name) {
      nameEl.textContent = `${vm} · ${e.detail.name}`;
      document.title = `${vm} — VM console`;
    }
  });

  rfb.addEventListener("disconnect", (e) => {
    clearModifiers();
    clearOutageOverlay();
    setState("reconnecting…");
    if (stopped) return;
    const clean = e.detail && e.detail.clean;
    if (!everConnected) {
      showOverlay(
        clean ? "VM console disconnected" : "VM console lost",
        clean ? "The session ended." : "The VM may have stopped, or the VNC port closed. Retrying…",
        true,
      );
    } else {
      scheduleOutageOverlay(
        clean ? "VM console disconnected" : "VM console lost",
        clean ? "The session ended. Reconnecting…" : "The VM may have stopped, or the VNC port closed. Retrying…",
      );
    }
    retryTimer = setTimeout(connect, retryDelay);
    retryDelay = Math.min(retryDelay * 1.6, 8000);
  });

  rfb.addEventListener("credentialsrequired", async () => {
    const stored = await loadVncPassword();
    const password = stored || await askVncPassword();
    if (password !== null) {
      try { rfb.sendCredentials({ password }); } catch (_) {}
    }
  });

  rfb.addEventListener("securityfailure", (e) => {
    showFatal("VNC security negotiation failed", String(e.detail && e.detail.reason || "Unknown error"));
  });

  rfb.addEventListener("clipboard", (e) => {
    if (e.detail && e.detail.text && navigator.clipboard) {
      navigator.clipboard.writeText(e.detail.text).catch(() => {});
    }
  });
}

// ---------------------------------------------------------------------------
// On-screen keyboard bridge
//
// Mobile browsers will not open their keyboard for a canvas.  The Keyboard
// button reveals and focuses a slim text field; its input/keydown events are
// translated into RFB key events.  The canvas still handles hardware keyboards
// normally when it has focus.
// ---------------------------------------------------------------------------
const SPECIAL_KEYS = {
  Backspace: [0xFF08, "Backspace"],
  Enter: [0xFF0D, "Enter"],
  Tab: [0xFF09, "Tab"],
  Escape: [0xFF1B, "Escape"],
  Delete: [0xFFFF, "Delete"],
  Home: [0xFF50, "Home"],
  End: [0xFF57, "End"],
  PageUp: [0xFF55, "PageUp"],
  PageDown: [0xFF56, "PageDown"],
  ArrowLeft: [0xFF51, "ArrowLeft"],
  ArrowUp: [0xFF52, "ArrowUp"],
  ArrowRight: [0xFF53, "ArrowRight"],
  ArrowDown: [0xFF54, "ArrowDown"],
};

function sendKey(keysym, code = "Unidentified") {
  if (!rfb) return;
  try { rfb.sendKey(keysym, code); } catch (_) {}
}

// One-shot modifiers.  They apply to the next text character, special key or
// toolbar key, then clear.  Several can be armed together (e.g. Super+Shift).
const MODIFIERS = [
  { name: "Ctrl", btn: ctrlBtn, keysym: 0xFFE3, code: "ControlLeft" },
  { name: "Alt", btn: altBtn, keysym: 0xFFE9, code: "AltLeft" },
  { name: "Shift", btn: shiftBtn, keysym: 0xFFE1, code: "ShiftLeft" },
  { name: "Super", btn: superBtn, keysym: 0xFFEB, code: "MetaLeft" },
];
const armedModifiers = new Set();

function setModifier(def, active) {
  if (active) armedModifiers.add(def.name);
  else armedModifiers.delete(def.name);
  def.btn.classList.toggle("active", active);
  def.btn.setAttribute("aria-pressed", String(active));
}

function toggleModifier(def) {
  setModifier(def, !armedModifiers.has(def.name));
}

function clearModifiers() {
  for (const def of MODIFIERS) setModifier(def, false);
}

const SHIFTED_KEYS = {
  "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
  "*": "8", "(": "9", ")": "0", "_": "-", "+": "=", "{": "[", "}": "]",
  "|": "\\", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
};

function sendKeyOne(keysym, code = "Unidentified", shift = false) {
  if (!rfb) return;
  const active = MODIFIERS.filter((def) => armedModifiers.has(def.name));
  const extraShift = shift && !armedModifiers.has("Shift");
  try {
    for (const def of active) rfb.sendKey(def.keysym, def.code, true);
    if (extraShift) rfb.sendKey(0xFFE1, "ShiftLeft", true);
    rfb.sendKey(keysym, code);
    if (extraShift) rfb.sendKey(0xFFE1, "ShiftLeft", false);
    for (const def of [...active].reverse()) rfb.sendKey(def.keysym, def.code, false);
  } catch (_) {
  } finally {
    clearModifiers();
  }
}

function textKeysym(ch) {
  const cp = ch.codePointAt(0);
  if (cp === 0x0A || cp === 0x0D) return [0xFF0D, "Enter", false];
  if (cp === 0x09) return [0xFF09, "Tab", false];
  if (cp >= 0x41 && cp <= 0x5A) return [cp + 0x20, "Unidentified", true];
  const shifted = SHIFTED_KEYS[ch];
  if (shifted) return [shifted.charCodeAt(0), "Unidentified", true];
  if (cp >= 0x20 && cp <= 0xFF) return [cp, "Unidentified", false];
  return [0x01000000 + cp, "Unidentified", false];
}

function sendText(text) {
  for (const ch of Array.from(text)) {
    const [keysym, code, shift] = textKeysym(ch);
    sendKeyOne(keysym, code, shift);
  }
}

function focusKeyboard() {
  if (!kbd) return;
  kbd.value = "";
  kbd.classList.add("visible");
  keyboardBtn.classList.add("active");
  try {
    kbd.focus();
    kbd.setSelectionRange(0, 0);
  } catch (_) {}
}

keyboardBtn.addEventListener("click", () => {
  if (kbd.classList.contains("visible") && document.activeElement === kbd) {
    kbd.blur();
    kbd.classList.remove("visible");
    keyboardBtn.classList.remove("active");
    clearModifiers();
  } else {
    focusKeyboard();
  }
});
backspaceBtn.addEventListener("click", () => {
  sendKeyOne(0xFF08, "Backspace");
  focusKeyboard();
});
enterBtn.addEventListener("click", () => {
  sendKeyOne(0xFF0D, "Enter");
  focusKeyboard();
});
tabBtn.addEventListener("click", () => {
  sendKeyOne(0xFF09, "Tab");
  focusKeyboard();
});
escBtn.addEventListener("click", () => {
  sendKeyOne(0xFF1B, "Escape");
  focusKeyboard();
});
for (const def of MODIFIERS) {
  def.btn.setAttribute("aria-pressed", "false");
  def.btn.addEventListener("click", () => {
    toggleModifier(def);
    focusKeyboard();
  });
}

kbd.addEventListener("compositionstart", () => { composing = true; });
kbd.addEventListener("compositionend", (e) => {
  composing = false;
  if (e.data) sendText(e.data);
  kbd.value = "";
});
kbd.addEventListener("beforeinput", (e) => {
  if (composing) return;
  if (e.inputType === "deleteContentBackward") {
    e.preventDefault();
    sendKeyOne(0xFF08, "Backspace");
  } else if (e.inputType === "insertLineBreak" || e.inputType === "insertParagraph") {
    e.preventDefault();
    sendKeyOne(0xFF0D, "Enter");
  }
});
kbd.addEventListener("input", () => {
  if (composing) return;
  const text = kbd.value;
  kbd.value = "";
  if (text) sendText(text);
});
kbd.addEventListener("keydown", (e) => {
  if (e.isComposing) return;
  const special = SPECIAL_KEYS[e.key];
  if (special) {
    e.preventDefault();
    sendKeyOne(special[0], special[1]);
    return;
  }
  // Printable characters are handled by the input event.  This also covers
  // phone keyboards and IMEs that do not emit usable keydown data.
});

// Tapping the framebuffer should hand focus back to noVNC (and close the
// phone keyboard), matching desktop behaviour.
screen.addEventListener("pointerdown", () => {
  if (kbd.classList.contains("visible")) {
    kbd.blur();
    kbd.classList.remove("visible");
    keyboardBtn.classList.remove("active");
    clearModifiers();
  }
});

reconnectBtn.addEventListener("click", connect);

fullscreenBtn.addEventListener("click", async () => {
  try {
    if (!document.fullscreenElement) await document.documentElement.requestFullscreen();
    else await document.exitFullscreen();
    setTimeout(() => { try { rfb && rfb.focus(); } catch (_) {} }, 120);
  } catch (_) {}
});

window.addEventListener("paste", (e) => {
  const text = e.clipboardData && e.clipboardData.getData("text");
  if (text && rfb) {
    try { rfb.clipboardPasteFrom(text); } catch (_) {}
  }
});

window.addEventListener("beforeunload", () => {
  stopped = true;
  clearTimeout(retryTimer);
  clearOutageOverlay();
  try { rfb && rfb.disconnect(); } catch (_) {}
});

if (vm) {
  nameEl.textContent = vm;
  document.title = `${vm} — VM console`;
}
connect();
