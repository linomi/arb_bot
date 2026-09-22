/**
 * Shared Tehran / Jalali time helpers.
 * Backend stores naive UTC (isoformat without Z). Treat those as UTC, then show Asia/Tehran.
 */
(function (global) {
  function toJalali(gy, gm, gd) {
    const g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334];
    let gy2 = gm > 2 ? gy + 1 : gy;
    let days =
      355666 +
      365 * gy +
      Math.floor((gy2 + 3) / 4) -
      Math.floor((gy2 + 99) / 100) +
      Math.floor((gy2 + 399) / 400) +
      gd +
      g_d_m[gm - 1];
    let jy = -1595 + 33 * Math.floor(days / 12053);
    days %= 12053;
    jy += 4 * Math.floor(days / 1461);
    days %= 1461;
    if (days > 365) {
      jy += Math.floor((days - 1) / 365);
      days = (days - 1) % 365;
    }
    const jm = days < 186 ? 1 + Math.floor(days / 31) : 7 + Math.floor((days - 186) / 30);
    const jd = 1 + (days < 186 ? days % 31 : (days - 186) % 30);
    return [jy, jm, jd];
  }

  /** Parse ISO; if no timezone suffix, assume UTC. */
  function parseAsUtc(iso) {
    if (!iso) return null;
    if (iso instanceof Date) {
      const t = iso.getTime();
      return isNaN(t) ? null : iso;
    }
    let s = String(iso).trim();
    if (!s) return null;
    // Already has Z or ±offset
    if (/[zZ]$/.test(s) || /[+-]\d{2}:?\d{2}$/.test(s)) {
      const d = new Date(s);
      return isNaN(d.getTime()) ? null : d;
    }
    // Space → T; strip fractional seconds noise
    s = s.replace(" ", "T");
    // Naive → treat as UTC
    const d = new Date(s.endsWith("Z") ? s : s + "Z");
    return isNaN(d.getTime()) ? null : d;
  }

  function tehranParts(iso) {
    const d = parseAsUtc(iso);
    if (!d) return null;
    const fmt = new Intl.DateTimeFormat("en-US", {
      timeZone: "Asia/Tehran",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    const parts = {};
    for (const p of fmt.formatToParts(d)) {
      if (p.type !== "literal") parts[p.type] = p.value;
    }
    const hour = parts.hour === "24" ? "00" : parts.hour;
    return {
      gy: parseInt(parts.year, 10),
      gm: parseInt(parts.month, 10),
      gd: parseInt(parts.day, 10),
      hh: hour,
      mi: parts.minute,
    };
  }

  /** Full label: 1404/07/01 15:30 */
  function fmtTime(iso) {
    if (!iso) return "--";
    const tp = tehranParts(iso);
    if (!tp) return String(iso).slice(0, 16);
    const [jy, jm, jd] = toJalali(tp.gy, tp.gm, tp.gd);
    return `${jy}/${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")} ${tp.hh}:${tp.mi}`;
  }

  /** Short chart axis: 07/01 15:30 */
  function shortLabel(iso) {
    if (!iso) return "";
    const tp = tehranParts(iso);
    if (!tp) return String(iso).slice(5, 16);
    const [, jm, jd] = toJalali(tp.gy, tp.gm, tp.gd);
    return `${String(jm).padStart(2, "0")}/${String(jd).padStart(2, "0")} ${tp.hh}:${tp.mi}`;
  }

  global.TehranTime = { toJalali, parseAsUtc, tehranParts, fmtTime, shortLabel };
  global.fmtTime = fmtTime;
  global._shortLabel = shortLabel;
})(typeof window !== "undefined" ? window : globalThis);
