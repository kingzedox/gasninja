"""
api_server.py – FastAPI metrics API for the GasNinja dashboard.

Serves bundle history, gas savings stats, and live metrics that
the Vercel frontend fetches to populate the dashboard.

Run with:  uvicorn api_server:app --host 0.0.0.0 --port 8000 --reload
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

logger = logging.getLogger("api_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")

# ──────────────────────────────────────────────
# App
# ──────────────────────────────────────────────
app = FastAPI(
    title="GasNinja API",
    description="Metrics API for the GasNinja Multicall3 Bundler Agent dashboard.",
    version="1.0.0",
)

# Allow the Vercel frontend to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, restrict to your Vercel domain.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# In-memory store (populated by the agent or demo data)
# ──────────────────────────────────────────────

@dataclass
class BundleRecord:
    tx_hash: str
    actions_count: int
    gas_used: int
    gas_saved: int
    savings_pct: float
    cost_usd: float
    standard_cost_usd: float
    timestamp: str
    status: str = "confirmed"
    action_labels: str = ""  # e.g. "Approve + Swap"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# In-memory bundle history — the agent appends here, API reads from here.
bundle_history: list[BundleRecord] = []

# ──────────────────────────────────────────────
# Generate realistic demo data
# ──────────────────────────────────────────────

ACTION_COMBOS = [
    (2, "Approve + Swap"),
    (3, "Approve + Swap + Deposit"),
    (3, "Swap + Deposit + Stake"),
    (4, "Approve + Swap + Deposit + Stake"),
    (5, "Approve + Swap + Bridge + Deposit + Stake"),
    (2, "Approve + Transfer"),
    (6, "Full DeFi Bundle"),
    (3, "Claim + Swap + Deposit"),
    (4, "Unstake + Claim + Swap + Bridge"),
    (5, "Multi-token Approve + Swap"),
]

ETH_PRICE_USD = 3200.0  # Approximate for cost calculations.
BASE_GAS_PRICE_GWEI = 0.005  # Base network gas is very cheap.


def _random_tx_hash() -> str:
    seed = f"{time.time()}-{random.random()}"
    return "0x" + hashlib.sha256(seed.encode()).hexdigest()[:64]


def _generate_demo_bundle(seconds_ago: int = 0) -> BundleRecord:
    """Generate a single realistic-looking bundle record."""
    actions_count, label = random.choice(ACTION_COMBOS)

    # Individual gas: each tx costs ~21000 base + 30000-80000 execution.
    per_call_gas = 21_000 + random.randint(30_000, 80_000)
    individual_total = actions_count * per_call_gas

    # Bundled: save the (N-1) × 21000 base overhead, plus some multicall efficiency.
    multicall_overhead = random.randint(5_000, 15_000)
    bundled_gas = individual_total - (actions_count - 1) * 21_000 + multicall_overhead
    gas_saved = max(0, individual_total - bundled_gas)
    savings_pct = round((gas_saved / individual_total) * 100, 1) if individual_total > 0 else 0

    # Cost in USD.
    gas_price_eth = BASE_GAS_PRICE_GWEI * 1e-9
    cost_usd = round(bundled_gas * gas_price_eth * ETH_PRICE_USD, 4)
    standard_cost_usd = round(individual_total * gas_price_eth * ETH_PRICE_USD, 4)

    ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)

    return BundleRecord(
        tx_hash=_random_tx_hash(),
        actions_count=actions_count,
        gas_used=bundled_gas,
        gas_saved=gas_saved,
        savings_pct=savings_pct,
        cost_usd=cost_usd,
        standard_cost_usd=standard_cost_usd,
        timestamp=ts.isoformat(),
        action_labels=label,
    )


def seed_demo_data(count: int = 50) -> None:
    """Pre-populate bundle history with realistic demo data."""
    bundle_history.clear()
    for i in range(count):
        seconds_ago = (count - i) * random.randint(15, 120)
        bundle_history.append(_generate_demo_bundle(seconds_ago))
    logger.info("🎲 Seeded %d demo bundles", count)


# ──────────────────────────────────────────────
# API endpoints
# ──────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Seed demo data on startup."""
    seed_demo_data(50)


@app.get("/api/health")
async def health():
    return {"status": "ok", "agent": "GasNinja", "version": "1.0.0"}


