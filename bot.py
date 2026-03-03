import os, time
import threading
from dataclasses import dataclass
from typing import Optional, Dict, Any, Tuple

from kalshi_rest import get_orderbook, create_order, get_order

# =========================
# ENV FLAGS (Render-friendly)
# =========================
def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")

def env_float(name: str, default: float) -> float:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return float(v)
    except Exception:
        return default

def env_int(name: str, default: int) -> int:
    v = os.environ.get(name)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default

# =========================
# CONFIG — EDIT THESE
# =========================
TICKER = os.environ.get("TICKER", "KXBTC15M-26MAR030345-45")
POLL_SECONDS = env_int("POLL_SECONDS", 2)

# SAFETY DEFAULTS (start here)
DRY_RUN = env_bool("DRY_RUN", True)      # default SAFE
ARM_LIVE = env_bool("ARM_LIVE", False)   # default SAFE

# Money / risk
BUDGET_USD = env_float("BUDGET_USD", 1.00)
MAX_DAILY_LOSS_USD = env_float("MAX_DAILY_LOSS_USD", 0.25)
FEES_SLIPPAGE_BUFFER_USD = env_float("FEES_SLIPPAGE_BUFFER_USD", 0.20)

# Exit thresholds (cents per contract, based on MID proxy)
STOP_LOSS_CENTS = env_int("STOP_LOSS_CENTS", 2)
TAKE_PROFIT_CENTS = env_int("TAKE_PROFIT_CENTS", 5)

# Strategy behavior (safer)
ALLOW_FLIP_ON_LOSS = env_bool("ALLOW_FLIP_ON_LOSS", False)
ALLOW_REENTER_ON_PROFIT = env_bool("ALLOW_REENTER_ON_PROFIT", True)

COOLDOWN_SECONDS = env_int("COOLDOWN_SECONDS", 20)
MAX_TRADES_PER_SESSION = env_int("MAX_TRADES_PER_SESSION", 20)

# Order pricing aggressiveness
ENTRY_PAY_UP_CENTS = env_int("ENTRY_PAY_UP_CENTS", 1)    # buy at bid+1
EXIT_SLIP_CENTS = env_int("EXIT_SLIP_CENTS", 1)          # (unused in your aggressive exit, kept)
EXIT_RETRIES = env_int("EXIT_RETRIES", 6)                # (unused in your aggressive exit, kept)
EXIT_RETRY_SLEEP = env_float("EXIT_RETRY_SLEEP", 1.0)    # (unused in your aggressive exit, kept)

# Optional extra safety: never auto-dump below this
MIN_EXIT_PRICE_CENTS = env_int("MIN_EXIT_PRICE_CENTS", 1)

KILL_FILE = os.environ.get("KILL_FILE", "STOP.txt")


# =========================
# Helpers
# =========================
def now_hms() -> str:
    return time.strftime("%H:%M:%S")

def money(x: float) -> str:
    sign = "+" if x >= 0 else "-"
    return f"{sign}${abs(x):.2f}"

def cents_to_usd(c: int) -> float:
    return c / 100.0

def parse_ob(ob_json: Dict[str, Any]) -> Tuple[list, list]:
    ob = ob_json.get("orderbook", ob_json)
    return (ob.get("yes") or []), (ob.get("no") or [])

def best_bid(book: list) -> Optional[int]:
    if not book:
        return None
    # levels are [price_cents, qty]
    prices = [int(l[0]) for l in book if l and l[0] is not None]
    return max(prices) if prices else None

def yes_mid_from_bids(yes_bid: Optional[int], no_bid: Optional[int]) -> Optional[int]:
    # Approx YES_ask = 100 - NO_bid, mid ~ (YES_bid + YES_ask)/2
    if yes_bid is None and no_bid is None:
        return None
    if yes_bid is None:
        return 100 - no_bid
    if no_bid is None:
        return yes_bid
    yes_ask = 100 - no_bid
    return int(round((yes_bid + yes_ask) / 2))

def opposite(side: str) -> str:
    return "no" if side == "yes" else "yes"

def price_key(side: str) -> str:
    return "yes_price" if side == "yes" else "no_price"

def calc_contracts(budget_usd: float, entry_price_cents: int) -> int:
    if entry_price_cents <= 0:
        return 0
    return max(1, int(budget_usd / cents_to_usd(entry_price_cents)))

def get_order_id(resp: Dict[str, Any]) -> Optional[str]:
    return resp.get("order_id") or resp.get("id")

def order_filled(order_json: Dict[str, Any]) -> bool:
    status = (order_json.get("status") or order_json.get("order_status") or "").lower()
    if status in ("filled", "executed"):
        return True
    filled = order_json.get("filled_count") or order_json.get("filled_qty") or order_json.get("filled") or 0
    try:
        return int(filled) > 0 and status not in ("canceled", "rejected")
    except Exception:
        return False

