/**
 * Neural-net style beta diagram with flowing glow particles.
 * Beta labels sit next to input symbols (not mid-wire) to avoid overlap.
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
    group && group.symbols && group.symbols.length
      ? group.symbols
      : [dep, ...Object.keys(betas)].filter(Boolean);
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
  const rowH = 48;
  const H = Math.max(180, nodes.length * rowH + 56);
  const leftX = 96;
  const rightX = W - 118;
  const midY = H / 2;
  const labelX = leftX + 28; // betas close to symbol nodes

  const maxAbs = Math.max(
    0.15,
    ...nodes.filter((n) => !n.isDep).map((n) => Math.abs(n.beta)),
    0.01,
  );

  let paths = "";
  let labels = "";
  let leftNodes = "";
  let particles = "";

  nodes.forEach((n, i) => {
    const y = 32 + i * rowH + rowH / 2;
    const short = String(n.symbol).replace(/[_-]?(IRT|USDT|RLS)$/i, "");
    const role = n.isDep ? "y" : "x";
    const pathId = `beta-path-${i}`;
    // Cubic bezier: leave symbol → soft mid → residual
    const d = `M ${leftX + 14} ${y} C ${(leftX + rightX) * 0.42} ${y}, ${(leftX + rightX) * 0.58} ${midY}, ${rightX - 20} ${midY}`;

    leftNodes += `
      <g class="beta-node ${n.isDep ? "dep" : "ind"}">
        <rect x="${leftX - 78}" y="${y - 15}" width="86" height="30" rx="7" />
        <text x="${leftX - 35}" y="${y + 1}" text-anchor="middle">${_esc(short)}</text>
        <text class="role" x="${leftX - 35}" y="${y + 13}" text-anchor="middle">${role}</text>
      </g>`;

    if (n.isDep) {
      const glow = 0.6;
      paths += `<path id="${pathId}" class="beta-wire pos" d="${d}"
        style="stroke-width:${1.6 + glow * 2.8}; opacity:${0.4 + glow * 0.5}; filter:url(#glow-pos)" />`;
      // Label near the symbol (right of node)
      labels += `<text class="beta-label pos near" x="${labelX}" y="${y - 18}" text-anchor="start">βᵧ = 1</text>`;
      particles += _particleDots(pathId, "pos", 3, 2.4 + i * 0.15);
    } else {
      const b = n.beta;
      const abs = Math.abs(b);
      const glow = Math.min(1, abs / maxAbs);
      const cls = b >= 0 ? "pos" : "neg";
      const filter = b >= 0 ? "url(#glow-pos)" : "url(#glow-neg)";
      paths += `<path id="${pathId}" class="beta-wire ${cls}" d="${d}"
        style="stroke-width:${1.2 + glow * 3.8}; opacity:${0.32 + glow * 0.6}; filter:${filter}" />`;
      const sign = b >= 0 ? "+" : "";
      labels += `<text class="beta-label ${cls} near" x="${labelX}" y="${y - 18}" text-anchor="start">β ${sign}${b.toFixed(3)}</text>`;
      // More particles for larger |β|
      const count = 2 + Math.round(glow * 2);
      const dur = 2.8 - glow * 0.9;
      particles += _particleDots(pathId, cls, count, dur + i * 0.12);
    }
  });

  const displayName = _truncate(name, 22);

  el.innerHTML = `
    <div class="beta-group-title" title="${_esc(name)}">${_esc(name)}</div>
    <svg class="beta-svg" viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="xMidYMid meet">
      <defs>
        <filter id="glow-pos" x="-80%" y="-80%" width="260%" height="260%">
          <feGaussianBlur stdDeviation="2.4" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="glow-neg" x="-80%" y="-80%" width="260%" height="260%">
          <feGaussianBlur stdDeviation="2.4" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <filter id="particle-glow" x="-150%" y="-150%" width="400%" height="400%">
          <feGaussianBlur stdDeviation="1.8" result="b"/>
          <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
        </filter>
        <radialGradient id="dot-pos" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#d4f0d4" stop-opacity="1"/>
          <stop offset="55%" stop-color="#7cb87c" stop-opacity="0.9"/>
          <stop offset="100%" stop-color="#6b9a6b" stop-opacity="0"/>
        </radialGradient>
        <radialGradient id="dot-neg" cx="50%" cy="50%" r="50%">
          <stop offset="0%" stop-color="#f5d0c8" stop-opacity="1"/>
          <stop offset="55%" stop-color="#c97868" stop-opacity="0.9"/>
          <stop offset="100%" stop-color="#b85c4a" stop-opacity="0"/>
        </radialGradient>
      </defs>
      ${paths}
      ${leftNodes}
      <g class="beta-node residual">
        <rect x="${rightX - 18}" y="${midY - 34}" width="118" height="68" rx="11" />
        <text x="${rightX + 41}" y="${midY - 6}" text-anchor="middle">R</text>
        <text class="role residual-name" x="${rightX + 41}" y="${midY + 14}" text-anchor="middle">${_esc(displayName)}</text>
      </g>
      ${labels}
      <g class="beta-particles" filter="url(#particle-glow)">${particles}</g>
    </svg>
    <div class="beta-legend">
      <span class="pos">● positive β</span>
      <span class="neg">● negative β</span>
      <span class="hint">particles flow symbols → R · glow ∝ |β|</span>
    </div>
  `;
}

function _particleDots(pathId, cls, count, baseDur) {
  let out = "";
  const fill = cls === "pos" ? "url(#dot-pos)" : "url(#dot-neg)";
  for (let k = 0; k < count; k++) {
    const dur = (baseDur + k * 0.35).toFixed(2);
    const begin = (k * (baseDur / count)).toFixed(2);
    const r = (2.2 + (k % 2) * 0.8).toFixed(1);
    out += `
      <circle class="beta-particle ${cls}" r="${r}" fill="${fill}" opacity="0">
        <animateMotion dur="${dur}s" begin="${begin}s" repeatCount="indefinite"
          keyPoints="0;1" keyTimes="0;1" calcMode="linear">
          <mpath href="#${pathId}"/>
        </animateMotion>
        <animate attributeName="opacity" values="0;0.95;0.95;0"
          keyTimes="0;0.08;0.85;1" dur="${dur}s" begin="${begin}s" repeatCount="indefinite"/>
      </circle>`;
  }
  return out;
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
