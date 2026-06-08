"""
demo/buggy_service/app.py
=========================
A realistic-looking Python microservice with **three planted bugs**
for demonstrating autodebug-agent's detection-and-fix pipeline.

Bugs
----
1. ``get_user_score()``   — IndexError  (no bounds check)
2. ``process_order()``    — KeyError    (missing dict key)
3. ``calculate_discount`` — ZeroDivisionError (zero denominator)
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Fake data layer
# ---------------------------------------------------------------------------
USER_SCORES: list[int] = [87, 92, 76, 63, 95]

ORDERS: list[dict[str, Any]] = [
    {"id": "ORD-001", "item": "Widget", "qty": 2, "price": 19.99, "discount_code": "SAVE10"},
    {"id": "ORD-002", "item": "Gadget", "qty": 1, "price": 49.99},  # <-- no discount_code
    {"id": "ORD-003", "item": "Gizmo",  "qty": 5, "price": 9.99,  "discount_code": "HALF"},
]


# ---------------------------------------------------------------------------
# Bug 1: IndexError — accessing a list without bounds check
# ---------------------------------------------------------------------------
def get_user_score(user_index: int) -> str:
    """Return a formatted string with the user's score.

    BUG: no bounds check — will crash if *user_index* >= len(USER_SCORES).
    """
    score = USER_SCORES[user_index]          # <-- BUG: no bounds check
    return f"User #{user_index} scored {score}/100"


# ---------------------------------------------------------------------------
# Bug 2: KeyError — accessing a dict key that may not exist
# ---------------------------------------------------------------------------
def process_order(order: dict[str, Any]) -> str:
    """Apply a discount code and return the order summary.

    BUG: assumes every order dict contains a ``discount_code`` key.
    """
    code = order["discount_code"]            # <-- BUG: key may be missing
    total = order["qty"] * order["price"]
    summary = (
        f"Order {order['id']}: {order['qty']}x {order['item']} "
        f"@ ${order['price']:.2f} = ${total:.2f} (code: {code})"
    )
    return summary


# ---------------------------------------------------------------------------
# Bug 3: ZeroDivisionError — percentage without zero-denominator check
# ---------------------------------------------------------------------------
def calculate_discount(original: float, sold: int, returned: int) -> float:
    """Calculate the effective discount percentage.

    BUG: when ``sold == returned`` the denominator is zero.
    """
    net_sold = sold - returned
    discount_pct = (original / net_sold) * 100  # <-- BUG: net_sold may be 0
    return round(discount_pct, 2)


# ---------------------------------------------------------------------------
# Main — trigger all three bugs and capture tracebacks
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"[{datetime.now().isoformat()}] INFO  buggy_service starting up ...")
    print(f"[{datetime.now().isoformat()}] INFO  Loaded {len(USER_SCORES)} users, {len(ORDERS)} orders")
    print()

    # --- Bug 1: IndexError -----------------------------------------------
    try:
        print(get_user_score(2))     # OK
        print(get_user_score(999))   # BOOM
    except Exception:
        traceback.print_exc()
        print()

    # --- Bug 2: KeyError -------------------------------------------------
    try:
        print(process_order(ORDERS[0]))  # OK (has discount_code)
        print(process_order(ORDERS[1]))  # BOOM (no discount_code)
    except Exception:
        traceback.print_exc()
        print()

    # --- Bug 3: ZeroDivisionError ----------------------------------------
    try:
        print(f"Discount: {calculate_discount(100.0, 5, 2)}%")  # OK
        print(f"Discount: {calculate_discount(100.0, 3, 3)}%")  # BOOM (3-3=0)
    except Exception:
        traceback.print_exc()
        print()

    print(f"[{datetime.now().isoformat()}] ERROR Service encountered fatal errors — exiting")
    sys.exit(1)


if __name__ == "__main__":
    main()
