# ⚡ GasNinja

**Gas-Optimized Multi-Tx Bundler Agent for the CROO Agent Protocol**

GasNinja is an AI-powered DeFi agent that acts as a microservice on the CROO protocol. It takes messy, natural-language intents (like "approve USDC and swap 100 for WETH"), parses them using Groq LLM, and executes them atomically via a **Multicall3** smart contract on the Base network to save gas and reduce transaction overhead.

## Features

- 🧠 **AI Intent Parsing**: Uses `llama-3.3-70b-versatile` via Groq to convert complex DeFi intents into strict Web3 transactions.
- 🛡️ **Action Validation**: Pre-flight checks ensure all addresses are checksummed, ABIs are valid, and arguments match before spending any gas.
- 🧪 **Dry-Run Simulation**: Uses `eth_call` to verify the transaction will succeed on-chain before execution.
- 💰 **Gas Optimization**: Bundles multiple transactions into a single Multicall3 execution, minimizing Base network base-fee overhead.
- 📊 **Real-time Metrics**: Includes a FastAPI server that tracks live bundles and gas savings for your frontend dashboard.
- 🔌 **CROO Native**: Built using the `croo-sdk`. Negotiates, executes, and delivers results seamlessly.

## Setup

1. Clone the repository and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create a `.env` file based on `.env.example`:
   ```bash
   cp .env.example .env
   ```
   Add your `BASE_RPC_URL`, `PRIVATE_KEY` (no 0x needed), `GROQ_API_KEY`, and `CAP_SDK_KEY`.

## Usage

### Run the Agent
To start the CAP provider and listen for live orders on the CROO network:
```bash
python3 cap_provider.py
```

### Run the Dashboard API
To run the background agent AND the metrics API for the frontend (this is what you deploy to Render):
```bash
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

### Run Tests
```bash
python3 -m pytest tests/ -v
```
