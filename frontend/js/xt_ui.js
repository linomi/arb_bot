/**
 * XT / multi-exchange UI wiring (loaded after main.js).
 * Patches API helpers and settings form for Nobitex + XT credentials.
 */
(function () {
  if (typeof API === "undefined") return;

  state.exchange = state.exchange || "nobitex";

  API.credStatus = function (exchange) {
    const q = exchange ? "?exchange=" + encodeURIComponent(exchange) : "";
    return this.get("/api/credentials/status" + q);
  };
  API.deleteCred = function (exchange) {
    const q = exchange ? "?exchange=" + encodeURIComponent(exchange) : "";
    return this.del("/api/credentials" + q);
  };
  API.botExchange = function (exchange) {
    return this.post("/api/bot/exchange", { exchange: exchange });
  };
  API.listLiquidSymbols = function (topN, exchange) {
    const params = new URLSearchParams();
    if (topN) params.set("top_n", topN);
    if (exchange) params.set("exchange", exchange);
    const q = params.toString() ? "?" + params : "";
    return this.get("/api/init/symbols" + q);
  };

  function syncCredAuthFields() {
    const credEx = document.getElementById("cred-exchange-select");
    const authSel = document.getElementById("auth-method-select");
    const t = document.getElementById("cred-fields-token");
    const k = document.getElementById("cred-fields-key");
    const xtHint = document.getElementById("cred-xt-hint");
    const ex = (credEx && credEx.value) || "nobitex";
    if (authSel) {
      Array.from(authSel.options).forEach(function (o) {
        if (ex === "xt") o.hidden = o.value !== "key_secret";
        else o.hidden = o.value === "key_secret";
      });
      var visible = Array.from(authSel.options).filter(function (o) { return !o.hidden; });
      if (visible.length && authSel.selectedOptions[0] && authSel.selectedOptions[0].hidden) {
        authSel.value = visible[0].value;
      }
    }
    var method = (authSel && authSel.value) || "token";
    if (t) t.style.display = method === "token" ? "" : "none";
    if (k) k.style.display = (method === "key_signature" || method === "key_secret") ? "" : "none";
    if (xtHint) xtHint.style.display = ex === "xt" ? "" : "none";
  }

  var _refreshBotState = window.refreshBotState;
  window.refreshBotState = async function () {
    if (_refreshBotState) await _refreshBotState();
    try {
      var s = await API.botState();
      var ex = s.exchange || "nobitex";
      state.exchange = ex;
      var exSel = document.getElementById("exchange-select");
      if (exSel && exSel.value !== ex) exSel.value = ex;
    } catch (e) {}
  };

  var exchangeSel = document.getElementById("exchange-select");
  if (exchangeSel) {
    exchangeSel.addEventListener("change", async function (e) {
      var ex = e.target.value;
      try {
        await API.botExchange(ex);
        state.exchange = ex;
        if (state.initMethod === "manual" && typeof loadLiquidSymbols === "function") {
          state.selectedSymbols.clear();
          await loadLiquidSymbols();
        }
        if (typeof refreshBotState === "function") refreshBotState();
      } catch (err) {
        alert("Failed to set exchange: " + (err.message || err));
        e.target.value = state.exchange || "nobitex";
      }
    });
  }

  var modeSel = document.getElementById("trading-mode-select");
  if (modeSel) {
    var clone = modeSel.cloneNode(true);
    modeSel.parentNode.replaceChild(clone, modeSel);
    clone.addEventListener("change", async function (e) {
      var mode = e.target.value;
      var exchange = state.exchange || "nobitex";
      if (mode === "live") {
        var status = await API.credStatus(exchange);
        if (!status.configured) {
          alert("No live credentials saved for " + exchange + " yet. Add them under Settings & Credentials first.");
          e.target.value = "paper";
          return;
        }
        if (!confirm("Switching to LIVE mode will place real orders on " + exchange + ". Continue?")) {
          e.target.value = "paper";
          return;
        }
      }
      await API.botMode(mode);
      refreshBotState();
    });
  }

  window.loadLiquidSymbols = async function () {
    var box = document.getElementById("symbol-checklist");
    if (box) box.innerHTML = '<div class="sym-loading">Loading liquid symbols…</div>';
    try {
      var data = await API.listLiquidSymbols(null, state.exchange || "nobitex");
      var raw = data && data.symbols != null ? data.symbols : data;
      state.liquidSymbols = Array.isArray(raw)
        ? raw.map(function (s) { return typeof s === "string" ? s : (s && (s.symbol || s.name)) || ""; }).filter(Boolean)
        : [];
      if (typeof setInitStatus === "function") {
        if (!state.liquidSymbols.length) {
          setInitStatus("No liquid symbols returned for " + (state.exchange || "nobitex") + ".");
        } else {
          setInitStatus("Loaded " + state.liquidSymbols.length + " liquid symbols (" + (state.exchange || "nobitex") + ").");
        }
      }
      if (typeof renderSymbolChecklist === "function") renderSymbolChecklist();
    } catch (e) {
      console.warn("loadLiquidSymbols", e);
      if (box) box.innerHTML = '<div class="sym-loading">Failed to load symbols.</div>';
      if (typeof setInitStatus === "function") setInitStatus("Could not load symbols: " + (e.message || e));
    }
  };

  window.refreshSettingsTab = async function () {
    var credEx = document.getElementById("cred-exchange-select");
    var ex = (credEx && credEx.value) || state.exchange || "nobitex";
    try {
      var status = await API.credStatus(ex);
      var el = document.getElementById("cred-status");
      if (el) {
        var name = ex === "xt" ? "XT.com" : "Nobitex";
        el.textContent = status.configured
          ? name + ": credentials configured (" + (status.auth_method || "?") + ")."
          : name + ": no credentials saved.";
      }
    } catch (e) {}
    syncCredAuthFields();
    try {
      var sys = await API.getConfigSection("system");
      state.systemCfg = sys;
      var form = document.getElementById("system-form");
      if (form && typeof renderParamForm === "function") renderParamForm(form, sys);
    } catch (e) {}
  };

  var credForm = document.getElementById("cred-form");
  if (credForm) {
    var cf = credForm.cloneNode(true);
    credForm.parentNode.replaceChild(cf, credForm);
    cf.addEventListener("submit", async function (e) {
      e.preventDefault();
      var fd = new FormData(cf);
      var payload = Object.fromEntries(fd.entries());
      var authSel = document.getElementById("auth-method-select");
      var credEx = document.getElementById("cred-exchange-select");
      if (authSel) payload.auth_method = authSel.value;
      if (credEx) payload.exchange = credEx.value;
      try {
        await API.saveCred(payload);
        refreshSettingsTab();
        alert("Credentials saved for " + (payload.exchange || "nobitex") + ".");
      } catch (err) {
        alert("Save failed: " + (err.message || err));
      }
    });
  }

  var delCred = document.getElementById("delete-cred-btn");
  if (delCred) {
    var dc = delCred.cloneNode(true);
    delCred.parentNode.replaceChild(dc, delCred);
    dc.addEventListener("click", async function () {
      var credEx = document.getElementById("cred-exchange-select");
      var ex = (credEx && credEx.value) || "nobitex";
      if (!confirm("Remove credentials for " + ex + "?")) return;
      await API.deleteCred(ex);
      refreshSettingsTab();
    });
  }

  var authSel = document.getElementById("auth-method-select");
  if (authSel) authSel.addEventListener("change", syncCredAuthFields);

  var credExSel = document.getElementById("cred-exchange-select");
  if (credExSel) {
    credExSel.addEventListener("change", function () {
      syncCredAuthFields();
      refreshSettingsTab();
    });
  }

  var _createManual = API.createManualGroup.bind(API);
  API.createManualGroup = function (payload) {
    payload = payload || {};
    if (!payload.exchange) payload.exchange = state.exchange || "nobitex";
    return _createManual(payload);
  };

  if (typeof FIELD_HINTS !== "undefined") {
    FIELD_HINTS.xt_leverage = "XT only: leverage applied once per symbol before first order";
    FIELD_HINTS.xt_margin_mode = "XT only: isolated or cross";
    FIELD_HINTS.funding_rate_estimate = "XT only: assumed funding per interval (0 = Nobitex-like)";
    FIELD_HINTS.expected_holding_funding_intervals = "XT only: fixed intervals x funding estimate in cost_rate";
  }

  syncCredAuthFields();
})();
