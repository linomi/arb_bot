"""
Beta-aware leg sizing for raw-price OLS residuals.

    y = a + sum(beta_j * x_j) + residual

Notional basis (``basis`` argument of leg_orders):
  "gross" (default): trade_notional is the TOTAL notional of all legs.
      qty_y  = trade_notional / (P_y * (1 + sum_j |beta_j| * P_xj / P_y))
  "dependent" (legacy rows only): trade_notional is the dependent leg's notional.
      qty_y  = trade_notional / P_y
In both cases qty_xj = |beta_j| * qty_y.

Negligible independent legs (contrib << dependent notional) are dropped
instead of scaling the whole basket up to meet the exchange min order size.
Remaining legs are then scaled so each meets min_order_value.
"""
from __future__ import annotations

import logging

log = logging.getLogger("sizing")

# Single source of truth for Nobitex IRT minimum order value (Rial).
# XT uses per-market limits from load_markets instead.
MIN_ORDER_VALUE_IRT = 50_000.0

# Drop independent leg if its notional is below this fraction of dependent notional.
DEFAULT_MIN_WEIGHT_FRACTION = 0.02


class ContractRoundingError(ValueError):
    """Raised when legs cannot be expressed in whole exchange contracts within the allowed size band."""

    def __init__(self, reason: str, details: dict | None = None):
        self.reason = reason
        self.details = details or {}
        super().__init__(reason)


class ExcessiveScalingError(ValueError):
    """Raised when leg_orders would inflate the basket beyond max_scale."""

    def __init__(self, scale: float, symbol: str | None = None, max_scale: float | None = None):
        self.scale = scale
        self.symbol = symbol
        self.max_scale = max_scale
        msg = f"scale {scale:.3f}"
        if symbol:
            msg += f" required for {symbol}"
        if max_scale is not None:
            msg += f" exceeds max_scale={max_scale}"
        super().__init__(msg)


def leg_orders(
    dependent_symbol: str,
    direction: str,
    betas: dict,
    prices: dict,
    trade_notional: float,
    min_order_value: float | None = None,
    min_order_value_irt: float | None = None,  # backward-compat alias
    min_weight_fraction: float = DEFAULT_MIN_WEIGHT_FRACTION,
    max_scale: float | None = None,
    basis: str = "gross",
    contract_sizes: dict | None = None,
    contract_mode: str = "joint",
    contract_max_size_dev: float = 0.35,
) -> list[dict]:
    """``contract_sizes`` ({symbol: base units per contract}) turns on whole-contract rounding (XT):
    each leg is rounded to an integer number of contracts exactly as the exchange will, and
    ``ContractRoundingError`` is raised when that is impossible. Without it legs stay continuous."""
    if basis not in ("gross", "dependent"):
        raise ValueError(f"unknown notional basis: {basis}")
    if direction not in ("long_residual", "short_residual"):
        raise ValueError(f"unknown direction: {direction}")
    notional = float(trade_notional)
    if notional <= 0:
        raise ValueError("trade_notional must be positive")

    # Resolve minimum: explicit min_order_value > legacy min_order_value_irt > default IRT
    if min_order_value is not None:
        min_val = float(min_order_value)
    elif min_order_value_irt is not None:
        min_val = float(min_order_value_irt)
    else:
        min_val = MIN_ORDER_VALUE_IRT

    py = float(prices[dependent_symbol])
    if py <= 0:
        raise ValueError(f"invalid price for {dependent_symbol}: {py}")

    dep_side = "buy" if direction == "long_residual" else "sell"

    # Relative weight of each regressor leg vs the dependent leg (|beta| * P_x / P_y).
    # The 2% drop rule is expressed in these units, independent of the notional basis.
    rel: dict[str, float] = {}
    beta_of: dict[str, float] = {}
    px_of: dict[str, float] = {}
    dropped: list[dict] = []
    for sym, beta in (betas or {}).items():
        b = float(beta)
        px = float(prices[sym])
        if px <= 0:
            raise ValueError(f"invalid price for {sym}: {px}")
        w = abs(b) * px / py
        if w < float(min_weight_fraction):
            dropped.append({
                "symbol": str(sym),
                "beta": b,
                "weight": w,
                "reason": f"weight {w:.4f} < min_weight_fraction {min_weight_fraction}",
            })
            continue
        rel[str(sym)] = w
        beta_of[str(sym)] = b
        px_of[str(sym)] = px

    if basis == "gross":
        dep_notional = notional / (1.0 + sum(rel.values()))
    else:
        dep_notional = notional
    dep_qty = dep_notional / py
    for d in dropped:
        d["leg_notional"] = abs(d["beta"]) * dep_qty * float(prices[d["symbol"]])

    legs: list[dict] = [
        {
            "symbol": dependent_symbol,
            "side": dep_side,
            "qty": float(dep_qty),
            "price": py,
            "is_dependent": True,
        }
    ]
    for sym, w in rel.items():
        b = beta_of[sym]
        same_side = b < 0
        side = dep_side if same_side else ("sell" if dep_side == "buy" else "buy")
        legs.append({
            "symbol": sym,
            "side": side,
            "qty": float(abs(b) * dep_qty),
            "price": px_of[sym],
            "is_dependent": False,
            "beta": b,
        })

    if dropped:
        log.info(
            "dropped negligible legs (min_weight=%.3f): %s",
            min_weight_fraction,
            [(d["symbol"], round(d["weight"], 5)) for d in dropped],
        )

    # Scale remaining legs to meet exchange minimum — never inflate for dropped legs.
    scale = 1.0
    scale_symbol = None
    for leg in legs:
        leg_notional = float(leg["qty"]) * float(leg["price"])
        if leg_notional > 0 and leg_notional < min_val:
            needed = min_val / leg_notional
            if needed > scale:
                scale = needed
                scale_symbol = leg["symbol"]

    if max_scale is not None and scale > float(max_scale):
        raise ExcessiveScalingError(scale, symbol=scale_symbol, max_scale=float(max_scale))

    if scale > 1.0:
        for leg in legs:
            leg["qty"] = float(leg["qty"]) * scale
            leg["scaled_for_min"] = True
            leg["scale"] = scale

    # Attach drop list on dependent leg metadata for audit trail
    legs[0]["dropped_legs"] = dropped

    if contract_sizes and (contract_mode or "off") != "off":
        legs = round_legs_to_contracts(
            legs, contract_sizes, mode=contract_mode,
            max_size_dev=contract_max_size_dev, min_order_value=min_val,
        )
    return legs


