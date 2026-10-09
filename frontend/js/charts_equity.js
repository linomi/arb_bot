/* Equity curve — live starts from account balance; paper from paper_start_balance; margin-in-use overlay */
(function () {
  let equityChart = null;

  function tradePnl(t) {
    // Live: only exchange realized
    if (t.mode === "live") {
      if (t.realized_pnl != null && isFinite(Number(t.realized_pnl))) return Number(t.realized_pnl);
      return null;
    }
    if (t.realized_pnl != null && isFinite(Number(t.realized_pnl))) return Number(t.realized_pnl);
    if (t.pnl != null && isFinite(Number(t.pnl))) return Number(t.pnl);
    return null;
  }

  function label(iso) {
    if (!iso) return "Start";
    if (window.TehranTime) return window.TehranTime.shortLabel(iso);
    if (typeof window._shortLabel === "function") return window._shortLabel(iso);
    return String(iso || "").slice(5, 16);
  }

  window.renderEquityChart = function (canvasId, trades) {
    const closed = (trades || [])
      .filter((t) => t.status === "closed" && t.close_time != null && tradePnl(t) != null)
      .slice()
      .sort((a, b) => {
        const ta = window.TehranTime ? window.TehranTime.parseAsUtc(a.close_time) : new Date(a.close_time);
        const tb = window.TehranTime ? window.TehranTime.parseAsUtc(b.close_time) : new Date(b.close_time);
        return (ta || 0) - (tb || 0);
      });

    const points = [];
    let cum = 0;
    closed.forEach((t) => {
      cum += tradePnl(t);
      points.push({ time: t.close_time, equity: cum, cum_pnl: cum });
    });
    window.renderEquityCurvePoints(canvasId, points, { startEquity: 0 });
  };

  function toMs(iso) {
    if (!iso) return null;
    const d = window.TehranTime && window.TehranTime.parseAsUtc
      ? window.TehranTime.parseAsUtc(iso) : new Date(iso);
    const ms = d ? +d : NaN;
    return isFinite(ms) ? ms : null;
  }

  function fmt(v, d) {
    return Number(v).toLocaleString(undefined, { maximumFractionDigits: d == null ? 2 : d });
  }

  let showMargin = true;
  try { showMargin = localStorage.getItem("equityMargin") !== "0"; } catch (e) {}

  /**
   * points: array of {time, equity?, cum_pnl?, pnl?, is_start?}
   * meta: { startEquity, accountBalance, source, curve }  (curve = full /equity_curve payload)
   */
  window.renderEquityCurvePoints = function (canvasId, points, meta) {
    const canvas = document.getElementById(canvasId);
    if (!canvas || typeof Chart === "undefined") {
      console.warn("renderEquityCurvePoints: missing canvas or Chart.js");
      return;
    }
    if (equityChart) {
      try { equityChart.destroy(); } catch (e) {}
      equityChart = null;
    }
    canvas.style.width = "100%";
    canvas.style.height = "180px";

    const emptyEl = document.getElementById("equity-empty");
    const series = points || [];
    const startEq = meta && meta.startEquity != null ? Number(meta.startEquity) : 0;
    const curve = (meta && meta.curve) || {};
    const isLive = meta && String(meta.source || "").includes("exchange");

    if (!series.length) {
      if (emptyEl) emptyEl.hidden = false;
      const ctx = canvas.getContext("2d");
      if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    if (emptyEl) emptyEl.hidden = true;

    // Equity points (close events) + margin step events on one numeric time axis.
    const closes = series.filter((p) => !p.is_start && p.time)
      .map((p) => ({ x: toMs(p.time), y: Number(p.equity != null ? p.equity : startEq + Number(p.cum_pnl || 0)) }))
      .filter((p) => p.x != null);
    const margins = (curve.margin_series || [])
      .map((m) => ({ x: toMs(m.time), y: Number(m.margin) })).filter((m) => m.x != null);
    const firstX = Math.min(
      closes.length ? closes[0].x : Infinity,
      margins.length ? margins[0].x : Infinity,
    );
    const startX = isFinite(firstX) ? firstX - 1000 : Date.now();
    const eqData = [{ x: startX, y: startEq }].concat(closes);
    const lastX = Math.max(eqData[eqData.length - 1].x, margins.length ? margins[margins.length - 1].x : 0);
    if (lastX > eqData[eqData.length - 1].x) eqData.push({ x: lastX, y: eqData[eqData.length - 1].y });

    const first = eqData[0].y;
    const last = eqData[eqData.length - 1].y;
    const up = last >= first;
    const lineColor = up ? "#6b9a6b" : "#b85c4a";

    const datasets = [{
      label: isLive ? "Account equity" : "Equity",
      data: eqData,
      yAxisID: "y",
      borderColor: lineColor,
      backgroundColor: up ? "rgba(107,154,107,0.12)" : "rgba(184,92,74,0.12)",
      borderWidth: 2,
      pointRadius: eqData.length <= 40 ? 3 : 0,
      pointBackgroundColor: lineColor,
      tension: 0.15,
      fill: true,
    }];
    const hasMargin = margins.length > 0;
    if (hasMargin && showMargin) {
      const mData = [{ x: startX, y: 0 }].concat(margins);
      mData.push({ x: lastX, y: margins[margins.length - 1].y });
      datasets.push({
        label: t("equity.margin_line"),
        data: mData,
        yAxisID: "y2",
        stepped: "before",
        borderColor: "#c9a24b",
        backgroundColor: "rgba(201,162,75,0.10)",
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
      });
    }

    equityChart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: { datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        parsing: false,
        interaction: { mode: "nearest", intersect: false, axis: "x" },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: (items) => (items.length ? label(new Date(items[0].parsed.x).toISOString()) : ""),
              label: (item) => item.dataset.yAxisID === "y2"
                ? `${t("equity.margin_line")}: ${fmt(item.parsed.y)}`
                : `${isLive ? "Equity" : t("equity.stat.equity")}: ${fmt(item.parsed.y, isLive ? 0 : 2)}`,
            },
          },
        },
        scales: {
          x: {
            type: "linear",
            min: startX, max: lastX === startX ? startX + 1000 : lastX,
            ticks: {
              color: "#c4b5a0", maxRotation: 0, autoSkip: true, maxTicksLimit: 6, font: { size: 9 },
              callback: (v) => label(new Date(v).toISOString()),
            },
            grid: { color: "#4a3c30" },
          },
          y: { position: "left", ticks: { color: "#c4b5a0", font: { size: 9 } }, grid: { color: "#4a3c30" } },
          y2: {
            display: hasMargin && showMargin, position: "right", min: 0,
            ticks: { color: "#c9a24b", font: { size: 9 } }, grid: { drawOnChartArea: false },
          },
        },
      },
    });

    // hint + stats strip + margin toggle
    const hint = document.querySelector(".equity-panel .hint");
    if (hint) {
      if (isLive) {
        const bal = meta.accountBalance != null ? Number(meta.accountBalance).toFixed(0) : "?";
        hint.textContent = `Live · start≈${fmt(startEq, 0)} · now free≈${bal} · exchange PnL only · Tehran time`;
      } else {
        hint.textContent = t("equity.hint.paper", { start: fmt(startEq, 0) });
      }
    }
    const panel = canvas.closest(".equity-panel");
    if (panel) {
      let strip = panel.querySelector(".equity-stats");
      if (!strip) {
        strip = document.createElement("div");
        strip.className = "equity-stats";
        const wrap = canvas.closest(".equity-wrap");
        (wrap || canvas).insertAdjacentElement("afterend", strip);
      }
      const item = (k, v, cls) => `<span class="eq-stat"><span class="eq-k">${k}</span> <b class="${cls || ""}">${v}</b></span>`;
      const ret = curve.return_pct != null ? `${curve.return_pct >= 0 ? "+" : ""}${fmt(curve.return_pct)}%` : "--";
      strip.innerHTML =
        item(t("equity.stat.start"), fmt(startEq, 0)) +
        item(t("equity.stat.equity"), fmt(last, 2)) +
        item(t("equity.stat.return"), ret, curve.return_pct >= 0 ? "pos" : "neg") +
        item(t("equity.stat.maxdd"), curve.max_drawdown_pct != null ? `${fmt(curve.max_drawdown_pct)}%` : "--") +
        item(t("equity.stat.margin"), `${fmt(curve.current_margin || 0)} / ${fmt(curve.peak_margin || 0)}`) +
        (hasMargin ? `<label class="eq-toggle"><input type="checkbox" ${showMargin ? "checked" : ""}> ${t("equity.margin_line")}</label>` : "");
      const cb = strip.querySelector(".eq-toggle input");
      if (cb) cb.addEventListener("change", () => {
        showMargin = cb.checked;
        try { localStorage.setItem("equityMargin", showMargin ? "1" : "0"); } catch (e) {}
        window.renderEquityCurvePoints(canvasId, points, meta);
      });
    }
  };
})();
