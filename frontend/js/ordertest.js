// TEMPORARY order-test tab (remove together with backend/routers/ordertest_router.py)
const OrderTestView = (() => {
  const $ = (id) => document.getElementById(id);
  let loaded = false, symbols = [];

  function status(msg, err) {
    const s = $("ot-status");
    s.textContent = msg || "";
    s.className = "ot-status" + (err ? " ot-err" : "");
  }
  function fillSelect(filter) {
    const sel = $("ot-symbol");
    const f = (filter || "").toUpperCase();
    sel.innerHTML = "";
    symbols.filter((r) => !f || r.symbol.toUpperCase().includes(f)).forEach((r) => {
      const o = document.createElement("option");
      o.value = r.symbol;
      o.textContent = `${r.symbol}  (max ${r.max_leverage ?? "?"}x, min ${r.min_cost ?? "?"} USDT)`;
      sel.appendChild(o);
    });
  }
  function showResult(obj) {
    $("ot-result").hidden = false;
    $("ot-json").textContent = JSON.stringify(obj, null, 2);
    const a = obj.analysis || obj.summary || {};
    const rows = Object.entries(a).filter(([, v]) => v === null || typeof v !== "object")
      .map(([k, v]) => `<tr><td>${k}</td><td>${v === null ? "-" : v}</td></tr>`).join("");
    $("ot-facts").innerHTML = rows || "<tr><td colspan=2>no analysis</td></tr>";
    if (obj.ok === false) status("Failed: " + (obj.error || "unknown"), true);
  }
  async function refreshState() {
    try {
      const st = await API.get("/api/ordertest/state");
      const keys = Object.keys(st.open || {});
      $("ot-open-list").textContent = keys.length ? "Open test positions: " + keys.join(", ") : "No open test position.";
      if (keys.length) $("ot-symbol-close").value = keys[0];
      $("ot-close").disabled = !keys.length;
    } catch (e) { status(e.message, true); }
  }
  async function load() {
    try {
      const r = await API.get("/api/ordertest/symbols");
      symbols = r.symbols || [];
      $("ot-notional").max = r.max_notional;
      $("ot-leverage").max = r.max_leverage;
      fillSelect("");
      loaded = true;
    } catch (e) { status(e.message, true); }
  }
  async function act(kind) {
    if (!$("ot-confirm").checked) { status("Tick the confirmation box first.", true); return; }
    const btns = [$("ot-open"), $("ot-close")];
    btns.forEach((b) => (b.disabled = true));
    status(kind === "open" ? "Placing order..." : "Closing...");
    try {
      let body;
      if (kind === "open") {
        body = { symbol: $("ot-symbol").value, side: $("ot-side").value,
                 notional: parseFloat($("ot-notional").value), leverage: parseInt($("ot-leverage").value, 10), confirm: true };
      } else {
        body = { symbol: $("ot-symbol-close").value, confirm: true };
      }
      const r = await API.post("/api/ordertest/" + kind, body);
      showResult(r);
      if (r.ok !== false) status(kind === "open" ? "Opened. Now close it." : "Closed. Copy/download the log.");
    } catch (e) { status(e.message, true); }
    $("ot-open").disabled = false;
    await refreshState();
  }
  function bind() {
    $("ot-search").addEventListener("input", (e) => fillSelect(e.target.value));
    $("ot-open").addEventListener("click", () => act("open"));
    $("ot-close").addEventListener("click", () => act("close"));
    $("ot-refresh").addEventListener("click", refreshState);
    $("ot-copy").addEventListener("click", () => navigator.clipboard.writeText($("ot-json").textContent));
  }
  let bound = false;
  return {
    async show() {
      if (!bound) { bind(); bound = true; }
      if (!loaded) await load();
      await refreshState();
    },
  };
})();