def _drift_stats(legs: list[dict], ideal_n: list[float], new_n: list[float]) -> tuple[float, float, float]:
    """(size_ratio, hedge_drift, drift_notional). size_ratio = new gross / ideal gross. hedge_drift is the
    largest relative deviation of any leg from its ideal share AFTER removing the common size change,
    drift_notional = sum of |new leg notional - ideal leg notional x size_ratio| (the unhedged amount)."""
    g0, g1 = sum(ideal_n), sum(new_n)
    lam = g1 / g0 if g0 > 0 else 1.0
    drifts = [(n1 / (lam * n0) - 1.0) if n0 > 0 and lam > 0 else 0.0 for n0, n1 in zip(ideal_n, new_n)]
    dn = sum(abs(n1 - lam * n0) for n0, n1 in zip(ideal_n, new_n))
    return lam, (max((abs(d) for d in drifts), default=0.0)), dn


def round_legs_to_contracts(
    legs: list[dict],
    contract_sizes: dict,
    *,
    mode: str = "joint",
    max_size_dev: float = 0.35,
    min_order_value: float | None = None,
) -> list[dict]:
    """Round every leg that has a known contract size to a whole number of contracts.

    mode "floor":   truncate (what the exchange client did silently before).
    mode "nearest": round each leg on its own.
    mode "joint":   search nearby whole-contract combinations and keep the one whose legs stay closest
                    to the planned hedge ratio, with total size within +-``max_size_dev`` of the plan.
    A leg that cannot reach 1 contract (or the exchange minimum) makes the entry infeasible.
    Rounding details are stored on the dependent leg as ``rounding``."""
    import itertools
    import math

    if mode not in ("floor", "nearest", "joint"):
        raise ValueError(f"unknown contract rounding mode: {mode}")
    dev = max(0.0, float(max_size_dev))
    mv = float(min_order_value) if min_order_value else 0.0
    ideal_n = [float(l["qty"]) * float(l["price"]) for l in legs]
    cs_list = [float(contract_sizes[l["symbol"]]) if l["symbol"] in contract_sizes and float(contract_sizes[l["symbol"]]) > 0 else None
               for l in legs]
    ideal_c = [(float(l["qty"]) / cs) if cs else None for l, cs in zip(legs, cs_list)]
    one_contract = [(cs * float(l["price"])) if cs else None for l, cs in zip(legs, cs_list)]

    def too_small(i: int) -> ContractRoundingError:
        sym = legs[i]["symbol"]
        if ideal_c[i] is not None and ideal_c[i] >= 1.0 and mv > 0:
            return ContractRoundingError(
                f"{sym}: {math.floor(ideal_c[i])} contract(s) of {one_contract[i]:.2f} USDT fall below the "
                f"exchange minimum {mv:.2f} USDT — raise trade_notional",
                {"symbol": sym, "planned_notional": ideal_n[i], "contract_value": one_contract[i],
                 "contract_size": cs_list[i], "min_order_value": mv},
            )
        return ContractRoundingError(
            f"{sym}: planned {ideal_n[i]:.2f} USDT is {ideal_c[i]:.2f} contracts; one contract is "
            f"{one_contract[i]:.2f} USDT (contract size {cs_list[i]:g}) — raise trade_notional or use a "
            f"coin with smaller contracts",
            {"symbol": sym, "planned_notional": ideal_n[i], "contract_value": one_contract[i], "contract_size": cs_list[i]},
        )

    def min_ok(i: int, k: float) -> bool:
        return not (mv > 0 and cs_list[i] and k * one_contract[i] < mv - 1e-9)

    chosen: list[float | None] = [None] * len(legs)
    if mode in ("floor", "nearest"):
        for i, c in enumerate(ideal_c):
            if c is None:
                continue
            k = math.floor(c + 1e-9) if mode == "floor" else math.floor(c + 0.5)
            if k < 1 or not min_ok(i, k):
                raise too_small(i)
            chosen[i] = float(k)
    else:
        grid = sorted({round(1.0 + dev * (j - 7) / 7.0, 6) for j in range(15)})
        cands: list[list[float | None]] = []
        for i, c in enumerate(ideal_c):
            if c is None:
                cands.append([None])
                continue
            ks = set()
            for lam in grid:
                x = c * lam
                for k in (math.floor(x), math.ceil(x)):
                    if k >= 1 and min_ok(i, k):
                        ks.add(float(k))
            if not ks:
                raise too_small(i)
            cands.append(sorted(ks))
        combos = 1
        for cc in cands:
            combos *= len(cc)
        if combos > 60000:  # very wide baskets: fall back to per-leg nearest
            return round_legs_to_contracts(legs, contract_sizes, mode="nearest",
                                           max_size_dev=max_size_dev, min_order_value=min_order_value)
        best = None
        for combo in itertools.product(*cands):
            new_n = [ideal_n[i] if combo[i] is None else combo[i] * one_contract[i] for i in range(len(legs))]
            lam, drift, _dn = _drift_stats(legs, ideal_n, new_n)
            if abs(lam - 1.0) > dev + 1e-9:
                continue
            score = drift + 0.1 * abs(lam - 1.0)
            if best is None or score < best[0] - 1e-12:
                best = (score, combo)
        if best is None:
            # no combination keeps the total within the band: the smallest leg is the usual culprit
            worst = max((i for i, c in enumerate(ideal_c) if c is not None),
                        key=lambda i: one_contract[i] / max(ideal_n[i], 1e-12))
            raise too_small(worst)
        chosen = list(best[1])

    out: list[dict] = []
    new_n: list[float] = []
    for i, leg in enumerate(legs):
        nl = dict(leg)
        if chosen[i] is not None:
            nl["ideal_qty"] = float(leg["qty"])
            nl["contracts"] = float(chosen[i])
            nl["contract_size"] = cs_list[i]
            nl["qty"] = float(chosen[i]) * cs_list[i]
        out.append(nl)
        new_n.append(float(nl["qty"]) * float(nl["price"]))
    lam, drift, dn = _drift_stats(legs, ideal_n, new_n)
    info = {
        "mode": mode, "size_ratio": lam, "hedge_drift": drift, "drift_notional": dn,
        "ideal_gross": sum(ideal_n), "gross": sum(new_n),
        "legs": [
            {"symbol": l["symbol"], "contracts": o.get("contracts"), "ideal_contracts": ideal_c[i],
             "contract_value": one_contract[i],
             "drift": (new_n[i] / (lam * ideal_n[i]) - 1.0) if ideal_n[i] > 0 and lam > 0 else 0.0}
            for i, (l, o) in enumerate(zip(legs, out))
        ],
    }
    for o in out:
        if o.get("is_dependent"):
            o["rounding"] = info
            break
    else:
        out[0]["rounding"] = info
    return out


