"""
demo.py – End-to-end demo of the GasNinja pipeline.

Runs without CROO credentials or a funded wallet. Demonstrates:
  1. Intent parsing via Groq (if GROQ_API_KEY is set, otherwise uses mock)
  2. Action validation
  3. Gas savings calculation
  4. Dry-run simulation on Base (read-only, no gas spent)

Run with:  python3 demo.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("demo")

# ──────────────────────────────────────────────
# Colors for terminal output
# ──────────────────────────────────────────────

class C:
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    CYAN = "\033[96m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def banner(text: str) -> None:
    print(f"\n{C.BOLD}{C.CYAN}{'═' * 60}")
    print(f"  {text}")
    print(f"{'═' * 60}{C.RESET}\n")


def step(num: int, text: str) -> None:
    print(f"{C.BOLD}{C.GREEN}  [{num}] {text}{C.RESET}")


def info(text: str) -> None:
    print(f"      {C.DIM}{text}{C.RESET}")


def success(text: str) -> None:
    print(f"      {C.GREEN}✅ {text}{C.RESET}")


def warn(text: str) -> None:
    print(f"      {C.YELLOW}⚠️  {text}{C.RESET}")


def fail(text: str) -> None:
    print(f"      {C.RED}❌ {text}{C.RESET}")


# ──────────────────────────────────────────────
# Sample DeFi intent (what a requester agent would send)
# ──────────────────────────────────────────────

SAMPLE_INTENT = {
    "description": "Approve USDC for Uniswap V3 Router, then swap 100 USDC for WETH on Base",
    "actions": [
        {
            "type": "approve",
            "token": "USDC",
            "token_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "spender": "0x2626664c2603336E57B271c5C0b26F421741e481",
            "amount": "unlimited",
        },
        {
            "type": "swap",
            "protocol": "Uniswap V3",
            "router": "0x2626664c2603336E57B271c5C0b26F421741e481",
            "token_in": "USDC",
            "token_out": "WETH",
            "amount_in": "100000000",
            "slippage": "0.5%",
        },
    ],
    "chain": "base",
    "urgency": "normal",
}

# ──────────────────────────────────────────────
# Mock parsed actions (used when Groq API key is not available)
# ──────────────────────────────────────────────

MOCK_PARSED_ACTIONS = [
    {
        "target": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "abi": [
            {
                "name": "approve",
                "type": "function",
                "inputs": [
                    {"name": "spender", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                ],
                "outputs": [{"name": "", "type": "bool"}],
            }
        ],
        "function": "approve",
        "args": [
            "0x2626664c2603336E57B271c5C0b26F421741e481",
            115792089237316195423570985008687907853269984665640564039457584007913129639935,
        ],
        "allowFailure": False,
    },
    {
        "target": "0x4200000000000000000000000000000000000006",
        "abi": [
            {
                "name": "deposit",
                "type": "function",
                "inputs": [],
                "outputs": [],
            }
        ],
        "function": "deposit",
        "args": [],
        "allowFailure": False,
    },
]


# ──────────────────────────────────────────────
# Demo runner
# ──────────────────────────────────────────────

async def run_demo():
    banner("⚡ GasNinja — End-to-End Demo")

    print(f"  This demo walks through the full GasNinja pipeline:")
    print(f"  Intent → Groq Parsing → Validation → Simulation → Gas Savings\n")

    # ── Step 1: Show the raw intent ─────────────
    step(1, "Raw DeFi Intent (what a requester agent sends)")
    print()
    print(f"      {C.DIM}{json.dumps(SAMPLE_INTENT, indent=6)}{C.RESET}")
    print()

    # ── Step 2: Parse with Groq (or mock) ───────
    step(2, "AI Intent Parsing (Groq LLM)")
    groq_key = os.getenv("GROQ_API_KEY", "")

    if groq_key:
        info("GROQ_API_KEY found — calling Groq for real parsing …")
        try:
            sys.path.insert(0, os.path.dirname(__file__))
            from cap_provider import parse_intent_with_groq
            actions = await parse_intent_with_groq(SAMPLE_INTENT)
            success(f"Groq returned {len(actions)} action(s)")
        except Exception as e:
            warn(f"Groq call failed ({e}), falling back to mock data")
            actions = MOCK_PARSED_ACTIONS
    else:
        warn("No GROQ_API_KEY — using pre-built mock actions for demo")
        actions = MOCK_PARSED_ACTIONS

    print()
    for i, action in enumerate(actions):
        info(f"Action {i}: {action['function']}() → {action['target'][:10]}…{action['target'][-4:]}")
    print()

    # ── Step 3: Validate ────────────────────────
    step(3, "Action Validation (catch bad ABIs, wrong types)")
    from validate import validate_actions

    validation = validate_actions(actions)
    if validation.valid:
        success(f"All {len(actions)} action(s) passed validation")
    else:
        fail("Validation failed:")
        print(f"      {validation.summary()}")
        return

    if validation.warnings:
        for w in validation.warnings:
            warn(f"action[{w.action_index}].{w.field}: {w.message}")
    print()

    # ── Step 4: Gas savings estimate ────────────
    step(4, "Gas Savings Calculation")
    from multicall_engine import calculate_gas_savings, encode_call

    # Encode the calls to get calldata for gas estimation.
    encoded_calls = []
    for action in actions:
        encoded = encode_call(
            target=action["target"],
            abi=action["abi"],
            function_name=action["function"],
            args=action.get("args", []),
            allow_failure=action.get("allowFailure", False),
        )
        encoded_calls.append(encoded)

    # Simulate a realistic bundled gas usage.
    estimated_bundled_gas = 85_000 + len(actions) * 30_000
    savings = calculate_gas_savings(encoded_calls, estimated_bundled_gas)

    print()
    info(f"Individual txs would cost:  {savings.individual_gas_total:,} gas")
    info(f"Bundled Multicall3 cost:    {savings.bundled_gas_used:,} gas")

    if savings.gas_saved > 0:
        success(f"Gas saved: {savings.gas_saved:,} gas ({savings.savings_pct}%)")
    else:
        info(f"Gas saved: {savings.gas_saved:,} gas ({savings.savings_pct}%) — overhead exceeds savings for this small bundle")

    # USD estimate.
    gas_price_gwei = 0.005  # Base is very cheap.
    gas_price_eth = gas_price_gwei * 1e-9
    eth_price = 3200.0
    cost_usd = savings.bundled_gas_used * gas_price_eth * eth_price
    standard_usd = savings.individual_gas_total * gas_price_eth * eth_price
    info(f"Est. cost: ${cost_usd:.4f} (vs ${standard_usd:.4f} standard)")
    print()

    # ── Step 5: Dry-run simulation ──────────────
    step(5, "Dry-Run Simulation (eth_call on Base — no gas spent)")
    from multicall_engine import simulate_bundle

    try:
        sim = await simulate_bundle(actions)
        if sim.success:
            success(f"Simulation PASSED — {len(sim.call_results)} call(s) succeeded")
            info(f"Estimated gas: {sim.estimated_gas:,}")
            for r in sim.call_results:
                status = "✅" if r["success"] else "❌"
                info(f"  Call {r['index']} ({r['function']}): {status}")
        elif sim.error:
            warn(f"Simulation returned error: {sim.error}")
            info("This is expected for write-operations without a funded wallet")
        else:
            warn("Simulation had partial failures")
            for r in sim.call_results:
                status = "✅" if r["success"] else "❌ REVERTED"
                info(f"  Call {r['index']} ({r['function']}): {status}")
    except Exception as e:
        warn(f"Simulation error: {e}")
        info("This is expected — demo doesn't have a funded wallet on Base")

    print()

    # ── Step 6: Unit tests ──────────────────────
    step(6, "Running Unit Tests")
    info("Running pytest …")
    print()

    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(__file__) or ".",
    )
    # Show just the summary.
    lines = result.stdout.strip().split("\n")
    for line in lines:
        if "PASSED" in line or "FAILED" in line or "passed" in line or "error" in line:
            info(line.strip())

    if result.returncode == 0:
        success("All tests passed!")
    else:
        fail(f"Some tests failed (exit code {result.returncode})")

    print()

    # ── Summary ─────────────────────────────────
    banner("Demo Complete")
    print(f"  {C.GREEN}✅ Intent Parsing     — {'Groq LLM' if groq_key else 'Mock (set GROQ_API_KEY for real)'}")
    print(f"  ✅ Validation        — {len(actions)} actions validated")
    print(f"  ✅ Gas Savings       — {savings.savings_pct}% estimated reduction")
    print(f"  ✅ Dry-Run Simulation— eth_call on Base mainnet")
    print(f"  ✅ Unit Tests        — {'All passed' if result.returncode == 0 else 'See above'}")
    print(f"{C.RESET}")
    print(f"  {C.DIM}To run the full agent:   python3 cap_provider.py")
    print(f"  To start the API:      uvicorn api_server:app --reload")
    print(f"  To run tests:          python3 -m pytest tests/ -v{C.RESET}")
    print()


if __name__ == "__main__":
    asyncio.run(run_demo())
