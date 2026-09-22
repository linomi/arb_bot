/**
 * Neural-net style beta diagram:
 * left = symbols (inputs), right = group residual node,
 * curved wires labeled with beta; glow ∝ |beta|;
 * green-ish = positive beta, terracotta = negative.
 */
function renderBetaDiagram(containerId, group, fit) {
  const el = typeof containerId === "string" ? document.getElementById(containerId) : containerId;
  if (!el) return;

  const dep = (group && group.dependent_symbol) || (fit && fit.dependent) || "";
  const betas = (fit && fit.betas) || {};
  const symbols = (group && group.symbols) || Object.keys(betas);
  const name = (group && group.name) || "group";

  // Build nodes: all symbols; dependent is special (y), independents have beta
  const nodes = symbols.map((s) => ({
    symbol: s,
    isDep: s === dep,
    beta: s === dep ? 1.0 : Number(betas[s] != null ? betas[s] : 0),
  }));

  const W = Math.max(320, el.clientWidth || 480);
  const rowH = 44;
  const H = Math.max(140, nodes.length * rowH + 40);
  const leftX = 90;
  const rightX = W - 100;
  const midY = H / 2;

  const maxAbs = Math.max(0.15, ...nodes.filter((n) => !n.isDep).map((n) => Math.abs(n.beta)), 0.01);

  let paths = "";
  let labels = "";
  let leftNodes = "";

  nodes.forEach((n, i) => {
    const y = 28 + i * rowH + rowH / 2;
    const short = n.symbol.replace(/IRT$|USDT$|RLS$/i, "");
    const role = n.isDep ? "y" : "x";
    leftNodes += `
      <g class="beta-node ${n.isDep ? "dep" : "ind"}">
        <rect x="${leftX - 70}" y="${y - 14}" width="78" height="28" rx="6" />
        <text x="${leftX - 31}" y="${y + 4}" text-anchor="middle">${short}</text>
        <text class="role" x="${leftX - 31}" y="${y + 16}" text-anchor="middle">${role}</text>
      </g>`;

    if (n.isDep) {
      // dependent feeds residual with implicit weight 1
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

  el.innerHTML = `
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
        <rect x="${rightX - 12}" y="${midY - 28}" width="100" height="56" rx="10" />
        <text x="${rightX + 38}" y="${midY - 2}" text-anchor="middle">R</text>
        <text class="role" x="${rightX + 38}" y="${midY + 14}" text-anchor="middle">${_truncate(name, 14)}</text>
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
