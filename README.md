# Punt OEMS

> **A first proper Solana Punt OEMS (Order Execution Management System), generalized beyond ZCAT so you can use the same execution layer for new StonkFun punts.**

---

## 🚀 Live Shareable Web App

Open the live scanner and adaptive execution simulator directly in your browser (no installation or download required):

### **[https://21e8-miner.github.io/punt-oems/](https://21e8-miner.github.io/punt-oems/)**

---

## 📦 Direct Standalone Downloads

- **Direct Bundle (.zip)**: [punt_oems_bundle.zip](https://github.com/21e8-miner/punt-oems/releases/latest/download/punt_oems_bundle.zip)
- **GitHub Release (v1.0.0)**: [Release v1.0.0](https://github.com/21e8-miner/punt-oems/releases/tag/v1.0.0)
- **Source Code Archive**: [main.zip](https://github.com/21e8-miner/punt-oems/archive/refs/heads/main.zip)

---

Local Solana scanner, execution planner, and paper/live child-order runner. Starts in paper mode by default with zero external dependencies, and never sends a live transaction unless explicitly armed with `--live`, credentials, and manual confirmation.

```
observe → size → quote → guard → execute child → reassess → repeat
```

It uses **Jupiter** as the execution router via its Swap V2 `/order` + `/execute` path: multiple routing engines compete for execution, while Jupiter handles RTSE slippage estimation, priority fees, transaction landing, and its Beam execution pipeline ([Jupiter Developer Platform](https://developers.jup.ag/docs/swap/order-and-execute)).

---

## What is in this build

### Preloaded Instruments
* **ZCAT**: `HcRLc9VDgjLeK154xDawfb1dmVJ98DoSqcwTHGqiDeJR`
  * Pre-configured expected Token-2022 transfer fee: `300 bps` (3.0%)
* **Buy The Cat (BTC)**: `E4Ap4icMLwKot8rkkTbq5JkS5kZxt5XCE3yfxbzYBjHx` — independently confirmed StonkFun Buy The Cat mint ([OpenSea](https://opensea.io/token/solana/E4Ap4icMLwKot8rkkTbq5JkS5kZxt5XCE3yfxbzYBjHx))

### Execution Modes
1. **`ADAPTIVE` (Default)**: Each child order is dynamically constrained by the tightest of:
   * The live Jupiter price-impact curve
   * Your configured maximum child size
   * Your permitted percentage of actual 5-minute DEX turnover
2. **`TWAP`**: Same execution safeguards, spread across your requested duration.
3. **`IMMEDIATE`**: Attempts the total requested notional immediately, but still aborts if the live impact guard is violated.

---

## The Important Difference From a Basic Trading Bot

Before **every individual child order**, Punt OEMS re-reads the market state and evaluates:
1. **Is liquidity sufficient?** (Checks against min liquidity floor)
2. **Are DexScreener and Gecko reasonably synchronized?** (Rejects cross-source divergence spikes)
3. **Has the Token-2022 transfer tax changed?** (Verifies against expected bps and state hash)
4. **Has price run away from our arrival price?** (Guards against adverse drift during execution)
5. **What does Jupiter say this exact child will actually receive?** (Live route & quote inspection)
6. **Is price impact inside our budget?** (Dynamic size halving until impact criteria is satisfied)

**If any answer is bad, the plan pauses immediately rather than chasing.**

Furthermore, it checks the final Jupiter V2 `/order` response against the quote observed immediately prior. If execution deteriorates by more than the permitted slippage threshold between quote and signing, **it refuses to sign**.

---

## Minimal-Footprint Dynamic Sizing

Suppose you want to exit \$20K of ZCAT. Rather than blindly submitting four \$5K swaps on a fixed timer, the OEMS builds a live depth curve:

| Requested Child | Jupiter Impact | Effective Price | Route |
|---|---|---|---|
| \$250 | live quote | live quote | Raydium / Meteora / Orca |
| \$500 | live quote | live quote | Raydium / Meteora / Orca |
| \$1,000 | live quote | live quote | Raydium / Meteora / Orca |
| \$2,500 | live quote | live quote | Raydium / Meteora / Orca |
| \$5,000 | live quote | live quote | Raydium / Meteora / Orca |
| \$10,000 | live quote | live quote | Raydium / Meteora / Orca |
| \$25,000 | live quote | live quote | Raydium / Meteora / Orca |
| \$50,000 | live quote | live quote | Raydium / Meteora / Orca |

It selects the largest child satisfying your impact limit, then further caps it by a fraction of actual 5-minute volume:
* If the market suddenly becomes shallow (\$5K → 38 bps, but \$10K → 106 bps), the OEMS stays around \$5K or below rather than mechanically following the original schedule.
* If 5-minute turnover collapses, child size automatically scales down.
* Cadence is lightly jittered (±18%) so you never submit a predictable on-chain footprint.

---

## Token-2022 Awareness

The OEMS directly reads the on-chain mint data from Solana RPC and tracks:
* `transferFeeBasisPoints`
* `maximumFee`
* `transferFeeConfigAuthority`
* `withdrawWithheldAuthority`
* `stateHash` (SHA-256 fingerprint of config and epoch-bracketed fees)

For ZCAT, the expected state is configured to `300 bps`. If the contract authority changes the fee or switches configuration, an active plan **immediately pauses** before submitting another child order.

---

## Execution Risk Controls

Live mode requires **three independent actions**:
1. Start the server with `--live`
2. Provide a local Solana keypair and `JUPITER_API_KEY`
3. Type `LIVE` in the confirmation box when starting an execution plan

Additional safety controls:
* **Strict Key Isolation**: The private key is read locally from your CLI JSON file and is **never** sent over the network or transmitted to Jupiter. The OEMS signs the Jupiter-produced versioned transaction locally and sends only the signed transaction payload.
* **$50,000 Default Live Notional Cap**: Configurable via `--max-live-plan-usd`.
* **Fail-Safe Restarts**: Plans survive server restarts as `PAUSED` and never automatically resume.
* **Global Kill Switch**: One-click "Kill all" cancels every open and working plan.

---

## Getting Started

### 1. Paper Mode (Zero Setup)
Runs out of the box with Python standard library. No private keys, wallet, or external packages required.

Double-click `run_paper.command`, or run:
```bash
python3 punt_oems.py
```
Open your browser to:
```
http://127.0.0.1:8770
```

### 2. Live Mode Setup
Install the optional signing dependency:
```bash
python3 -m pip install solders
```
Export your credentials and start:
```bash
export JUPITER_API_KEY='your_api_key_here'
export SOLANA_KEYPAIR="$HOME/.config/solana/id.json"

./run_live.command
```
*(Or invoke directly: `python3 punt_oems.py --live --keypair "$SOLANA_KEYPAIR"`)*

---

## File Structure

```
punt-oems/
├── punt_oems.py         # Complete OEMS engine (scanner, planner, runner, web UI)
├── run_paper.command    # macOS launcher for paper mode
├── run_live.command     # macOS launcher for live mode
├── requirements.txt     # Optional live dependencies (solders)
├── tests/
│   └── test_punt_oems.py # Automated test suite
├── oems_journal.jsonl   # Append-only execution journal (generated at runtime)
├── oems_state.json      # Persistent plan & fill state (generated at runtime)
├── LICENSE              # MIT License
└── README.md
```

All outbound HTTP calls set `User-Agent: OpenAI File Downloader, XaiImageApiFetch/1.0`.

---

## Roadmap

1. **Arrival-Price Implementation Shortfall**:
   Measure actual realized price against arrival price at OEMS start, broken down into:
   $$\text{Shortfall} = \text{Transfer Tax} + \text{AMM Fees} + \text{Price Impact} + \text{Market Drift}$$
   This quantifies exactly how much value the dynamic slicing algorithm is capturing vs. naive execution.

2. **Jupiter Trigger V2 Catastrophe Exits**:
   Integrate Jupiter Trigger V2 ([Jupiter Developer Platform](https://developers.jup.ag/docs/trigger/create-order)). Trigger V2 keeps pending orders off-chain and private until execution, supports partial fills, OCO take-profit/stop-loss, and USD-price triggers.

### Target Architecture
```
Punt Sentinel → OEMS Adaptive Execution → Jupiter Swap V2 → Private Trigger V2 Catastrophe Exit
```
Focused strictly on execution quality for \$2M–\$150M Solana tokens without institutional bloat.

---

## License

MIT