def extract_avg_fill_price_cents(order_json: Dict[str, Any], side: str) -> Optional[int]:
    """
    Different accounts return different fields. Try common ones.
    We want the average fill price in cents for YES/NO.
    """
    for k in ("avg_fill_price", "average_fill_price", "avg_price", "fill_price"):
        v = order_json.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, str) and "." in v:
                return int(round(float(v) * 100))
            return int(v)
        except Exception:
            pass

    fills = order_json.get("fills") or []
    if isinstance(fills, list) and fills:
        prices = []
        for f in fills:
            pv = f.get(price_key(side)) or f.get("price") or f.get("price_cents")
            try:
                if isinstance(pv, str) and "." in pv:
                    prices.append(int(round(float(pv) * 100)))
                else:
                    prices.append(int(pv))
            except Exception:
                continue
        if prices:
            return int(round(sum(prices) / len(prices)))

    return None


# =========================
# State
# =========================
@dataclass
class Position:
    side: str                 # yes/no
    contracts: int
    entry_yes_mid: int
    entry_fill_cents: int     # actual avg fill (cents, for that side)
    entry_ts: float


# =========================
# Choose entry side based on HIGHER BIDS only
# =========================
def choose_entry_side(yes_bid: Optional[int], no_bid: Optional[int], mom: int) -> Optional[str]:
    """
    ONLY buys the side with HIGHER bids.
    Never buys the cheaper side.
    """
    if yes_bid is None or no_bid is None:
        return None

    if yes_bid > no_bid:
        print(f"📊 Higher bids: YES ({yes_bid}¢ > {no_bid}¢) → Buying YES")
        return "yes"
    elif no_bid > yes_bid:
        print(f"📊 Higher bids: NO ({no_bid}¢ > {yes_bid}¢) → Buying NO")
        return "no"
    else:
        if mom >= 1:
            print("📊 Bids equal, momentum up → Buying YES")
            return "yes"
        elif mom <= -1:
            print("📊 Bids equal, momentum down → Buying NO")
            return "no"
        else:
            print("📊 Bids equal, no momentum → No trade")
            return None


# =========================
# Trading functions
# =========================
def place_order(
    action: str,
    side: str,
    ticker: str,
    contracts: int,
    price_cents: int,
    tif: Optional[str] = None,
    reduce_only: bool = False
) -> Dict[str, Any]:
    payload = {
        "ticker": ticker,
        "action": action,   # buy/sell
        "side": side,       # yes/no
        "type": "limit",
        "count": int(contracts),
        price_key(side): int(price_cents),
    }
    if tif:
        payload["time_in_force"] = tif  # "immediate_or_cancel"
    if reduce_only:
        payload["reduce_only"] = True

    print(f"SENDING: {payload}")

    if DRY_RUN or not ARM_LIVE:
        return {"status": "dry_run", "order_id": "DRYRUN", "payload": payload}

    return create_order(payload)

def confirm_fill(resp: Dict[str, Any], side: str, contracts: int, timeout_s: float = 2.0) -> Tuple[bool, Optional[int], Optional[Dict[str, Any]]]:
    """
    Returns: (filled?, avg_fill_cents, order_json)
    """
    if DRY_RUN or not ARM_LIVE:
        p = resp.get("payload", {})
        px = p.get(price_key(side))
        return True, int(px) if px is not None else None, resp

    oid = get_order_id(resp)
    if not oid:
        return False, None, None

    end = time.time() + timeout_s
    last = None
    while time.time() < end:
        try:
            last = get_order(oid)
            if order_filled(last):
                avg = extract_avg_fill_price_cents(last, side)
                return True, avg, last
        except Exception:
            pass
        time.sleep(0.3)

    return False, None, last

def print_money_breakdown(side: str, action: str, contracts: int, price_cents: int):
    used = contracts * cents_to_usd(price_cents)
    max_profit = contracts * cents_to_usd(100 - price_cents)
    print("------ MONEY ------")
    print(f"{action.upper()} {side.upper()} x{contracts} @ {price_cents}¢")
    print(f"Capital used: ${used:.2f}")
    print(f"Max profit:   ${max_profit:.2f}")
    print("-------------------")


