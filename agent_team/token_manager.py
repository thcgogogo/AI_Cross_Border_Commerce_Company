"""
token_manager.py — The team's "CFO": tracks and limits MONEY spending (in ¥).

This is a deliberately DETERMINISTIC agent (plain accounting, not an LLM) so the
numbers are always exact. It works in Chinese Yuan (¥): each model's real
per-token US$ price is converted to ¥ as calls happen, and the manager:

  - keeps a running total of ¥ spent vs. an overall budget,
  - enforces per-task budgets ("only ¥1,000 for video generation"),
  - warns when a budget drops below 10% remaining, and
  - hard-stops ("cuts off") a task once its budget is exhausted, BEFORE the next
    API call is made — so you can never blow past a limit.

It is thread-safe (the background job records spend while the website reads
status) and saves itself to a JSON file so budgets and spend survive restarts.

If it loads an OLD token-based file (from before the move to money), it discards
those token counts and re-seeds the ¥ budgets from config — usage starts fresh.
"""

import json
import os
import threading
from datetime import datetime, timezone

# Bumps when the on-disk schema changes meaning. v1 stored token COUNTS; v2 (now)
# stores MONEY in ¥. A file without this marker is treated as v1 and reset.
CURRENCY = "CNY"


class BudgetExceededError(RuntimeError):
    """Raised to CUT OFF spending when a task or the overall budget is exhausted."""

    def __init__(self, budget_name, message):
        self.budget_name = budget_name
        super().__init__(message)


class TokenManager:
    def __init__(self, store_path, total_budget=None, warn_at=0.10, task_budgets=None):
        self._lock = threading.RLock()
        self.store_path = store_path
        self.warn_at = warn_at
        # A saved ¥ file is the source of truth. A fresh start — OR an old
        # token-based file (no "currency": "CNY" marker) — seeds the ¥ budgets
        # from config and starts spend at zero (old token counts are discarded).
        loaded = self._load()
        if loaded is not None and loaded.get("currency") == CURRENCY:
            self._state = loaded
            self._state.setdefault("total_budget", total_budget)
            self._state.setdefault("total_used", 0.0)
            self._state.setdefault("budgets", {})
        else:
            self._state = {
                "currency": CURRENCY,
                "total_budget": total_budget,
                "total_used": 0.0,
                "budgets": {
                    name: {"limit": limit, "used": 0.0}
                    for name, limit in (task_budgets or {}).items()
                },
                "updated_at": _now(),
            }
        self._state["currency"] = CURRENCY
        self._save()

    # ---- configuration ----------------------------------------------------
    def set_total_budget(self, limit):
        with self._lock:
            self._state["total_budget"] = _norm_limit(limit)
            self._touch()

    def set_budget(self, name, limit):
        """Create or update a named task budget."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Budget name cannot be empty.")
        with self._lock:
            entry = self._state["budgets"].get(name, {"used": 0})
            entry["limit"] = _norm_limit(limit)
            self._state["budgets"][name] = entry
            self._touch()

    def remove_budget(self, name):
        with self._lock:
            self._state["budgets"].pop(name, None)
            self._touch()

    def reset_usage(self):
        """Zero out all spend counters but keep the budgets/limits."""
        with self._lock:
            self._state["total_used"] = 0.0
            for b in self._state["budgets"].values():
                b["used"] = 0.0
            self._touch()

    # ---- enforcement ------------------------------------------------------
    def ensure_can_spend(self, budget_name):
        """Raise BudgetExceededError if there's nothing left to spend."""
        with self._lock:
            total_budget = self._state["total_budget"]
            if total_budget is not None and self._state["total_used"] >= total_budget:
                raise BudgetExceededError(
                    "total",
                    f"Overall budget of {_cny(total_budget)} is used up.",
                )
            b = self._state["budgets"].get(budget_name)
            if b and b.get("limit") is not None and b["used"] >= b["limit"]:
                raise BudgetExceededError(
                    budget_name,
                    f"Budget '{budget_name}' ({_cny(b['limit'])}) is used up.",
                )

    def record_cost(self, budget_name, cny):
        """Add a call's real ¥ cost to the total and the named budget."""
        try:
            amount = float(cny or 0.0)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        with self._lock:
            self._state["total_used"] = round(self._state["total_used"] + amount, 6)
            b = self._state["budgets"].setdefault(budget_name, {"limit": None, "used": 0.0})
            b["used"] = round(b.get("used", 0.0) + amount, 6)
            self._touch()

    # ---- reads ------------------------------------------------------------
    def status(self):
        """A full snapshot for the API / UI, with computed remaining %, state, warnings."""
        with self._lock:
            total_budget = self._state["total_budget"]
            total_used = self._state["total_used"]
            budgets = []
            warnings = []
            for name, b in sorted(self._state["budgets"].items()):
                info = self._budget_info(name, b["limit"], b["used"])
                budgets.append(info)
                if info["state"] == "warning":
                    warnings.append(
                        f"Budget '{name}' is low: {info['pct_remaining']}% left "
                        f"({_cny(info['remaining'])} of {_cny(b['limit'])})."
                    )
                elif info["state"] == "exceeded":
                    warnings.append(f"Budget '{name}' is used up — that task is cut off.")

            total = self._budget_info("Overall", total_budget, total_used)
            if total["state"] == "warning":
                warnings.insert(0, f"Overall budget is low: {total['pct_remaining']}% left.")
            elif total["state"] == "exceeded":
                warnings.insert(0, "Overall budget is used up — all tasks are cut off.")

            return {
                "currency": CURRENCY,
                "total": total,
                "budgets": budgets,
                "warnings": warnings,
                "updated_at": self._state.get("updated_at"),
            }

    def _budget_info(self, name, limit, used):
        used = round(float(used or 0.0), 4)
        if limit is None:
            return {"name": name, "limit": None, "used": used, "remaining": None,
                    "pct_used": None, "pct_remaining": None, "state": "ok", "unlimited": True}
        remaining = round(max(limit - used, 0), 4)
        pct_used = round(100 * used / limit, 1) if limit else 0.0
        pct_remaining = round(100 * remaining / limit, 1) if limit else 0.0
        if used >= limit:
            state = "exceeded"
        elif remaining <= self.warn_at * limit:
            state = "warning"
        else:
            state = "ok"
        return {"name": name, "limit": limit, "used": used, "remaining": remaining,
                "pct_used": pct_used, "pct_remaining": pct_remaining,
                "state": state, "unlimited": False}

    # ---- persistence ------------------------------------------------------
    def _touch(self):
        self._state["updated_at"] = _now()
        self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.store_path), exist_ok=True)
        tmp = self.store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.store_path)  # atomic

    def _load(self):
        if not os.path.exists(self.store_path):
            return None
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None


def _norm_limit(limit):
    """Turn user input into a positive ¥ budget, or None for 'unlimited'."""
    if limit in (None, "", 0, "0"):
        return None
    limit = round(float(limit), 2)
    return limit if limit > 0 else None


def _cny(amount):
    """Format a ¥ amount for warning messages, e.g. 1234.5 -> '¥1,234.50'."""
    return f"¥{float(amount or 0.0):,.2f}"


def _now():
    return datetime.now(timezone.utc).isoformat()
