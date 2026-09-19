const API = {
  async _req(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
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

  // config
  getAllConfig() { return this.get("/api/config"); },
  getConfigSection(section) { return this.get(`/api/config/${section}`); },
  updateConfigSection(section, data) { return this.put(`/api/config/${section}`, { data }); },

  // groups
  listGroups(status) { return this.get(`/api/groups${status ? `?status=${status}` : ""}`); },
  getGroup(id) { return this.get(`/api/groups/${id}`); },
  setGroupStatus(id, status) { return this.patch(`/api/groups/${id}/status`, { status }); },
  deleteGroup(id) { return this.del(`/api/groups/${id}`); },
  listFits(id) { return this.get(`/api/groups/${id}/fits`); },
  latestFit(id) { return this.get(`/api/groups/${id}/fits/latest`); },
  liveFit(id) { return this.get(`/api/groups/${id}/live-fit`); },
  listTrades(id) { return this.get(`/api/groups/${id}/trades`); },
  groupPerformance(id) { return this.get(`/api/groups/${id}/performance`); },
  allPerformance() { return this.get(`/api/groups/performance/all`); },

  // init
  runInit(method, activate) { return this.post("/api/init/run", { method, activate }); },
  initProgress() { return this.get("/api/init/progress"); },

  // bot
  botState() { return this.get("/api/bot/state"); },
  botStart() { return this.post("/api/bot/start"); },
  botStop() { return this.post("/api/bot/stop"); },
  botMode(mode) { return this.post("/api/bot/mode", { trading_mode: mode }); },

  // credentials
  credStatus() { return this.get("/api/credentials/status"); },
  saveCred(payload) { return this.post("/api/credentials", payload); },
  deleteCred() { return this.del("/api/credentials"); },
};
