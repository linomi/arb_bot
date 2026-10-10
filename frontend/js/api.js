const API = {
  async _req(method, path, body) {
    const opts = {
      method,
      headers: {},
      credentials: "include", // send session cookie
    };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    if (res.status === 401) {
      // Signal UI that login is required
      if (typeof window !== "undefined" && typeof window.__onAuthRequired === "function") {
        window.__onAuthRequired();
      }
      let detail = "Not authenticated";
      try { detail = (await res.json()).detail || detail; } catch (e) {}
      throw new Error(`${method} ${path} -> 401: ${detail}`);
    }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) {}
      throw new Error(`${method} ${path} -> ${res.status}: ${detail}`);
    }
    if (res.status === 204) return null;
    return res.json();
  },
  get(path) { return this._req("GET", path); },
  post(path, body) { return this._req("POST", path, body ?? {}); },
  put(path, body) { return this._req("PUT", path, body ?? {}); },
  patch(path, body) { return this._req("PATCH", path, body ?? {}); },
  del(path) { return this._req("DELETE", path); },

  // Auth
  authStatus() { return this.get("/api/auth/status"); },
  login(username, password) { return this.post("/api/auth/login", { username, password }); },
  logout() { return this.post("/api/auth/logout"); },

  getAllConfig() { return this.get("/api/config"); },
  getConfigSection(section) { return this.get(`/api/config/${section}`); },
  updateConfigSection(section, data) { return this.put(`/api/config/${section}`, { data }); },

  listGroups(status) { return this.get(`/api/groups${status ? `?status=${status}` : ""}`); },
  getGroup(id) { return this.get(`/api/groups/${id}`); },
  setGroupStatus(id, status) { return this.patch(`/api/groups/${id}/status`, { status }); },
  deleteGroup(id) { return this.del(`/api/groups/${id}`); },
  clearTrades(id, mode) {
    const q = mode ? `?mode=${encodeURIComponent(mode)}` : "";
    return this.del(`/api/groups/${id}/trades${q}`);
  },
  listFits(id) { return this.get(`/api/groups/${id}/fits`); },
  latestFit(id) { return this.get(`/api/groups/${id}/fits/latest`); },
  getFit(groupId, fitId) { return this.get(`/api/groups/${groupId}/fits/${fitId}`); },
  olderPrices(groupId, beforeSec, bars) { return this.get(`/api/groups/${groupId}/prices?before=${beforeSec}&bars=${bars}`); },
  getFitExtended(groupId, fitId) { return this.get(`/api/groups/${groupId}/fits/${fitId}/extended`); },
  liveFit(id) { return this.get(`/api/groups/${id}/live-fit?persist=false`); },
  listTrades(id, mode) {
    const q = mode ? `?mode=${encodeURIComponent(mode)}` : "";
    return this.get(`/api/groups/${id}/trades${q}`);
  },
  groupPerformance(id, mode) {
    const q = mode ? `?mode=${encodeURIComponent(mode)}` : "";
    return this.get(`/api/groups/${id}/performance${q}`);
  },
  allPerformance(mode) {
    const q = mode ? `?mode=${encodeURIComponent(mode)}` : "";
    return this.get(`/api/groups/performance/all${q}`);
  },
  equityCurve(id, mode) {
    const q = mode ? `?mode=${encodeURIComponent(mode)}` : "";
    return this.get(`/api/groups/${id}/equity_curve${q}`);
  },
  createManualGroup(payload) { return this.post("/api/groups/manual", payload); },

  runInit(method, activate, exchange) {
    const body = { method, activate };
    if (exchange) body.exchange = exchange;
    return this.post("/api/init/run", body);
  },
  initProgress() { return this.get("/api/init/progress"); },
  listLiquidSymbols(topN, exchange) {
    const params = new URLSearchParams();
    if (topN) params.set("top_n", topN);
    if (exchange) params.set("exchange", exchange);
    const q = params.toString() ? `?${params}` : "";
    return this.get(`/api/init/symbols${q}`);
  },

  botState() { return this.get("/api/bot/state"); },
  botDiagnostics() { return this.get("/api/bot/diagnostics"); },
  botStart() { return this.post("/api/bot/start"); },
  botStop() { return this.post("/api/bot/stop"); },
  botMode(mode) { return this.post("/api/bot/mode", { trading_mode: mode }); },
  botExchange(exchange) { return this.post("/api/bot/exchange", { exchange }); },

  credStatus(exchange) {
    const q = exchange ? `?exchange=${encodeURIComponent(exchange)}` : "";
    return this.get(`/api/credentials/status${q}`);
  },
  saveCred(payload) { return this.post("/api/credentials", payload); },
  deleteCred(exchange) {
    const q = exchange ? `?exchange=${encodeURIComponent(exchange)}` : "";
    return this.del(`/api/credentials${q}`);
  },

  fundingRates(symbol, limit) {
    const params = new URLSearchParams({ symbol });
    if (limit) params.set("limit", limit);
    return this.get(`/api/debug/funding-rates?${params}`);
  },
};
