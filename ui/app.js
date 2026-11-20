(() => {
  const $ = (id) => document.getElementById(id);
  let context = { config: {} };
  let status = {};
  let images = [];
  let recipes = [];
  let vms = [];
  let jobTimer = null;
  let agentTimer = null;
  const UI_VERSION = "0.1.11";
  const backend = (path, method = "GET", body) => window.orqella.call("backend.request", {
    method,
    path,
    ...(body !== undefined ? { body } : {}),
  });

  function nestedFrame(source) {
    return [$("consoleFrame"), $("terminalFrame")].find(
      (frame) => frame && frame.contentWindow === source,
    ) || null;
  }

  function nestedFrameUrl(url) {
    let token = context?.ws_token || "";
    if (!token) {
      const match = location.hash.match(/[#&]__orqella_ws=([^&]+)/);
      if (match) {
        try { token = decodeURIComponent(match[1]); } catch (_) { token = match[1]; }
      }
    }
    if (!token) return url;
    const separator = url.includes("#") ? "&" : "#";
    return `${url}${separator}__orqella_ws=${encodeURIComponent(token)}`;
  }

  function loadNestedFrame(frame, url) {
    const target = nestedFrameUrl(url);
    if (window.__ORQELLA_MODULE_INLINE && typeof window.__orqellaModuleBroker?.bundle === "function") {
      let bundlePath = target;
      try {
        const resolved = new URL(target, document.baseURI);
        bundlePath = `${resolved.pathname}${resolved.search}`;
      } catch (_) {}
      frame.removeAttribute("src");
      frame.srcdoc = "";
      window.__orqellaModuleBroker.bundle(bundlePath)
        .then((html) => { frame.srcdoc = html; })
        .catch(() => { frame.src = target; });
      return;
    }
    frame.src = target;
  }

  function initNestedFrame(frame) {
    if (!frame?.contentWindow) return;
    frame.contentWindow.postMessage(
      { type: "orqella:init", context: { ...(context || {}), surface: "page" } },
      "*",
    );
  }

  function initAllNestedFrames() {
    for (const id of ["consoleFrame", "terminalFrame"]) initNestedFrame($(id));
  }

  window.addEventListener("message", async (event) => {
    const frame = nestedFrame(event.source);
    if (!frame) return;
    const message = event.data;
    if (!message || typeof message !== "object") return;
    if (message.type === "orqella:frame-ready") {
      initNestedFrame(frame);
      return;
    }
    if (message.type !== "orqella:request") return;
    const id = message.id;
    try {
      const result = await window.orqella.call(message.method, message.params || {});
      frame.contentWindow.postMessage({ type: "orqella:response", id, ok: true, result }, "*");
    } catch (error) {
      frame.contentWindow.postMessage({
        type: "orqella:response",
        id,
        ok: false,
        error: String(error?.message || error || "request failed"),
      }, "*");
    }
  });

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const gib = (bytes) => `${(Number(bytes || 0) / 1024 ** 3).toFixed(1)} GiB`;

  function setStatus(text, bad = false) {
    $("platform").textContent = text;
    $("platform").className = bad ? "sub bad" : "sub";
  }

  async function load() {
    try {
      status = await backend("/api/status");
      const data = await backend("/api/images");
      images = data.images || [];
      recipes = (await backend("/api/recipes")).recipes || [];
      vms = (await backend("/api/vms")).vms || [];
      render();
    } catch (error) {
      setStatus(error.message || "VM Lab backend unavailable", true);
    }
  }

  function render() {
    const missing = status.missing || [];
    $("setup").hidden = !!status.ready;
    if (!status.ready) {
      $("setupMissing").textContent = missing.join(", ") || "host tools";
      $("setupHelp").textContent = status.platform === "macos"
        ? "Install QEMU with Homebrew: brew install qemu. Then reload this page."
        : status.platform === "windows"
          ? "Install QEMU for Windows and make qemu-system-x86_64.exe and qemu-img.exe available on PATH."
          : "Install QEMU/KVM and OVMF (edk2-ovmf), then reload this page.";
    }
    setStatus(`${status.platform || "host"} · ${status.arch || ""} · ${status.qemu ? "QEMU ready" : "QEMU missing"}${status.uefi ? " · UEFI" : ""}`, !status.ready);
    $("createButton").disabled = !status.ready;
    const counts = status.counts || { total: vms.length, running: vms.filter((vm) => vm.running).length };
    $("summary").innerHTML = `<div class="stat"><b>${counts.total}</b><span>machines</span></div><div class="stat"><b>${counts.running}</b><span>running</span></div><div class="stat"><b>${vms.filter((vm) => vm.display === "gui").length}</b><span>desktop</span></div><div class="stat"><b>${vms.filter((vm) => vm.display === "headless").length}</b><span>headless</span></div>`;
    renderRecipes();
    renderImages();
    renderVms();
  }

  function recipeById(id) {
    return recipes.find((recipe) => recipe.id === id);
  }

  function renderRecipes() {
    const select = $("vmRecipe");
    const previous = select.value;
    select.innerHTML = `<option value="">None — use image catalog</option>` + recipes.map((recipe) => `<option value="${esc(recipe.id)}">${esc(recipe.name)}</option>`).join("");
    if (recipes.some((recipe) => recipe.id === previous)) select.value = previous;
    applyRecipe();
  }

  function applyRecipe() {
    const recipe = recipeById($("vmRecipe").value);
    $("vmImage").disabled = !!recipe;
    if (recipe) {
      $("pathRow").hidden = true;
      $("imageHint").textContent = `${recipe.image || "custom image"}${recipe.description ? ` · ${recipe.description}` : ""}`;
      if (recipe.cpus) $("vmCpus").value = recipe.cpus;
      if (recipe.memory_mb) $("vmMemory").value = recipe.memory_mb;
      if (recipe.display) $("vmDisplay").value = recipe.display;
    } else {
      renderImages();
    }
  }

  function renderImages() {
    const select = $("vmImage");
    const previous = select.value;
    select.innerHTML = images.map((image) => `<option value="${esc(image.id)}" ${image.host_compatible === false ? "disabled" : ""}>${esc(image.name)}${image.host_compatible === false ? " (wrong host)" : ""}</option>`).join("");
    if (images.some((image) => image.id === previous)) select.value = previous;
    const selected = images.find((image) => image.id === select.value);
    if (!$("vmRecipe").value) $("pathRow").hidden = !selected?.requires_path;
    $("unattendRow").hidden = !(selected?.kind === "iso" || selected?.os === "windows" || $("vmRecipe").value);
    $("imageHint").textContent = selected?.note || (selected?.kind === "cloud" ? "The base image downloads on first create." : selected?.kind === "iso" ? "Installer boots through the web console; install, then choose Finish install." : "");
    if (selected && !$("vmRecipe").value) {
      $("vmCpus").value = selected.cpus || context.config.default_cpus || 2;
      $("vmMemory").value = selected.memory_mb || context.config.default_memory_mb || 4096;
    }
    if (!$("vmRecipe").value) $("vmDisplay").value = context.config.default_display || "gui";
  }

  function renderVms() {
    const root = $("vmList");
    if (!vms.length) {
      root.innerHTML = `<div class="empty">No VMs yet. Create one above.</div>`;
      return;
    }
    root.innerHTML = vms.map((vm) => {
      const actions = [];
      if (vm.running) {
        actions.push(`<button data-action="console" data-vm="${esc(vm.name)}">Console</button>`);
        if (vm.os !== "windows" && vm.ssh_command) actions.push(`<button data-action="terminal" data-vm="${esc(vm.name)}">Terminal</button>`);
        actions.push(`<button data-action="stop" data-vm="${esc(vm.name)}">Stop</button><button class="danger" data-action="force-stop" data-vm="${esc(vm.name)}">Force stop</button>`);
      } else {
        actions.push(`<button data-action="start" data-vm="${esc(vm.name)}">Start</button>`);
      }
      if (vm.kind === "iso" && !vm.installed) actions.push(`<button data-action="eject" data-vm="${esc(vm.name)}">Finish install</button>`);
      if (vm.ssh_command) actions.push(`<button data-action="ssh" data-vm="${esc(vm.name)}">Copy SSH</button>`);
      actions.push(`<button data-action="reset" data-vm="${esc(vm.name)}">Reset</button><button class="danger" data-action="destroy" data-vm="${esc(vm.name)}">Destroy</button>`);
      return `<article class="vm-card ${vm.running ? "running" : ""}">
        <div class="vm-top"><span class="vm-name">${esc(vm.name)}</span><span class="chip ${vm.running ? "ok" : ""}">${vm.running ? "running" : "stopped"}</span></div>
        <div class="vm-meta"><span>${esc(vm.os || "linux")}</span><span>${esc(vm.display || "gui")}</span><span><b>${esc(vm.cpus)}</b> vCPU</span><span><b>${esc(vm.memory_mb)}</b> MB</span><span><b>${gib(vm.disk_bytes)}</b></span></div>
        <div class="vm-meta"><span>ssh <b>${esc(vm.ssh_port || "—")}</b></span><span>vnc <b>${esc(vm.vnc_port || "—")}</b></span><span>${esc(vm.image || "")}</span></div>
        <div class="vm-actions">${actions.join("")}</div>
      </article>`;
    }).join("");
    root.querySelectorAll("[data-action]").forEach((button) => {
      button.onclick = () => action(button.dataset.vm, button.dataset.action, button);
    });
  }

  let confirmResolve = null;
  function confirmAction(title, text, confirmLabel = "Confirm") {
    return new Promise((resolve) => {
      $("confirmTitle").textContent = title;
      $("confirmText").textContent = text;
      $("confirmOk").textContent = confirmLabel;
      $("confirmModal").hidden = false;
      confirmResolve = resolve;
      $("confirmCancel").focus();
    });
  }

  function resolveConfirm(value = false) {
    if (!confirmResolve) return;
    const resolve = confirmResolve;
    confirmResolve = null;
    $("confirmModal").hidden = true;
    resolve(value);
  }

  async function action(name, actionName, button) {
    if (actionName === "console") return openConsole(name);
    if (actionName === "terminal") return openTerminal(name);
    if (actionName === "ssh") {
      const vm = vms.find((entry) => entry.name === name);
      try {
        await navigator.clipboard.writeText(vm?.ssh_command || "");
        setStatus("SSH command copied", false);
      } catch (error) { setStatus(error.message, true); }
      return;
    }
    if (actionName === "destroy") {
      const ok = await confirmAction("Destroy VM", `Delete ${name} and its disk? This cannot be undone.`, "Destroy");
      if (!ok) return;
    }
    if (actionName === "reset") {
      const ok = await confirmAction("Reset VM", `Reset ${name}? Its disk will be discarded.`, "Reset");
      if (!ok) return;
    }
    const label = actionName === "force-stop" ? "Force stop" : actionName.charAt(0).toUpperCase() + actionName.slice(1);
    const original = button?.textContent;
    if (button) {
      button.disabled = true;
      if (actionName === "stop" || actionName === "force-stop") button.textContent = "Stopping…";
    }
    try {
      setStatus(`${label} ${name}…`);
      const result = await backend("/api/vms/action", "POST", { name, action: actionName });
      if (result?.ok === false) throw new Error(result.error || `${label} failed`);
      await load();
      setStatus(`${label} ${name} complete`);
    } catch (error) {
      setStatus(error.message || `${label} failed`, true);
    } finally {
      if (button?.isConnected) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  }

  function openConsole(name) {
    $("consoleTitle").textContent = `${name} · console`;
    loadNestedFrame($("consoleFrame"), `console.html?name=${encodeURIComponent(name)}&v=${UI_VERSION}`);
    $("consoleModal").hidden = false;
  }

  function openTerminal(name) {
    $("terminalTitle").textContent = `${name} · terminal`;
    loadNestedFrame($("terminalFrame"), `terminal.html?name=${encodeURIComponent(name)}&v=${UI_VERSION}`);
    $("terminalModal").hidden = false;
  }

  async function create(event) {
    event.preventDefault();
    const recipeId = $("vmRecipe").value;
    const image = images.find((entry) => entry.id === $("vmImage").value);
    if (!recipeId && !image) return;
    const path = $("vmPath").value.trim();
    const body = {
      name: $("vmName").value.trim(),
      recipe: $("vmRecipe").value || "",
      image: image?.id || "",
      cpus: Number($("vmCpus").value),
      memory_mb: Number($("vmMemory").value),
      display: $("vmDisplay").value,
      path,
      unattend: $("vmUnattend").checked,
      windows_password: $("vmPassword").value,
      allow_lan_forwards: false,
      start: true,
    };
    $("createButton").disabled = true;
    $("createLog").hidden = false;
    $("createLog").textContent = "";
    $("createStatus").textContent = "Creating…";
    try {
      const job = await backend("/api/vms/create", "POST", body);
      pollJob(job.id);
    } catch (error) {
      $("createButton").disabled = false;
      $("createStatus").textContent = error.message || "Could not create VM";
    }
  }

  async function pollJob(id) {
    clearTimeout(jobTimer);
    try {
      const job = await backend(`/api/jobs/${encodeURIComponent(id)}`);
      $("createLog").textContent = (job.log || "").slice(-8000);
      $("createLog").scrollTop = $("createLog").scrollHeight;
      $("createStatus").textContent = `${job.status || "working"}…`;
      if (job.status === "succeeded") {
        $("createStatus").textContent = "VM created";
        $("createButton").disabled = !status.ready;
        $("createPanel").hidden = true;
        await load();
        return;
      }
      if (job.status === "failed") {
        $("createButton").disabled = !status.ready;
        $("createStatus").textContent = job.error || "Create failed";
        return;
      }
      jobTimer = setTimeout(() => pollJob(id), 1000);
    } catch (error) {
      $("createButton").disabled = !status.ready;
      $("createStatus").textContent = error.message || "Could not poll create job";
    }
  }


  async function runAgent() {
    const prompt = $("agentPrompt").value.trim();
    if (!prompt) return;
    $("agentRun").disabled = true;
    $("agentStatus").textContent = "Starting agent…";
    const instruction = `Work in this VM Lab data directory. Read AGENT.md before changing anything. Create a reusable VM recipe according to the user's request.\n\nRequirements:\n- Put the recipe in recipes/<id>.json using the documented schema.\n- Use an existing catalog image id, or define an image URL/path and download it into images/ if needed.\n- Add cloud_init and/or provision commands for guest configuration when appropriate.\n- Keep the recipe portable across Linux, macOS, and Windows hosts where possible; reject macOS guest automation on non-macOS hosts.\n- Do not edit module source files.\n\nAvailable catalog ids: ${images.map((image) => image.id).join(", ") || "none"}.\n\nUser request:\n${prompt}`;
    const knownRecipes = new Set(recipes.map((recipe) => recipe.id));
    try {
      const dataRoot = status.data_root;
      if (!dataRoot) throw new Error("VM Lab data directory is unavailable");
      const job = await window.orqella.hooks.run("agent.prompt", {
        prompt: instruction,
        root: dataRoot,
        open: true,
        wait_ms: 8000,
        node: context.node?.id || "local",
      });
      if (job.open_error) {
        $("agentStatus").textContent = `Agent started, but could not open the conversation: ${job.open_error}`;
      } else if (job.session_id) {
        $("agentStatus").textContent = "Opened in the conversation viewer.";
      } else {
        $("agentStatus").textContent = `agent ${job.id.slice(0, 8)} running…`;
      }
      pollAgent(job.id, knownRecipes);
    } catch (error) {
      $("agentRun").disabled = false;
      $("agentStatus").textContent = error.message || "Could not start agent";
    }
  }

  async function pollAgent(id, knownRecipes) {
    clearTimeout(agentTimer);
    try {
      const job = await window.orqella.call("agent.status", { id });
      $("agentStatus").textContent = `${job.status}…`;
      if (job.status === "succeeded") {
        $("agentRun").disabled = false;
        $("agentStatus").textContent = "Recipe ready.";
        $("agentPrompt").value = "";
        await load();
        const first = recipes.find((recipe) => !knownRecipes?.has(recipe.id)) || recipes[recipes.length - 1];
        if (first) {
          $("vmRecipe").value = first.id;
          applyRecipe();
          $("createPanel").hidden = false;
        }
        return;
      }
      if (job.status === "failed") {
        $("agentRun").disabled = false;
        $("agentStatus").textContent = job.error || "Agent failed";
        return;
      }
      agentTimer = setTimeout(() => pollAgent(id, knownRecipes), 1500);
    } catch (error) {
      $("agentRun").disabled = false;
      $("agentStatus").textContent = error.message || "Could not read agent job";
    }
  }

  $("newToggle").onclick = () => { $("createPanel").hidden = !$("createPanel").hidden; if (!$("createPanel").hidden) $("vmName").focus(); };
  $("refresh").onclick = () => load();
  $("vmImage").onchange = renderImages;
  $("vmRecipe").onchange = applyRecipe;
  $("vmUnattend").onchange = () => { $("passwordRow").hidden = !$("vmUnattend").checked; };
  $("askAi").onclick = () => { $("agentPanel").hidden = !$("agentPanel").hidden; if (!$("agentPanel").hidden) $("agentPrompt").focus(); };
  $("agentRun").onclick = runAgent;
  $("createForm").onsubmit = create;
  $("consoleClose").onclick = () => { $("consoleModal").hidden = true; $("consoleFrame").removeAttribute("srcdoc"); $("consoleFrame").src = "about:blank"; };
  $("terminalClose").onclick = () => { $("terminalModal").hidden = true; $("terminalFrame").removeAttribute("srcdoc"); $("terminalFrame").src = "about:blank"; };
  [["consoleModal", "consoleFrame"], ["terminalModal", "terminalFrame"]].forEach(([modal, frame]) => {
    $(modal).onclick = (event) => { if (event.target === $(modal)) { $(modal).hidden = true; $(frame).removeAttribute("srcdoc"); $(frame).src = "about:blank"; } };
  });
  $("confirmCancel").onclick = () => resolveConfirm(false);
  $("confirmOk").onclick = () => resolveConfirm(true);
  $("confirmModal").onclick = (event) => { if (event.target === $("confirmModal")) resolveConfirm(false); };
  document.addEventListener("keydown", (event) => {
    if ($("confirmModal").hidden) return;
    if (event.key === "Escape") { event.preventDefault(); resolveConfirm(false); }
  });
  window.orqella.ready((ctx) => {
    context = ctx;
    initAllNestedFrames();
    window.orqella.onContext((next) => {
      context = next || context;
      initAllNestedFrames();
    });
    load();
  });
})();
