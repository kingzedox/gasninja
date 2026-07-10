"""
cap_provider.py – CROO Agent Protocol provider for GasNinja.

Listens for negotiations and paid orders over the CAP WebSocket,
uses Groq to parse messy intent payloads into strict web3 call
descriptors, validates them, bundles them via multicall_engine, and
delivers the tx hash + gas savings back through the CROO SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import traceback
from typing import Any

from dotenv import load_dotenv
from groq import Groq
from croo import AgentClient, Config, EventType, DeliverOrderRequest

from multicall_engine import execute_bundle, simulate_bundle, BundleResult
from validate import validate_actions

load_dotenv()

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logger = logging.getLogger("cap_provider")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)

# ──────────────────────────────────────────────
# Environment variables
# ──────────────────────────────────────────────
CAP_BASE_URL: str = os.getenv("CAP_BASE_URL", "")
CAP_WS_URL: str = os.getenv("CAP_WS_URL", "")
CAP_RPC_URL: str = os.getenv("CAP_RPC_URL", "")
CAP_SDK_KEY: str = os.getenv("CAP_SDK_KEY", "")
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")

# ──────────────────────────────────────────────
# Clients
# ──────────────────────────────────────────────
groq_client = Groq(api_key=GROQ_API_KEY)

cap_config = Config(
    base_url=CAP_BASE_URL,
    ws_url=CAP_WS_URL,
    rpc_url=CAP_RPC_URL,
)
cap_client = AgentClient(cap_config, sdk_key=CAP_SDK_KEY)

# ──────────────────────────────────────────────
# In-memory order context (negotiation_id → parsed actions)
# ──────────────────────────────────────────────
_order_payloads: dict[str, list[dict[str, Any]]] = {}

# ──────────────────────────────────────────────
# Groq LLM helper – parse messy intent → strict actions
# ──────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a DeFi intent parser.  The user will give you a raw JSON payload
describing one or more intended smart-contract actions on the Base network
(chain ID 8453).  Your job is to return a **valid JSON object** with a
single key "actions" whose value is an array of action objects.

Each action object must have exactly these keys:

  - "target"       (string): Checksummed Ethereum address of the contract.
  - "abi"          (array):  Minimal JSON ABI containing only the function
                              that will be called.  Each ABI entry must have
                              "name", "type": "function", "inputs" (array of
                              {name, type}), and "outputs".
  - "function"     (string): Solidity function name (e.g. "approve").
  - "args"         (array):  Ordered list of arguments matching the ABI inputs.
  - "allowFailure" (bool):   true if this sub-call may revert without
                              reverting the whole bundle; default false.

Rules:
  • Output ONLY the JSON object — no markdown fences, no explanations.
  • For token amounts, keep them as raw integer strings (wei / smallest unit).
  • If the intent is ambiguous, make reasonable DeFi assumptions
    (e.g. max-uint256 for unlimited approvals).
  • Validate that each ABI snippet matches the function name and args.
  • Every address must be checksummed.
"""


