"""
multicall_engine.py – Web3 Multicall3 bundler for Base mainnet.

Takes an array of call descriptors, encodes them into a single
Multicall3.aggregate3() transaction, signs with a private key from .env,
and broadcasts to Base.  Returns a BundleResult with the transaction hash,
gas metrics, and savings breakdown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
logger = logging.getLogger("multicall_engine")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(levelname)s | %(message)s")

BASE_RPC_URL: str = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
MULTICALL3_ADDRESS: str = Web3.to_checksum_address(
    os.getenv("MULTICALL3_ADDRESS", "0xcA11bde05977b3631167028862bE2a173976CA11")
)

# Base mainnet chain ID.
CHAIN_ID: int = 8453

# Fixed overhead per individual transaction on any EVM chain (21 000 gas).
INDIVIDUAL_TX_BASE_GAS: int = 21_000

# Minimal ABI – only the aggregate3 function we need.
MULTICALL3_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {"internalType": "bool", "name": "allowFailure", "type": "bool"},
                    {"internalType": "bytes", "name": "callData", "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Call3[]",
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"internalType": "bool", "name": "success", "type": "bool"},
                    {"internalType": "bytes", "name": "returnData", "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Result[]",
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "payable",
        "type": "function",
    }
]

# ──────────────────────────────────────────────
# Web3 singleton
# ──────────────────────────────────────────────
w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
# Base is a PoA (Optimism-derived) chain — inject the extra-data middleware.
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

multicall_contract = w3.eth.contract(address=MULTICALL3_ADDRESS, abi=MULTICALL3_ABI)


def _get_account():
    """Derive account from the private key stored in .env."""
    if not PRIVATE_KEY:
        raise EnvironmentError("PRIVATE_KEY is not set in .env")
    # Auto-prefix 0x if missing (common when copying from MetaMask).
    key = PRIVATE_KEY if PRIVATE_KEY.startswith("0x") else f"0x{PRIVATE_KEY}"
    return w3.eth.account.from_key(key)


# ──────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────

@dataclass
class GasSavings:
    """Breakdown of gas saved by bundling vs. sending N individual txs."""

    individual_gas_total: int = 0       # Sum of (21 000 + calldata gas) per call
    bundled_gas_used: int = 0           # Actual gasUsed from the mined receipt
    gas_saved: int = 0                  # individual_gas_total - bundled_gas_used
    savings_pct: float = 0.0            # gas_saved / individual_gas_total * 100
    num_calls: int = 0
    individual_tx_base_overhead: int = INDIVIDUAL_TX_BASE_GAS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SimulationResult:
    """Result of a dry-run eth_call simulation."""

    success: bool = False
    call_results: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    estimated_gas: int = 0


@dataclass
class BundleResult:
    """Complete result returned by execute_bundle."""

    tx_hash: str = ""
    chain_id: int = CHAIN_ID
    network: str = "base-mainnet"
    explorer_url: str = ""
    block_number: int = 0
    gas_used: int = 0
    gas_savings: GasSavings = field(default_factory=GasSavings)
    actions_count: int = 0
    status: str = "confirmed"
    execution_time_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ──────────────────────────────────────────────
# Call encoding helpers
# ──────────────────────────────────────────────

def _coerce_args(abi: list[dict[str, Any]], function_name: str, args: list[Any]) -> list[Any]:
    """
    Coerce argument types to match the ABI.

    Groq often returns uint256/int256 values as JSON strings (e.g.
    "115792089...935").  web3.py requires native Python ints for
    integer Solidity types.  This function finds the ABI entry for
    *function_name* and converts string-encoded integers to int.
    """
    # Find the matching ABI entry.
    fn_abi = None
    for entry in abi:
        if isinstance(entry, dict) and entry.get("name") == function_name:
            fn_abi = entry
            break

    if fn_abi is None or "inputs" not in fn_abi:
        return args  # Can't coerce without ABI info — return as-is.

    inputs = fn_abi["inputs"]
    coerced = list(args)  # shallow copy

    for i, (arg, inp) in enumerate(zip(coerced, inputs)):
        sol_type = inp.get("type", "")

        # Convert string integers to int for uint*/int* types.
        if (sol_type.startswith("uint") or sol_type.startswith("int")) and isinstance(arg, str):
            try:
                coerced[i] = int(arg)
            except ValueError:
                pass  # Leave as-is; validation will catch it later.

        # Convert string booleans.
        elif sol_type == "bool" and isinstance(arg, str):
            coerced[i] = arg.lower() in ("true", "1", "yes")

    return coerced

def encode_call(
    target: str,
    abi: list[dict[str, Any]],
    function_name: str,
    args: list[Any] | None = None,
    allow_failure: bool = False,
) -> tuple[str, bool, bytes]:
    """
    Encode a single smart-contract call into the (target, allowFailure, callData)
    tuple expected by Multicall3.aggregate3.

    Parameters
    ----------
    target : str
        Contract address (checksummed or not).
    abi : list
        Full or partial ABI containing *at least* the target function.
    function_name : str
        Solidity function name (e.g. ``"approve"``).
    args : list | None
        Positional arguments for the function call.
    allow_failure : bool
        If True, the call may revert without reverting the whole bundle.

    Returns
    -------
    tuple  (address, allowFailure, callData)
    """
    target = Web3.to_checksum_address(target)
    contract = w3.eth.contract(address=target, abi=abi)

    # Coerce args: Groq often returns uint256 values as strings.
    # web3.py needs native Python ints for integer ABI types.
    coerced_args = _coerce_args(abi, function_name, args or [])

    call_data: bytes = contract.encode_abi(function_name, args=coerced_args)
    return (target, allow_failure, call_data)


def _build_calls(actions: list[dict[str, Any]]) -> list[tuple[str, bool, bytes]]:
    """
    Transform the user-facing action list into aggregate3 call tuples.

    Each action dict must contain:
        - target   (str):  Contract address.
        - abi      (list): Contract ABI (or a partial ABI with the needed fn).
        - function (str):  Function name.
        - args     (list): Function arguments.
        - allowFailure (bool, optional): defaults to False.
    """
    calls: list[tuple[str, bool, bytes]] = []
    for idx, action in enumerate(actions):
        try:
            calls.append(
                encode_call(
                    target=action["target"],
                    abi=action["abi"],
                    function_name=action["function"],
                    args=action.get("args", []),
                    allow_failure=action.get("allowFailure", False),
                )
            )
        except Exception as exc:
            logger.error("Failed to encode action #%d (%s): %s", idx, action.get("function", "?"), exc)
            raise ValueError(f"Action #{idx} encoding failed: {exc}") from exc
    return calls


# ──────────────────────────────────────────────
# Gas savings calculator
# ──────────────────────────────────────────────

def calculate_gas_savings(
    calls: list[tuple[str, bool, bytes]],
    bundled_gas_used: int,
) -> GasSavings:
    """
    Estimate how much gas was saved by bundling N calls into one Multicall3 tx
    versus sending N separate transactions.

    The estimate is conservative:
      individual_cost = N × 21 000  (base tx overhead)
                      + Σ calldata_gas_i  (16 gas per non-zero byte, 4 per zero)

    This does NOT include execution gas for each individual call (which would
    also be duplicated), so the real savings are even higher.
    """
    num_calls = len(calls)

    # Calculate calldata cost for each individual tx.
    total_calldata_gas = 0
    for _target, _allow, call_data in calls:
        for byte in call_data:
            total_calldata_gas += 16 if byte != 0 else 4

    individual_gas_total = (num_calls * INDIVIDUAL_TX_BASE_GAS) + total_calldata_gas
    gas_saved = max(0, individual_gas_total - bundled_gas_used)
    savings_pct = (gas_saved / individual_gas_total * 100) if individual_gas_total > 0 else 0.0

    return GasSavings(
        individual_gas_total=individual_gas_total,
        bundled_gas_used=bundled_gas_used,
        gas_saved=gas_saved,
        savings_pct=round(savings_pct, 2),
        num_calls=num_calls,
    )


# ──────────────────────────────────────────────
# Dry-run simulation (eth_call preflight)
# ──────────────────────────────────────────────

async def simulate_bundle(actions: list[dict[str, Any]]) -> SimulationResult:
    """
    Simulate the Multicall3 bundle via eth_call WITHOUT broadcasting.
    Returns decoded per-call success/failure results and the gas estimate.

    Use this to catch reverts before spending real gas.
    """
    if not actions:
        return SimulationResult(success=False, error="Empty actions list")

    logger.info("🧪 Simulating %d action(s) via eth_call …", len(actions))

    try:
        calls = _build_calls(actions)
        account = _get_account()
        sender = account.address

        # Build the tx for estimation (no gas fields needed for eth_call).
        tx = multicall_contract.functions.aggregate3(calls).build_transaction(
            {
                "from": sender,
                "nonce": 0,  # Doesn't matter for simulation.
                "chainId": CHAIN_ID,
                "maxFeePerGas": 0,
                "maxPriorityFeePerGas": 0,
            }
        )

        # eth_call — will revert if any non-allowFailure call fails.
        raw_result = await asyncio.to_thread(
            w3.eth.call, {"to": tx["to"], "from": sender, "data": tx["data"]}
        )

        # Decode the aggregate3 return value: Result[] = (bool success, bytes returnData)[]
        decoded = multicall_contract.decode_function_result("aggregate3", raw_result)
        call_results = []
        for i, (success, return_data) in enumerate(decoded[0]):
            call_results.append({
                "index": i,
                "success": success,
                "return_data_hex": return_data.hex() if return_data else "",
                "function": actions[i].get("function", "unknown"),
            })

        # Gas estimate.
        estimated_gas = await asyncio.to_thread(w3.eth.estimate_gas, tx)

        all_ok = all(r["success"] for r in call_results)
        logger.info(
            "🧪 Simulation %s  |  %d/%d calls succeeded  |  est. gas: %s",
            "PASSED ✅" if all_ok else "PARTIAL ⚠️",
            sum(1 for r in call_results if r["success"]),
            len(call_results),
            estimated_gas,
        )

        return SimulationResult(
            success=all_ok,
            call_results=call_results,
            estimated_gas=estimated_gas,
        )

    except Exception as exc:
        logger.error("🧪 Simulation FAILED ❌: %s", exc)
        return SimulationResult(success=False, error=str(exc))


# ──────────────────────────────────────────────
# Core public API
# ──────────────────────────────────────────────

async def execute_bundle(
    actions: list[dict[str, Any]],
    skip_simulation: bool = False,
) -> BundleResult:
    """
    Bundle *actions* into a single Multicall3 transaction, sign, broadcast,
    and wait for the receipt.

    Parameters
    ----------
    actions : list[dict]
        Each dict has keys: target, abi, function, args, allowFailure (opt).
    skip_simulation : bool
        If True, skip the dry-run eth_call preflight.

    Returns
    -------
    BundleResult
        Full result with tx hash, gas metrics, and savings breakdown.

    Raises
    ------
    ValueError  – if any call encoding fails or simulation shows a revert.
    RuntimeError – if the transaction reverts on-chain.
    """
    t_start = time.monotonic()

    if not actions:
        raise ValueError("actions list is empty – nothing to bundle")

    # ── Step 1: Dry-run simulation ──────────────────────────────
    if not skip_simulation:
        sim = await simulate_bundle(actions)
        if not sim.success:
            failed = [r for r in sim.call_results if not r["success"]]
            detail = f"Reverted calls: {failed}" if failed else sim.error
            raise ValueError(f"Simulation failed — aborting before spending gas. {detail}")
        logger.info("🧪 Preflight passed — proceeding to broadcast.")

    # ── Step 2: Encode & build transaction ──────────────────────
    logger.info("🔧 Encoding %d action(s) into a Multicall3 bundle …", len(actions))
    calls = _build_calls(actions)

    account = _get_account()
    sender = account.address

    nonce = await asyncio.to_thread(w3.eth.get_transaction_count, sender, "pending")
    base_fee = await asyncio.to_thread(lambda: w3.eth.get_block("latest")["baseFeePerGas"])

    # EIP-1559 gas pricing — generous tip for fast inclusion on Base.
    max_priority_fee = w3.to_wei(0.1, "gwei")
    max_fee = base_fee * 2 + max_priority_fee

    tx = multicall_contract.functions.aggregate3(calls).build_transaction(
        {
            "from": sender,
            "nonce": nonce,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": max_priority_fee,
            "chainId": CHAIN_ID,
        }
    )

    # Estimate gas with a 20 % safety buffer.
    estimated_gas = await asyncio.to_thread(w3.eth.estimate_gas, tx)
    tx["gas"] = int(estimated_gas * 1.2)

    logger.info(
        "⛽ Gas estimate: %s  |  maxFee: %s gwei  |  nonce: %s",
        estimated_gas,
        round(max_fee / 1e9, 4),
        nonce,
    )

    # ── Step 3: Sign & broadcast ────────────────────────────────
    signed = account.sign_transaction(tx)
    tx_hash = await asyncio.to_thread(w3.eth.send_raw_transaction, signed.raw_transaction)
    tx_hash_hex: str = tx_hash.hex()
    logger.info("📡 Broadcast tx: %s", tx_hash_hex)

    # ── Step 4: Wait for receipt ────────────────────────────────
    receipt = await asyncio.to_thread(w3.eth.wait_for_transaction_receipt, tx_hash, timeout=120)

    if receipt["status"] != 1:
        logger.error("❌ Tx reverted! hash=%s  gasUsed=%s", tx_hash_hex, receipt["gasUsed"])
        raise RuntimeError(f"Transaction {tx_hash_hex} reverted on-chain")

    # ── Step 5: Calculate gas savings ───────────────────────────
    gas_used = receipt["gasUsed"]
    savings = calculate_gas_savings(calls, gas_used)

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    logger.info(
        "✅ Tx confirmed in block %s  |  gasUsed: %s  |  saved: %s gas (%.1f%%)  |  hash: %s",
        receipt["blockNumber"],
        gas_used,
        savings.gas_saved,
        savings.savings_pct,
        tx_hash_hex,
    )

    return BundleResult(
        tx_hash=tx_hash_hex,
        explorer_url=f"https://basescan.org/tx/{tx_hash_hex}",
        block_number=receipt["blockNumber"],
        gas_used=gas_used,
        gas_savings=savings,
        actions_count=len(actions),
        execution_time_ms=elapsed_ms,
    )


# ──────────────────────────────────────────────
# Quick sanity test (run this file directly)
# ──────────────────────────────────────────────

if __name__ == "__main__":
    # Example: read-only call to WETH.name() on Base (costs no gas, just a demo).
    WETH_BASE = "0x4200000000000000000000000000000000000006"
    ERC20_ABI = [
        {
            "constant": True,
            "inputs": [],
            "name": "name",
            "outputs": [{"name": "", "type": "string"}],
            "type": "function",
        }
    ]

    demo_actions = [
        {
            "target": WETH_BASE,
            "abi": ERC20_ABI,
            "function": "name",
            "args": [],
            "allowFailure": True,
        }
    ]

    async def _main():
        try:
            # Test simulation first.
            sim = await simulate_bundle(demo_actions)
            print(f"Simulation: success={sim.success}, gas={sim.estimated_gas}")
            print(f"Call results: {sim.call_results}")

            # Full execution (will fail without a funded key — that's expected).
            result = await execute_bundle(demo_actions)
            print(f"TX Hash: {result.tx_hash}")
            print(f"Gas savings: {result.gas_savings.to_dict()}")
        except Exception as e:
            print(f"Demo error (expected if no funded key): {e}")

    asyncio.run(_main())
