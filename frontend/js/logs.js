/* Logs tab: searchable / filterable view of the server log (GET /api/logs).
 * Newest first. Live tail polls with after_id and prepends; "Load older" pages with before_id. */
const LogsView = (() => {
  const CATS = ["trade", "engine", "exchange", "telegram", "init", "api", "other"];
  const MAX_ROWS = 2000;
  const S = {
    items: [], q: "", level: "", cats: new Set(), live: true, timer: null,
    hasMore: false, matched: 0, total: 0, wired: false, busy: false, gen: 0,
  };
  let searchDebounce = null;

  const esc = (x) => String(x == null ? "" : x).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const $ = (id) => document.getElementById(id);

  const _fmt = new Intl.DateTimeFormat("en-GB", {
    timeZone: "Asia/Tehran", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  function fmtStamp(iso) {
    const TT = window.TehranTime;
    const d = TT && TT.parseAsUtc ? TT.parseAsUtc(iso) : new Date(iso);
    if (!d) return "--";
    let date = "";
    if (TT && TT.tehranParts && TT.toJalali) {
      const p = TT.tehranParts(d);
      if (p) {
        const [, jm, jd] = TT.toJalali(p.gy, p.gm, p.gd);
        date = `${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")} `;
      }
    }
    return date + _fmt.format(d);
  }

  function params(extra) {
    const p = new URLSearchParams();
    if (S.q.trim()) p.set("q", S.q.trim());
    if (S.level) p.set("level", S.level);
    if (S.cats.size) p.set("category", [...S.cats].join(","));
    p.set("limit", "200");
    Object.entries(extra || {}).forEach(([k, v]) => { if (v != null) p.set(k, v); });
    return p.toString();
  }

  function render() {
    const box = $("log-list");
    if (!box) return;
    if (!S.items.length) {
      box.innerHTML = `<div class="table-empty">${t(S.total ? "logs.empty.filtered" : "logs.empty")}</div>`;
    } else {
      box.innerHTML = S.items.map((r) => `
        <div class="log-row lvl-${esc(r.level)}">
          <span class="log-ts">${fmtStamp(r.ts)}</span>
          <span class="log-lvl">${esc(r.level)}</span>
          <span class="log-cat cat-${esc(r.cat)}">${esc(t("logs.cat." + r.cat))}</span>
          <span class="log-msg">${esc(r.msg)}</span>
        </div>`).join("");
    }
    const c = $("log-count");
    if (c) c.textContent = t("logs.count", { shown: S.items.length, matched: S.matched, total: S.total });
    const more = $("log-more");
    if (more) more.hidden = !S.hasMore;
  }

  async function reload() {
    const gen = ++S.gen;
    try {
      const res = await API.get("/api/logs?" + params());
      if (gen !== S.gen) return;
      S.items = res.items; S.matched = res.matched; S.total = res.total; S.hasMore = res.has_more;
      render();
    } catch (e) {
      const box = $("log-list");
      if (box) box.innerHTML = `<div class="table-empty">${esc(e.message || e)}</div>`;
    }
  }

  async function poll() {
    if (S.busy || !S.items.length) return S.items.length ? undefined : reload();
    S.busy = true;
    const gen = S.gen;
    try {
      const res = await API.get("/api/logs?" + params({ after_id: S.items[0].id }));
      if (gen !== S.gen) return;
      S.total = res.total;
      if (res.items.length) {
        S.items = res.items.concat(S.items).slice(0, MAX_ROWS);
        S.matched += res.matched;
        render();
      }
    } catch (e) { /* transient; next tick retries */ }
    finally { S.busy = false; }
  }

  async function loadOlder() {
    if (!S.items.length) return;
    const gen = S.gen;
    const res = await API.get("/api/logs?" + params({ before_id: S.items[S.items.length - 1].id }));
    if (gen !== S.gen) return;
    S.items = S.items.concat(res.items).slice(0, MAX_ROWS);
    S.hasMore = res.has_more && S.items.length < MAX_ROWS;
    render();
  }

  function syncChips() {
    document.querySelectorAll("#log-cats .chip").forEach((b) => {
      const c = b.dataset.cat;
      b.classList.toggle("on", c === "" ? S.cats.size === 0 : S.cats.has(c));
    });
  }

  function buildChips() {
    $("log-cats").innerHTML = `<button type="button" class="chip" data-cat="">${t("logs.cat.all")}</button>` +
      CATS.map((c) => `<button type="button" class="chip" data-cat="${c}">${t("logs.cat." + c)}</button>`).join("");
    syncChips();
  }

  function wire() {
    if (S.wired) return;
    S.wired = true;
    buildChips();
    $("log-cats").addEventListener("click", (e) => {
      const b = e.target.closest(".chip");
      if (!b) return;
      const c = b.dataset.cat;
      if (c === "") S.cats.clear();
      else if (S.cats.has(c)) S.cats.delete(c); else S.cats.add(c);
      syncChips(); reload();
    });
    $("log-search").addEventListener("input", (e) => {
      S.q = e.target.value;
      clearTimeout(searchDebounce);
      searchDebounce = setTimeout(reload, 250);
    });
    $("log-level").addEventListener("change", (e) => { S.level = e.target.value; reload(); });
    $("log-live").addEventListener("change", (e) => { S.live = e.target.checked; schedule(); });
    $("log-more").addEventListener("click", loadOlder);
    $("log-refresh").addEventListener("click", reload);
    $("log-clear").addEventListener("click", () => {
      S.q = ""; S.level = ""; S.cats.clear();
      $("log-search").value = ""; $("log-level").value = "";
      syncChips(); reload();
    });
    syncChips();
  }

  function schedule() {
    clearInterval(S.timer); S.timer = null;
    if (S.live) S.timer = setInterval(() => { if (!document.hidden) poll(); }, 3000);
  }

  return {
    show() { wire(); reload(); schedule(); },
    hide() { clearInterval(S.timer); S.timer = null; },
    /** language switch: re-label chips and rows */
    relabel() {
      if (!S.wired) return;
      buildChips();
      render();
    },
  };
})();
window.LogsView = LogsView;