async def parse_intent_with_groq(raw_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Send the raw negotiation requirements to Groq and get back a
    strictly-formatted list of call descriptors.
    """
    logger.info("🧠 Sending intent to Groq for parsing …")

    user_message = json.dumps(raw_payload, indent=2)

    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )

        content: str = response.choices[0].message.content or "{}"
        parsed = json.loads(content)

        # Groq JSON-mode returns an object — extract the actions array.
        if isinstance(parsed, dict):
            for key in ("actions", "calls", "data", "results"):
                if key in parsed and isinstance(parsed[key], list):
                    parsed = parsed[key]
                    break
            else:
                raise ValueError(f"Groq returned an object with keys {list(parsed.keys())} — expected 'actions' array.")

        if not isinstance(parsed, list) or len(parsed) == 0:
            raise ValueError("Groq returned an empty or non-list response")

        logger.info("✅ Groq parsed %d action(s) successfully", len(parsed))
        return parsed

    except json.JSONDecodeError as exc:
        logger.error("❌ Groq response is not valid JSON: %s", exc)
        raise
    except Exception as exc:
        logger.error("❌ Groq parsing failed: %s", exc)
        raise


# ──────────────────────────────────────────────
# CAP event handlers
# ──────────────────────────────────────────────

async def on_negotiation_created(negotiation_id: str) -> None:
    """
    Triggered when a requester opens a negotiation.
    1.  Fetch the full negotiation to read the requirements payload.
    2.  Parse via Groq into strict call descriptors.
    3.  Validate the parsed actions (catch hallucinations).
    4.  Accept the negotiation (creates an order).
    """
    logger.info("📨 NEGOTIATION_CREATED → %s", negotiation_id)

    try:
        # 1 — Read negotiation details.
        negotiation = await asyncio.to_thread(cap_client.get_negotiation, negotiation_id)
        requirements = negotiation.requirements if hasattr(negotiation, "requirements") else {}

        if not requirements:
            logger.warning("⚠️  Negotiation %s has empty requirements — rejecting.", negotiation_id)
            return

        logger.info("📋 Requirements: %s", json.dumps(requirements, default=str)[:500])

        # 2 — Parse with Groq.
        actions = await parse_intent_with_groq(requirements)

        # 3 — Validate the parsed actions.
        validation = validate_actions(actions)
        if not validation.valid:
            logger.error(
                "❌ Groq output failed validation for negotiation %s:\n%s",
                negotiation_id,
                validation.summary(),
            )
            return  # Don't accept a job we can't fulfil.

        if validation.warnings:
            logger.warning(
                "⚠️  Validation warnings for negotiation %s:\n%s",
                negotiation_id,
                validation.summary(),
            )

        # 4 — Accept the negotiation.
        order = await asyncio.to_thread(cap_client.accept_negotiation, negotiation_id)
        order_id: str = order.id if hasattr(order, "id") else str(order)

        # Stash the parsed payload for when the order is paid.
        _order_payloads[order_id] = actions
        logger.info("✅ Negotiation accepted → order %s  (%d actions staged)", order_id, len(actions))

    except Exception:
        logger.error("❌ Error handling negotiation %s:\n%s", negotiation_id, traceback.format_exc())


async def on_order_paid(order_id: str) -> None:
    """
    Triggered once escrow is funded.
    1.  Retrieve the staged actions.
    2.  Simulate the bundle (dry-run) to catch reverts before spending gas.
    3.  Execute the Multicall3 bundle on Base.
    4.  Deliver the tx hash + gas savings back via CAP.
    """
    logger.info("💰 ORDER_PAID → %s", order_id)

    try:
        # 1 — Retrieve staged actions.
        actions = _order_payloads.pop(order_id, None)
        if actions is None:
            logger.error("❌ No staged actions for order %s — cannot fulfil.", order_id)
            return

        # 2 — Execute the bundle (includes simulation by default).
        logger.info("🚀 Executing Multicall3 bundle with %d call(s) …", len(actions))
        result: BundleResult = await execute_bundle(actions)
        logger.info("✅ Bundle executed → tx %s", result.tx_hash)

        # 3 — Deliver result back via CAP (with gas savings metrics).
        delivery_payload = {
            "tx_hash": result.tx_hash,
            "chain_id": result.chain_id,
            "network": result.network,
            "explorer_url": result.explorer_url,
            "block_number": result.block_number,
            "actions_count": result.actions_count,
            "status": result.status,
            "gas_used": result.gas_used,
            "gas_savings": result.gas_savings.to_dict(),
            "execution_time_ms": result.execution_time_ms,
        }

        await asyncio.to_thread(
            cap_client.deliver_order,
            order_id,
            DeliverOrderRequest(type="SCHEMA", data=delivery_payload),
        )

        # 4 — Update the dashboard API so live trades show up!
        try:
            from api_server import bundle_history, BundleRecord
            from datetime import datetime, timezone
            
            # Map action types to a short label
            types = [a.get("type", "call") for a in actions]
            label = " + ".join(t.capitalize() for t in types[:3])
            if len(types) > 3:
                label += f" (+{len(types)-3} more)"

            # Estimate USD cost (assuming $3200 ETH, 0.005 gwei base fee)
            gas_price_eth = 0.005 * 1e-9
            eth_price = 3200.0
            
            cost_usd = round(result.gas_used * gas_price_eth * eth_price, 4)
            individual_gas = result.gas_used + result.gas_savings.gas_saved
            standard_cost = round(individual_gas * gas_price_eth * eth_price, 4)

            record = BundleRecord(
                tx_hash=result.tx_hash,
                actions_count=result.actions_count,
                gas_used=result.gas_used,
                gas_saved=result.gas_savings.gas_saved,
                savings_pct=result.gas_savings.savings_pct,
                cost_usd=cost_usd,
                standard_cost_usd=standard_cost,
                timestamp=datetime.now(timezone.utc).isoformat(),
                action_labels=label
            )
            # Insert at the top of the history list
            bundle_history.insert(0, record)
            # Keep history bounded
            if len(bundle_history) > 200:
                bundle_history.pop()
            logger.info("✅ Live bundle added to dashboard API")
        except Exception as e:
            logger.error("Failed to add bundle to dashboard: %s", e)


        logger.info(
            "📦 Order %s delivered  |  saved %s gas (%.1f%%)",
            order_id,
            result.gas_savings.gas_saved,
            result.gas_savings.savings_pct,
        )

    except Exception:
        logger.error("❌ Error fulfilling order %s:\n%s", order_id, traceback.format_exc())

        # Best-effort: deliver an error report so the requester isn't left hanging.
        try:
            error_payload = {
                "status": "error",
                "order_id": order_id,
                "error": traceback.format_exc()[-500:],
            }
            await asyncio.to_thread(
                cap_client.deliver_order,
                order_id,
                DeliverOrderRequest(type="SCHEMA", data=error_payload),
            )
        except Exception:
            logger.error("❌ Could not deliver error payload either:\n%s", traceback.format_exc())


# ──────────────────────────────────────────────
# Main event loop
# ──────────────────────────────────────────────

async def main() -> None:
    """
    Connect to the CAP WebSocket and register event handlers.
    Runs indefinitely until interrupted.
    """
    logger.info("═" * 60)
    logger.info("⚡  GasNinja — Multicall3 Bundler Agent")
    logger.info("═" * 60)
    logger.info("CAP base_url : %s", CAP_BASE_URL)
    logger.info("CAP ws_url   : %s", CAP_WS_URL)

    # Validate config.
    missing = [
        name
        for name, val in [
            ("CAP_BASE_URL", CAP_BASE_URL),
            ("CAP_WS_URL", CAP_WS_URL),
            ("CAP_SDK_KEY", CAP_SDK_KEY),
            ("GROQ_API_KEY", GROQ_API_KEY),
            ("PRIVATE_KEY", os.getenv("PRIVATE_KEY", "")),
        ]
        if not val
    ]
    if missing:
        logger.error("❌ Missing env vars: %s — set them in .env", ", ".join(missing))
        return

    # Connect to the CAP WebSocket.
    stream = cap_client.connect_websocket()
    logger.info("🔌 WebSocket connected — listening for events …")

    # Register handlers.
    @stream.on(EventType.NEGOTIATION_CREATED)
    def _handle_negotiation(negotiation_id: str):
        asyncio.ensure_future(on_negotiation_created(negotiation_id))

    @stream.on(EventType.ORDER_PAID)
    def _handle_order_paid(order_id: str):
        asyncio.ensure_future(on_order_paid(order_id))

    # Keep the loop alive.
    logger.info("🟢 Agent is live and waiting for jobs …")
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("🛑 Event loop cancelled — shutting down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 GasNinja agent stopped by user.")
