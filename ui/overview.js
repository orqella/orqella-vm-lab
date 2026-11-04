(() => {
  const $ = (id) => document.getElementById(id);
  const backend = (path) => window.orqella.call("backend.request", { method: "GET", path });
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  async function load() {
    try {
      const [status, data] = await Promise.all([backend("/api/status"), backend("/api/vms")]);
      const vms = data.vms || [];
      const running = vms.filter((vm) => vm.running).length;
      $("state").textContent = status.ready ? "ready" : "unavailable";
      $("state").className = `state ${status.ready ? "ok" : "bad"}`;
      $("stats").innerHTML = `<div class="stat"><b>${vms.length}</b><span>machines</span></div><div class="stat"><b>${running}</b><span>running</span></div>`;
      $("machines").innerHTML = vms.length ? vms.slice(0, 5).map((vm) => `<div class="machine"><b>${esc(vm.name)}</b><span class="${vm.running ? "ok" : ""}">${vm.running ? "running" : "stopped"} · ${esc(vm.display || "gui")}</span></div>`).join("") : `<span class="muted">No VMs yet.</span>`;
    } catch {
      $("state").textContent = "unavailable";
      $("state").className = "state bad";
      $("machines").innerHTML = `<span class="muted">VM Lab backend unavailable.</span>`;
    }
  }
  window.orqella.ready(() => load());
})();