# =========================
# Main loop
# =========================
def main():
    # Optional: prevent accidental live runs locally
    if (not DRY_RUN) and ARM_LIVE and env_bool("REQUIRE_LIVE_CONFIRM", False):
        confirm = input("LIVE TRADING ENABLED. Type YES to continue: ")
        if confirm.strip().upper() != "YES":
            raise SystemExit("Cancelled.")

    daily_pnl = 0.0
    trades = 0
    pos: Optional[Position] = None
    cooldown_until = 0.0
    last_mid = None

    print(f"Ticker: {TICKER}")
    print(f"DRY_RUN={DRY_RUN} ARM_LIVE={ARM_LIVE} Budget=${BUDGET_USD:.2f}")
    print(f"Hard daily loss cap: -${MAX_DAILY_LOSS_USD:.2f}")
    print(f"Stop Loss: {STOP_LOSS_CENTS}¢ | Take Profit: {TAKE_PROFIT_CENTS}¢")
    print(f"Flip on loss: {ALLOW_FLIP_ON_LOSS} | Re-enter on profit: {ALLOW_REENTER_ON_PROFIT}")
    print(f"MIN_EXIT_PRICE_CENTS={MIN_EXIT_PRICE_CENTS}¢")
    print("Kill switch: create STOP.txt to stop.\n")

    while True:
        if os.path.exists(KILL_FILE):
            print("🛑 STOP.txt found. Exiting.")
            break

        if daily_pnl <= -MAX_DAILY_LOSS_USD:
            print(f"🛑 Daily loss cap hit ({money(daily_pnl)}). Stopping.")
            break

        if trades >= MAX_TRADES_PER_SESSION:
            print("🛑 Max trades reached. Stopping.")
            break

        if time.time() < cooldown_until:
            time.sleep(0.5)
            continue

        try:
            ob = get_orderbook(TICKER)
            yes_book, no_book = parse_ob(ob)
            yb = best_bid(yes_book)
            nb = best_bid(no_book)
            mid = yes_mid_from_bids(yb, nb)
        except Exception as e:
            print(f"[{now_hms()}] Data error: {e}")
            time.sleep(POLL_SECONDS)
            continue

        if mid is None:
            print(f"[{now_hms()}] Waiting… no mid (YES_bid={yb}, NO_bid={nb})")
            time.sleep(POLL_SECONDS)
            continue

        mom = 0 if last_mid is None else (mid - last_mid)
        last_mid = mid

        # =========================
        # FLAT: consider entry
        # =========================
        if pos is None:
            pick = choose_entry_side(yb, nb, mom)
            print(f"[{now_hms()}] FLAT midYES={mid}¢ mom={mom:+d}¢ pick={pick} | DailyPnL={money(daily_pnl)}")

            if pick is None:
                time.sleep(POLL_SECONDS)
                continue

            entry_bid = yb if pick == "yes" else nb
            if entry_bid is None:
                print("No bid on chosen side yet. Waiting…")
                time.sleep(POLL_SECONDS)
                continue

            entry_price = min(99, int(entry_bid) + ENTRY_PAY_UP_CENTS)
            contracts = calc_contracts(BUDGET_USD, entry_price)
            if contracts <= 0:
                print("Budget too small for this price. Waiting…")
                time.sleep(POLL_SECONDS)
                continue

            print_money_breakdown(pick, "buy", contracts, entry_price)
            resp = place_order("buy", pick, TICKER, contracts, entry_price, tif="immediate_or_cancel", reduce_only=False)
            filled, avg_fill, _order = confirm_fill(resp, pick, contracts, timeout_s=3.0)

            if not filled:
                print("❌ Entry not filled. Staying flat.")
                cooldown_until = time.time() + 1.0
                time.sleep(POLL_SECONDS)
                continue

            fill_price = avg_fill if avg_fill is not None else entry_price
            print(f"✅ ENTERED {pick.upper()} x{contracts} fill={fill_price}¢ (midYES ref={mid}¢)")
            trades += 1
            pos = Position(side=pick, contracts=contracts, entry_yes_mid=mid, entry_fill_cents=fill_price, entry_ts=time.time())
            cooldown_until = time.time() + 1.0
            time.sleep(POLL_SECONDS)
            continue

        # =========================
        # IN POSITION: stop / take profit logic
        # =========================
        pnl_cents = (mid - pos.entry_yes_mid) if pos.side == "yes" else (pos.entry_yes_mid - mid)

        current_bid = yb if pos.side == "yes" else nb
        if current_bid is not None:
            actual_delta = float(current_bid) - float(pos.entry_fill_cents)
            actual_loss_cents = max(0.0, -actual_delta)
            actual_profit_cents = max(0.0, actual_delta)
        else:
            actual_loss_cents = 999.0
            actual_profit_cents = 0.0

        print(
            f"[{now_hms()}] IN {pos.side.upper()} | Entry={pos.entry_fill_cents}¢ | "
            f"Bid={current_bid}¢ | Mid PnL={pnl_cents:+d}¢ | "
            f"Actual Loss={actual_loss_cents:.2f}¢ | Actual Profit={actual_profit_cents:.2f}¢ | "
            f"Daily={money(daily_pnl)}"
        )

        exit_reason = None

        if pnl_cents <= -STOP_LOSS_CENTS:
            exit_reason = f"MID_STOP_LOSS ({pnl_cents}¢)"

        if current_bid is not None and actual_loss_cents >= STOP_LOSS_CENTS:
            exit_reason = f"BID_STOP_LOSS ({actual_loss_cents:.2f}¢)"

        if pnl_cents >= TAKE_PROFIT_CENTS:
            exit_reason = f"TAKE_PROFIT ({pnl_cents}¢)"

        if current_bid is not None and actual_profit_cents >= TAKE_PROFIT_CENTS:
            exit_reason = f"BID_TAKE_PROFIT ({actual_profit_cents:.2f}¢)"

        if not exit_reason:
            time.sleep(POLL_SECONDS)
            continue

        # ===== AGGRESSIVE EXIT =====
        print(f"\n🚨🚨🚨 EXIT TRIGGER: {exit_reason} 🚨🚨🚨")
        print(f"🔥 FORCE SELLING {pos.side.upper()} x{pos.contracts} contracts")

        exit_bid = yb if pos.side == "yes" else nb
        if exit_bid is None:
            print("⚠️ No bid found - using minimum price")
            exit_bid = MIN_EXIT_PRICE_CENTS

        exited = False
        exit_fill_price = None

        for attempt in range(15):
            if attempt == 0:
                px = int(exit_bid)
                print(f"Attempt 1: Selling at bid {px}¢")
            elif attempt < 5:
                px = max(MIN_EXIT_PRICE_CENTS, int(exit_bid) - attempt)
                print(f"Attempt {attempt+1}: Selling at {px}¢ (more aggressive)")
            elif attempt < 10:
                px = max(MIN_EXIT_PRICE_CENTS, int(exit_bid) - 5)
                print(f"Attempt {attempt+1}: Selling at {px}¢ (very aggressive)")
            else:
                px = MIN_EXIT_PRICE_CENTS
                print(f"🔥 FIRE SALE: Selling at {px}¢ (min exit)")

            resp = place_order("sell", pos.side, TICKER, pos.contracts, px, tif="immediate_or_cancel", reduce_only=True)
            filled, avg_fill, _ = confirm_fill(resp, pos.side, pos.contracts, timeout_s=2.0)

            if filled:
                exited = True
                exit_fill_price = avg_fill if avg_fill is not None else px
                print(f"✅✅✅ EXIT SUCCESSFUL at {exit_fill_price}¢ ✅✅✅")
                break

            print(f"❌ Attempt {attempt+1} failed, trying again...")
            time.sleep(0.5)

        if not exited:
            print("💀💀💀 CRITICAL: Could not exit after 15 attempts! 💀💀💀")
            print(f"Emergency mode - trying one last time at {MIN_EXIT_PRICE_CENTS}¢")

            resp = place_order("sell", pos.side, TICKER, pos.contracts, MIN_EXIT_PRICE_CENTS, tif="immediate_or_cancel", reduce_only=True)
            filled, avg_fill, _ = confirm_fill(resp, pos.side, pos.contracts, timeout_s=3.0)

            if filled:
                exited = True
                exit_fill_price = avg_fill if avg_fill is not None else MIN_EXIT_PRICE_CENTS
                print(f"✅✅✅ EMERGENCY EXIT at {exit_fill_price}¢ ✅✅✅")
            else:
                print("💀 COULD NOT EXIT - MANUAL INTERVENTION REQUIRED")
                print("Keeping position and will try again next loop")
                time.sleep(POLL_SECONDS)
                continue

        entry_cost = pos.contracts * cents_to_usd(pos.entry_fill_cents)
        exit_proceeds = pos.contracts * cents_to_usd(int(exit_fill_price))
        trade_pnl = (exit_proceeds - entry_cost) - FEES_SLIPPAGE_BUFFER_USD

        daily_pnl += trade_pnl
        print(f"\n📊 TRADE RESULT: {money(trade_pnl)} | Daily Total: {money(daily_pnl)}")

        old_side = pos.side
        pos = None
        cooldown_until = time.time() + COOLDOWN_SECONDS

        if "LOSS" in exit_reason and ALLOW_FLIP_ON_LOSS:
            print(f"🔁 Flip enabled: will consider {opposite(old_side).upper()} after cooldown.")
        elif "PROFIT" in exit_reason and ALLOW_REENTER_ON_PROFIT:
            print("🔁 Re-enter on profit enabled: will look for next setup after cooldown.")
        else:
            print("Staying flat after exit.")

        time.sleep(POLL_SECONDS)


# =========================
# Render-friendly entrypoint
# =========================
if __name__ == "__main__":
    main()
