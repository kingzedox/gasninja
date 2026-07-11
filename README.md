**# ⚡ GasNinja — Multicall3 Bundler Agent

> **Gas-optimized multi-transaction bundler for the CROO Agent Protocol.**
> Built for the [DoraHacks CROO Agent Hackathon](https://dorahacks.io) — DeFi / On-chain Ops track.

GasNinja is an AI-powered microservice that receives multiple DeFi intents (approve, swap, deposit, etc.), validates them, bundles them into a **single atomic Multicall3 transaction** on Base mainnet, and delivers the result — with proof of gas savings — back through the CROO Agent Protocol.

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        CROO Agent Network (CAP)                        │
│                                                                        │
│  Requester Agent ──────► NEGOTIATION_CREATED ──────► GasNinja Agent    │
│                                                        │               │
│                                                   ┌────┴────┐         │
│                                                   │  Groq   │         │
│                                                   │  LLM    │         │
│                                                   │ (parse  │         │
│                                                   │ intent) │         │
│                                                   └────┬────┘         │
│                                                        │               │
│                                                   ┌────┴────┐         │
│                                                   │Validate │         │
│                                                   │  Layer  │         │
│                                                   │ (ABI +  │         │
│                                                   │  types) │         │
│                                                   └────┬────┘         │
│                                                        │               │
│                      ORDER_PAID ◄─── accept ◄──────────┘               │
│                         │                                              │
│                    ┌────┴─────────────────────────┐                    │
│                    │   Multicall3 Engine           │                    │
│                    │                               │                    │
│                    │  1. eth_call simulation       │                    │
│                    │  2. EIP-1559 gas pricing      │                    │
│                    │  3. Sign + broadcast          │                    │
│                    │  4. Wait for receipt           │                    │
│                    │  5. Calculate gas savings      │                    │
│                    └────┬─────────────────────────┘                    │
│                         │                                              │
│  Requester Agent ◄──── deliver_order(tx_hash, gas_savings) ◄──────────│
└─────────────────────────────────────────────────────────────────────────┘
                          │
                    ┌─────┴─────┐
                    │   Base    │
                    │  Mainnet  │
                    │ Chain 8453│
                    └───────────┘

```

---

## ✨ Key Features

| Feature | Description |
| --- | --- |
| **Multicall3 Bundling** | N calls → 1 transaction via `aggregate3()`. Atomic execution, single gas payment. |
| **Gas Savings Proof** | Calculates and reports exactly how much gas was saved vs. N individual transactions. |
| **Dry-Run Simulation** | `eth_call` preflight catches reverts *before* spending real gas. |
| **AI Intent Parsing** | Groq LLM transforms messy natural-language DeFi intents into strict ABI-encoded call descriptors. |
| **Validation Layer** | Catches bad addresses, ABI mismatches, type errors, and Groq hallucinations before they hit the chain. |
| **CROO Protocol Native** | Full CAP lifecycle: negotiate → accept → execute → deliver with USDC settlement. |
| **EIP-1559 Gas Pricing** | Dynamic base fee + priority tip for fast inclusion on Base. |
| **Error Recovery** | Failed bundles still deliver error reports so requesters aren't left hanging. |

---

## 📁 Project Structure

```
gasninja/
├── cap_provider.py          # CAP WebSocket listener + Groq integration
├── multicall_engine.py      # Web3 Multicall3 bundler + simulation + gas calc
├── validate.py              # ABI & action validation layer
├── requirements.txt         # Python dependencies
├── .env.example             # Environment variable template
├── README.md
└── tests/
    └── test_validate.py     # Unit tests for validation + gas savings

```

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/YOUR_USERNAME/gasninja.git
cd gasninja
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

```

### 2. Configure

```bash
cp .env.example .env

```

Fill in your `.env`:

| Variable | Description |
| --- | --- |
| `PRIVATE_KEY` | Ethereum private key (with ETH on Base for gas) |
| `BASE_RPC_URL` | Base mainnet RPC (default: `[https://mainnet.base.org](https://mainnet.base.org)`) |
| `CAP_BASE_URL` | CROO API endpoint |
| `CAP_WS_URL` | CROO WebSocket endpoint |
| `CAP_SDK_KEY` | Your CROO SDK key |
| `GROQ_API_KEY` | Groq API key for LLM intent parsing |

### 3. Run the Agent

```bash
python cap_provider.py

```

### 4. Run Tests

```bash
python -m pytest tests/ -v

```

---

## 🧪 How It Works

### 1. Negotiation (Intent Received)

A requester agent sends a messy JSON payload like:

```json
{
  "intent": "approve USDC for Uniswap router then swap 100 USDC for WETH"
}

```

### 2. AI Parsing (Groq)

GasNinja sends this to Groq's `llama-3.3-70b-versatile` model, which returns:

```json
[
  {
    "target": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "abi": [{"name": "approve", "type": "function", ...}],
    "function": "approve",
    "args": ["0x2626664c2603336E57B271c5C0b26F421741e481", "100000000"],
    "allowFailure": false
  },
  {
    "target": "0x2626664c2603336E57B271c5C0b26F421741e481",
    "abi": [{"name": "exactInputSingle", "type": "function", ...}],
    "function": "exactInputSingle",
    "args": [...],
    "allowFailure": false
  }
]

```

### 3. Validation

The validation layer checks:

* ✅ Valid checksummed Ethereum addresses
* ✅ ABI contains the referenced function
* ✅ Argument count matches ABI inputs
* ✅ Argument types are compatible (address, uint256, bool, bytes)

### 4. Simulation

An `eth_call` dry-run executes the full bundle without broadcasting. If any call reverts, the agent aborts — **zero gas wasted**.

### 5. Execution

The bundle is encoded into a single `Multicall3.aggregate3()` call, signed with EIP-1559 gas pricing, and broadcast to Base mainnet.

### 6. Delivery

The agent delivers back via CAP:

```json
{
  "tx_hash": "0xabc...123",
  "explorer_url": "https://basescan.org/tx/0xabc...123",
  "gas_used": 85000,
  "gas_savings": {
    "individual_gas_total": 121000,
    "bundled_gas_used": 85000,
    "gas_saved": 36000,
    "savings_pct": 29.75,
    "num_calls": 2
  },
  "execution_time_ms": 3200
}

```

---

## ⛽ Gas Savings Model

For each individual transaction the EVM charges a **21,000 gas base fee** plus calldata costs. By bundling N calls into one Multicall3 transaction, you pay that 21,000 overhead only **once**.

```
Individual cost = N × 21,000 + Σ(calldata_gas_i)
Bundled cost    = 1 × 21,000 + Σ(calldata_gas_i) + multicall_overhead

Savings         = (N - 1) × 21,000 − multicall_overhead

```

For a typical 5-call DeFi bundle, this saves **~30-40% gas**.

---

## 🛡️ Safety Features

* **Pre-flight simulation** prevents gas waste on reverts
* **Validation layer** catches Groq hallucinations (bad addresses, wrong arg types)
* **Best-effort error delivery** ensures requesters always get a response
* **20% gas buffer** on estimates prevents out-of-gas reverts
* **Private key never logged** — loaded from `.env` only

---

## 🧰 Tech Stack

| Component | Technology |
| --- | --- |
| Agent Protocol | CROO SDK (CAP) |
| Blockchain | Base Mainnet (Chain 8453) |
| Smart Contract | Multicall3 (`0xcA11bde...CA11`) |
| AI / LLM | Groq (`llama-3.3-70b-versatile`) |
| Web3 | web3.py 6.x |
| Language | Python 3.11+ / asyncio |

---

## 📜 License

MIT

---

Built with ⚡ for the [CROO Agent Hackathon](https://dorahacks.io) by the GasNinja team.**
