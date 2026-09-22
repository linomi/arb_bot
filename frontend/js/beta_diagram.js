/**
 * Neural-net style beta diagram.
 * Group codename is shown above the diagram and on the residual node.
 */
function renderBetaDiagram(containerId, group, fit) {
  const el = typeof containerId === "string" ? document.getElementById(containerId) : containerId;
  if (!el) return;

  const dep =
    (group && group.dependent_symbol) ||
    (fit && fit.dependent_symbol) ||
    (fit && fit.dependent) ||
    "";
  const betas = (fit && fit.betas) || {};
  const symbols =
    (group && group.symbols && group.symbols.length
      ? group.symbols
      : [dep, ...Object.keys(betas)].filter(Boolean));
  const name =
    (group && group.name) ||
    (fit && fit.group_name) ||
    "unnamed";

  const nodes = symbols.map((s) => ({
    symbol: s,
    isDep: s === dep,
    beta: s === dep ? 1.0 : Number(betas[s] != null ? betas[s] : 0),
  }));

  const W = Math.max(360, el.clientWidth || 480);
  const rowH = 44;
  const H = Math.max(160, nodes.length * rowH + 48);
  const leftX = 90;
  const rightX = W - 110;
  const midY = H / 2;

  const maxAbs = Math.max(
    0.15,
    ...nodes.filter((n) => !n.isDep).map((n) => Math.abs(n.beta)),
    0.01,
  );

  let paths = "";
  let labels = "";
  let leftNodes = "";

  nodes.forEach((n, i) => {
    const y = 28 + i * rowH + rowH / 2;
    const short = String(n.symbol).replace(/IRT$|USDT$|RLS$/i, "");
    const role = n.isDep ? "y" : "x";
    leftNodes += `
      <g class="beta-node ${n.isDep ? "dep" : "ind"}">
        <rect x="${leftX - 70}" y="${y - 14}" width="78" height="28" rx="6" />
        <text x="${leftX - 31}" y="${y + 4}" text-anchor="middle">${_esc(short)}</text>
        <text class="role" x="${leftX - 31}" y="${y + 16}" text-anchor="middle">${role}</text>
      </g>`;

    if (n.isDep) {
      const glow = 0.55;
      paths += `<path class="beta-wire pos" d="M ${leftX + 12} ${y} C ${(leftX + rightX) / 2} ${y}, ${(leftX + rightX) / 2} ${midY}, ${rightX - 18} ${midY}"
        style="stroke-width:${1.5 + glow * 3}; opacity:${0.35 + glow * 0.55}; filter:url(#glow-pos)" />`;
      labels += `<text class="beta-label pos" x="${(leftX + rightX) / 2 - 10}" y="${(y + midY) / 2 - 4}">βᵧ = 1</text>`;
    } else {
      const b = n.beta;
      const abs = Math.abs(b);
      const glow = Math.min(1, abs / maxAbs);
      const cls = b >= 0 ? "pos" : "neg";
      const filter = b >= 0 ? "url(#glow-pos)" : "url(#glow-neg)";
      paths += `<path class="beta-wire ${cls}" d="M ${leftX + 12} ${y} C ${(leftX + rightX) / 2} ${y}, ${(leftX + rightX) / 2} ${midY}, ${rightX - 18} ${midY}"
        style="stroke-width:${1.2 + glow * 4}; opacity:${0.3 + glow * 0.65}; filter:${filter}" />`;
      const ly = (y + midY) / 2 + (i % 2 === 0 ? -6 : 10);
      labels += `<text class="beta-label ${cls}" x="${(leftX + rightX) / 2}" y="${ly}">${b >= 0 ? "+" : ""}${b.toFixed(3)}</text>`;
    }
  });

  const displayName = _truncate(name, 22);

  el.innerHTML = `
    <div class="beta-group-title" title="${_esc(name)}">${_esc(name)}</div>
    <svg class="beta-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="xMidYMid meet">
      <defs>
        <filter id="glow-pos" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.2" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="glow-neg" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.2" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
      </defs>
      ${paths}
      ${leftNodes}
      <g class="beta-node residual">
        <rect x="${rightX - 16}" y="${midY - 32}" width="112" height="64" rx="10" />
        <text x="${rightX + 40}" y="${midY - 6}" text-anchor="middle">R</text>
        <text class="role residual-name" x="${rightX + 40}" y="${midY + 12}" text-anchor="middle">${_esc(displayName)}</text>
      </g>
      ${labels}
    </svg>
    <div class="beta-legend">
      <span class="pos">● positive β</span>
      <span class="neg">● negative β</span>
      <span class="hint">glow ∝ |β| · R = y − a − Σ β x</span>
    </div>
  `;
}

function _truncate(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

function _esc(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