def rounding_info(legs: list[dict]) -> dict | None:
    for l in legs or []:
        if l.get("rounding"):
            return l["rounding"]
    return None


def drift_gate(
    info: dict | None,
    *,
    expected_net_profit: float | None,
    max_drift: float = 0.15,
    move_estimate: float = 0.01,
    max_noise_ratio: float = 0.5,
) -> tuple[bool, dict]:
    """Accept or reject an entry whose legs were rounded to whole contracts.

    Rejects when (a) the hedge drifted more than ``max_drift`` (relative to any leg's planned share), or
    (b) the unhedged notional x an assumed ``move_estimate`` price move over the holding period exceeds
    ``max_noise_ratio`` x the expected net profit (so the rounding noise could swamp the edge).
    max_drift <= 0 / max_noise_ratio <= 0 disable the respective check. No info = nothing to check."""
    det: dict = {"hedge_drift": None, "drift_notional": None, "noise": None, "expected_net_profit": expected_net_profit,
                 "reject_reason": None}
    if not info:
        return True, det
    drift = float(info.get("hedge_drift") or 0.0)
    dn = float(info.get("drift_notional") or 0.0)
    noise = dn * float(move_estimate)
    det.update(hedge_drift=drift, drift_notional=dn, noise=noise)
    if max_drift and max_drift > 0 and drift > float(max_drift):
        det["reject_reason"] = f"hedge drift {drift:.1%} > max {float(max_drift):.1%}"
        return False, det
    if (max_noise_ratio and max_noise_ratio > 0 and expected_net_profit is not None
            and expected_net_profit > 0 and noise > float(max_noise_ratio) * float(expected_net_profit)):
        det["reject_reason"] = (f"drift noise {noise:.4f} > {float(max_noise_ratio):.0%} of expected net profit "
                                f"{expected_net_profit:.4f}")
        return False, det
    return True, det