@app.get("/api/metrics")
async def get_metrics():
    """
    Aggregate metrics for the dashboard stat cards.

    Returns:
        total_gas_saved_usd:   Total dollar value of gas saved.
        avg_cost_per_strategy: Average cost per bundled strategy in USD.
        avg_standard_cost:     Average cost without bundling.
        savings_reduction_pct: Percentage reduction vs. standard.
        bundled_executions:    Total number of bundled executions.
        active_agents:         Number of active agents (demo value).
    """
    if not bundle_history:
        return {
            "total_gas_saved_usd": 0,
            "avg_cost_per_strategy": 0,
            "avg_standard_cost": 0,
            "savings_reduction_pct": 0,
            "bundled_executions": 0,
            "active_agents": 0,
        }

    total_saved_usd = sum(
        b.standard_cost_usd - b.cost_usd for b in bundle_history
    )
    avg_cost = sum(b.cost_usd for b in bundle_history) / len(bundle_history)
    avg_standard = sum(b.standard_cost_usd for b in bundle_history) / len(bundle_history)
    reduction_pct = round((1 - avg_cost / avg_standard) * 100, 1) if avg_standard > 0 else 0

    return {
        "total_gas_saved_usd": round(total_saved_usd, 2),
        "avg_cost_per_strategy": round(avg_cost, 2),
        "avg_standard_cost": round(avg_standard, 2),
        "savings_reduction_pct": reduction_pct,
        "bundled_executions": len(bundle_history),
        "active_agents": random.randint(380, 420),
    }


@app.get("/api/bundles")
async def get_bundles(limit: int = 20):
    """
    Recent bundle history for the "Live Bundles" feed.
    Returns newest first.
    """
    recent = sorted(bundle_history, key=lambda b: b.timestamp, reverse=True)[:limit]
    return {
        "bundles": [b.to_dict() for b in recent],
        "total": len(bundle_history),
    }


@app.get("/api/chart/complexity")
async def get_complexity_chart():
    """
    Data for the "Execution Cost by Complexity" bar chart.
    Groups bundles by action count and compares standard vs. GasNinja cost.
    """
    # Group by action count buckets.
    buckets: dict[int, dict[str, list[float]]] = {}
    for b in bundle_history:
        count = b.actions_count
        if count not in buckets:
            buckets[count] = {"standard": [], "gasninja": [], "labels": []}
        buckets[count]["standard"].append(b.standard_cost_usd)
        buckets[count]["gasninja"].append(b.cost_usd)
        buckets[count]["labels"].append(b.action_labels)

    chart_data = []
    for count in sorted(buckets.keys()):
        data = buckets[count]
        # Pick the most common label for this bucket.
        label = max(set(data["labels"]), key=data["labels"].count)
        chart_data.append({
            "actions_count": count,
            "label": label,
            "standard_avg_usd": round(sum(data["standard"]) / len(data["standard"]), 4),
            "gasninja_avg_usd": round(sum(data["gasninja"]) / len(data["gasninja"]), 4),
            "sample_size": len(data["standard"]),
        })

    return {"chart": chart_data}


@app.get("/api/chart/savings_over_time")
async def get_savings_over_time():
    """
    Time-series data showing cumulative gas savings.
    """
    sorted_bundles = sorted(bundle_history, key=lambda b: b.timestamp)
    cumulative = 0.0
    points = []
    for b in sorted_bundles:
        cumulative += b.standard_cost_usd - b.cost_usd
        points.append({
            "timestamp": b.timestamp,
            "cumulative_saved_usd": round(cumulative, 2),
            "savings_pct": b.savings_pct,
        })

    return {"series": points}


# ──────────────────────────────────────────────
# Public function: called by cap_provider after a real bundle executes
# ──────────────────────────────────────────────

def record_bundle(
    tx_hash: str,
    actions_count: int,
    gas_used: int,
    gas_saved: int,
    savings_pct: float,
    action_labels: str = "",
) -> None:
    """
    Record a real bundle execution for the dashboard.
    Called from cap_provider.on_order_paid after a successful delivery.
    """
    gas_price_eth = BASE_GAS_PRICE_GWEI * 1e-9
    cost_usd = round(gas_used * gas_price_eth * ETH_PRICE_USD, 4)
    individual_gas = gas_used + gas_saved
    standard_cost_usd = round(individual_gas * gas_price_eth * ETH_PRICE_USD, 4)

    record = BundleRecord(
        tx_hash=tx_hash,
        actions_count=actions_count,
        gas_used=gas_used,
        gas_saved=gas_saved,
        savings_pct=savings_pct,
        cost_usd=cost_usd,
        standard_cost_usd=standard_cost_usd,
        timestamp=datetime.now(timezone.utc).isoformat(),
        action_labels=action_labels,
    )
    bundle_history.append(record)
    logger.info("📊 Recorded bundle %s for dashboard", tx_hash[:10])