def _is_irt(symbol: str) -> bool:
    s = (symbol or "").upper()
    return s.endswith("IRT") or s.endswith("RLS")


def quote_suffix(symbol: str) -> str:
    s = (symbol or "").upper()
    # Unified ccxt swap form BTC/USDT:USDT
    if ":USDT" in s or s.endswith(":USDT"):
        return "USDT"
    if s.endswith("USDT") or "/USDT" in s:
        return "USDT"
    if s.endswith("IRT") or s.endswith("RLS"):
        return "IRT"
    if len(s) > 3:
        return s[-3:]
    return s


def assert_same_quote(symbols: list[str], dependent: str | None = None) -> str:
    """Raise ValueError if symbols mix quote currencies. Returns common quote."""
    if not symbols:
        raise ValueError("no symbols")
    quotes = {quote_suffix(s) for s in symbols}
    if dependent:
        quotes.add(quote_suffix(dependent))
    if len(quotes) != 1:
        raise ValueError(
            f"mixed quote currencies in group: {sorted(quotes)} "
            f"(symbols={symbols}, dependent={dependent})"
        )
    return next(iter(quotes))


def assert_same_exchange(
    symbols: list[str],
    exchange: str,
    dependent: str | None = None,
) -> str:
    """
    Ensure symbols are consistent with the group's exchange.
    Nobitex groups must be IRT-quoted; XT groups must be USDT swap form.
    A group cannot mix a Nobitex IRT symbol with an XT USDT perpetual.
    """
    quote = assert_same_quote(symbols, dependent)
    ex = (exchange or "nobitex").lower()
    if ex == "nobitex" and quote != "IRT":
        raise ValueError(
            f"nobitex groups require IRT quote, got {quote} "
            f"(symbols={symbols}, dependent={dependent})"
        )
    if ex == "xt" and quote != "USDT":
        raise ValueError(
            f"xt groups require USDT settle, got {quote} "
            f"(symbols={symbols}, dependent={dependent})"
        )
    return quote


def close_legs_from_entry(legs_entry: list[dict], close_prices: dict) -> list[dict]:
    out = []
    for leg in legs_entry or []:
        if leg.get("already_closed") or leg.get("failed"):
            # still include for bookkeeping with zero qty close attempt skip
            pass
        sym = leg["symbol"]
        qty = float(leg.get("filled_qty") if leg.get("filled_qty") is not None else leg["qty"])
        out.append({
            "symbol": sym,
            "side": "sell" if leg["side"] == "buy" else "buy",
            "qty": qty,
            "price": float(close_prices.get(sym, leg.get("price") or 0)),
            "position_id": leg.get("position_id"),
            "order_id": leg.get("order_id"),
        })
    return out


def legs_gross_notional(legs: list[dict]) -> float:
    total = 0.0
    for leg in legs or []:
        q = float(leg.get("filled_qty") if leg.get("filled_qty") is not None else leg.get("qty") or 0)
        p = float(leg.get("fill_price") if leg.get("fill_price") is not None else leg.get("price") or 0)
        total += abs(q * p)
    return total


def min_leg_notional_irt(legs: list[dict]) -> float:
    vals = []
    for leg in legs or []:
        if _is_irt(leg.get("symbol", "")):
            vals.append(float(leg["qty"]) * float(leg.get("price") or 0))
    return min(vals) if vals else 0.0


def total_required_collateral(legs: list[dict], leverage: float = 1.0) -> float:
    """Approx collateral at given leverage (sum of leg notionals / leverage)."""
    lev = max(float(leverage), 1e-9)
    return legs_gross_notional(legs) / lev
