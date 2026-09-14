# whale-tracker

Wallet discovery and scoring for Solana memecoin traders. **Phase 1 only:** it
finds wallets, reconstructs what they actually made, scores them on
*repeatability*, flags the ones that only look good, and then tells you what
copying them would have been worth after realistic latency and slippage.

It is read-only. There is no trading, no wallet key, no signing, and no code
path that could place an order.

---

## What it does

1. **Discovery** — given a set of memecoin mints, pulls every swap that touched
   them (Helius parsed transaction history, optionally Birdeye's trade tape)
   and extracts every wallet that traded them.
2. **Reconstruction** — rebuilds each wallet's realised P&L per token:
   buys, sells, weighted-average entry and exit, cost-weighted hold time,
   what is still open.
3. **Scoring** — ranks wallets on whether they do it *again*: win rate across
   N distinct tokens, median and mean ROI, profit factor, max drawdown, trade
   count, recency — and flags wallets whose profit is one outlier.
4. **Pattern detection** — down-ranks launch-block snipers, statistically
   abnormal win rates, and clusters of wallets that trade in lockstep.
5. **Backtest** — simulates copying a wallet set over a date range with
   15–30 s reaction latency and 1–3 % slippage, filling against the real tape.
   **This is the go/no-go number.**

Everything lands in one SQLite file. Logs are structured JSON on stderr; data
goes to stdout, so `--json | jq` works.

---

## Setup

Requires Python 3.10+.

```bash
git clone <this repo>
cd whale-tracker

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -e .                   # installs the `whale-tracker` command
# or, without installing:  pip install -r requirements.txt
#                          python -m whale_tracker <command>
```

### API keys

```bash
cp .env.example .env
```

Then edit `.env` and fill in:

| Variable | Where to get it | Needed for |
|---|---|---|
| `HELIUS_API_KEY` | <https://dashboard.helius.dev> | ingestion, wallet history (the core) |
| `BIRDEYE_API_KEY` | <https://bds.birdeye.so> | historical USD pricing, token metadata, optional trade tape |

`.env` is in `.gitignore`. Keys are read from the environment only — nothing is
hardcoded, and `tests/test_config.py` fails the build if a key is ever pasted
into the source. The HTTP cache hashes request URLs before storing them, so
keys never reach the database either.

Everything else in `.env.example` (rate limits, cache TTL, ingestion caps,
log level, database path) has a working default; you only need the two keys.

### Verify the install without spending API credits

```bash
whale-tracker demo-seed          # synthetic universe: 10 tokens, ~200k trades
whale-tracker analyse            # P&L -> patterns -> scores
whale-tracker rank --reasons     # the ranked table, with flags explained
whale-tracker backtest --from-rank 5 --max-penalty 0.3
```

The synthetic universe deliberately contains a steady performer, a one-hit
wonder, a launch-block sniper, a four-wallet sybil set and a consistent loser,
so you can see what each detector does before pointing it at mainnet.

---

## Real run

```bash
whale-tracker init-db

# 1. Seed tokens: mints whose traders you want to study. One per line.
cat > tokens.txt <<'EOF'
7GCihgDB8fe6KNjn2MYtkzZcRjQy3t9GHdC8uHYmW2hr
EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm
EOF

# 2. Pull everyone who traded them.
whale-tracker ingest --tokens-file tokens.txt

# 3. Pull the wider history of the candidates that showed up.
#    Repeatability cannot be judged from your seed tokens alone.
whale-tracker expand --limit 100 --min-tokens 2

# 4. Rebuild P&L, run the detectors, score everyone.
whale-tracker analyse

# 5. Look at the table.
whale-tracker rank --limit 25 --min-tokens 4 --no-outliers --reasons

# 6. Inspect anything that interests you.
whale-tracker wallet <address> --trades

# 7. The go/no-go number.
whale-tracker backtest \
    --from-rank 10 --max-penalty 0.3 --no-outliers \
    --start 2025-06-01 --end 2025-09-01 \
    --latency 15-30 --slippage 1-3 \
    --size 250 --capital 5000
```

### On API budget

Ingesting a token walks its entire swap history, so a busy mint is thousands of
requests. Guardrails:

* `MAX_TXS_PER_TOKEN` (`.env`) and `--max-txs` cap the walk;
* `--since 30d` (or `--since 2025-06-01`) limits how far back it goes;
* every successful response is cached in SQLite for `HTTP_CACHE_TTL_SECONDS`
  (default 24 h), so re-runs and interrupted runs are cheap;
* `HELIUS_RATE_LIMIT_RPS` / `BIRDEYE_RATE_LIMIT_RPS` pace requests to your plan
  — the free Birdeye tier is roughly 1 rps, which is the default here.

Start with two or three tokens and `--max-txs 2000` to calibrate.

---

## How the score works

Every sub-score is bounded to `[0, 1]` by a **fixed** transform — not a
cross-sectional z-score — so a wallet's score means the same thing between runs
and is not re-based by whoever else happens to be in the table.

| Component | Weight | Transform | Why |
|---|---|---|---|
| Win rate | 0.22 | Beta-shrunk `(wins + 2) / (n + 5)` | 3-for-3 must not outrank 34-for-50 |
| ROI | 0.22 | `tanh(median ROI)` | **median** — an outlier cannot move it |
| Profit factor | 0.16 | `pf / (pf + 2)`, pf capped at 10 | rewards asymmetry, saturates |
| Consistency | 0.16 | `½(1 − top-1 share) + ½(1 − HHI)` | profit spread across trades, not one |
| Drawdown | 0.10 | `1 − maxDD%` | survivability |
| Recency | 0.08 | `0.5 ^ (days / 30)` | last year's alpha is not alpha |
| Activity | 0.06 | `log1p(trades) / log1p(30)` | sample size |

Then:

```
raw_score = 100 × Σ(weight × component) × sample_confidence
score     = raw_score × (1 − penalty)
```

* `sample_confidence = min(1, closed_positions / min_tokens)` — three tokens is
  the difference between a track record and an anecdote.
* `penalty` combines the pattern flags below with a flat **0.35 haircut for
  one-hit wonders**: the profit was real, the repeatability was not.

A wallet is flagged `single_outlier` when its top trade is ≥60 % of gross
profit, or its P&L goes negative without its best trade, or its mean ROI is
positive while its median is not.

### Accounting rules that matter

* **Weighted-average cost basis.** Every buy raises the average entry; every
  sell realises against it. Unlike FIFO, it invents no ordering the chain
  cannot support, and it matches how a trader describes their own position.
* **Tokens sold that were never bought** — airdrops, dev allocations, transfers
  in, or history older than your ingest window — have no cost basis. Their
  proceeds are recorded as `windfall_pnl_usd` and **excluded from ranking**;
  positions that are more than half windfall are dropped from the scored set
  entirely. Otherwise a wallet that was gifted a supply scores as a genius.
* **Wins are measured on basis P&L** (`proceeds − cost`), never on windfall.
* **Dust tolerance.** A position counts as closed once under 1 % of the tokens
  bought remain; memecoin traders rarely sell the last 0.4 %.
* **Only SOL/USDC/USDT-quoted swaps are recorded.** A memecoin-for-memecoin
  rotation has no unambiguous USD basis, and guessing one would corrupt the P&L.

---

## Pattern detection

Three families of wallet look excellent on a P&L table and are useless — or
actively dangerous — to copy. Each is a continuous penalty, not a ban, and each
records a human-readable reason (`whale-tracker rank --reasons`).

| Detector | What it looks for | Default penalty |
|---|---|---|
| **Launch sniper** | First buy in the launch block (or within 30 s / 2 slots), token after token | up to 0.55 above a 30 % base rate |
| **Abnormal win rate** | Binomial tail probability of the wallet's record against the *population's own* win rate; a spotless record only counts when it is also improbable (p < 0.05) | up to 0.45 |
| **Lockstep cluster** | Wallets entering the same tokens within 30 s of each other, over ≥3 shared tokens, at ≥60 % co-entry — union-found into clusters | up to 0.55, scaled by cluster size |

Penalties sum and are capped at 0.90.

A note on snipers: buying in the launch block is not necessarily misconduct.
It is *uncopyable*. By the time a follower sees the buy, 15–30 seconds of
candles have printed — which is exactly what the backtest then proves.

Candidate pairs for the cluster search are built with a sliding window over
each token's entry times, so the expensive all-pairs comparison only ever runs
on wallets that were genuinely close together.

---

## The backtest

```bash
whale-tracker backtest --from-rank 10 --start 2025-06-01 --end 2025-09-01 \
    --latency 15-30 --slippage 1-3 --size 250 --capital 5000 --trades
```

```
  copied trades      : 41
  win rate           : 53.7%
  net P&L            : $1,284.55
  ROI on capital     : 25.7%
  ROI on deployed    : 12.5%
  fees paid          : $63.40
  max drawdown       : 18.2%
  leader P&L (same size, no latency/slippage): $2,410.88
  latency+slippage drag: $1,126.33

==> GO: $1,284.55 net on $5,000 capital (25.7%)
```

**How fills work.** When a leader buys at `t`, your order is assumed to land at
`t + latency` and to fill at the price of the **next trade actually observed in
that token** at or after that moment — from the tape you ingested, not at the
leader's price. Slippage is then applied on top, and again on the way out. When
the leader sells a fraction of their position, you sell the same fraction.

**Modelled:** reaction latency (uniform in range), slippage (uniform in range),
DEX + priority fees (`--fee-bps`, `--network-fee`), fixed position sizing, a
cash constraint, a concurrency cap (`--max-open`), optional forced exit
(`--max-hold`), and closing anything still open at the end of the window at the
last observed price — so the headline number is never propped up by an
imaginary bag.

**Not modelled, and it matters:** market impact beyond the configured slippage
(on a thin token with size, reality is worse), failed and reverted
transactions, MEV/sandwiching, priority-fee auctions during congestion, and the
possibility that the leader's edge was that *nobody was copying them*. Read the
result as an optimistic upper bound.

Runs are reproducible: latency and slippage come from a seeded RNG (`--seed`).
Every run is stored (`whale-tracker backtests`, `--run-id N` for the detail)
with its parameters and every simulated fill, so you can diff assumptions.

Compare like for like before believing a number:

```bash
whale-tracker backtest --from-rank 10 --latency 0 --slippage 0 --label ideal
whale-tracker backtest --from-rank 10 --latency 15-30 --slippage 1-3 --label realistic
whale-tracker backtest --from-rank 10 --latency 30-60 --slippage 3-6 --label pessimistic
whale-tracker backtests
```

---

## CLI reference

| Command | What it does |
|---|---|
| `init-db` | Create the SQLite schema |
| `status` | Database contents, trade window, whether keys are set |
| `ingest --token M \| --tokens-file F` | Pull every wallet that traded those mints |
| `expand [--limit N]` | Pull candidates' wider history across all their tokens |
| `analyse` | Rebuild P&L, run detectors, score wallets |
| `rank [--reasons] [--json\|--csv F]` | The ranked candidate table |
| `wallet ADDR [--trades]` | One wallet: scorecard, flags, positions, full trade history |
| `token MINT` | One token: launch, and its best wallets |
| `backtest ...` | The copy-trade simulation |
| `backtests [--run-id N]` | Stored runs |
| `demo-seed` | Fill the database with the synthetic universe |
| `export --table T --out F.csv` | Dump `ranked`/`scores`/`flags`/`positions`/`trades`/`tokens` |

Useful global flags: `--db PATH`, `--env-file PATH`, `--log-level DEBUG`,
`--log-format console`.

Useful `rank` filters: `--min-tokens`, `--min-closed`, `--max-penalty`,
`--min-pnl`, `--no-outliers`.

---

## Data model

One SQLite file (`WHALE_DB_PATH`, default `data/whale_tracker.db`):

| Table | Contents |
|---|---|
| `tokens` | Mint, symbol, launch slot/timestamp, trade and wallet counts |
| `trades` | Normalised swap legs: wallet, mint, side, tokens, USD value, price, slot, dex |
| `wallet_token_pnl` | Realised P&L per (wallet, mint): basis, proceeds, ROI, avg entry/exit, hold time, open remainder |
| `wallet_scores` | The scorecard, plus `components_json` showing every sub-score and weight |
| `wallet_flags` | Sniper rate, insider p-value, cluster id/size, penalty, reasons |
| `v_ranked_wallets` | The ranked candidate table (scores joined to flags) |
| `backtest_runs` / `backtest_trades` | Every run's parameters, metrics and simulated fills |
| `price_points` | Hour-bucketed USD prices (mostly SOL) for valuing quote legs |
| `http_cache` | Cached provider responses, keyed by a hash |
| `ingest_runs` | Ingestion bookkeeping, including failures |

Query it directly whenever the CLI does not cut it:

```sql
SELECT wallet, score, win_rate, median_roi, realised_pnl_usd
FROM v_ranked_wallets
WHERE tokens_traded >= 5 AND penalty = 0 AND single_outlier = 0
ORDER BY score DESC LIMIT 20;
```

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

97 tests, no network access required. They cover the P&L arithmetic
(average-cost basis, partial exits, airdrops, dust), the scoring transforms and
their edge cases, all three detectors, the backtest fill model, provider
response parsing (including aggregator routes and balance-change fallbacks),
HTTP retry/cache behaviour, and an end-to-end run over the synthetic universe
that asserts a sniper never outranks a steady performer.

---

## Project layout

```
whale_tracker/
├── cli.py              # argparse CLI, table/JSON/CSV rendering
├── config.py           # .env loading, known mints, no hardcoded secrets
├── logging_setup.py    # structured JSON / console logging
├── db.py               # SQLite schema, upserts, cache, ranked view
├── models.py           # Trade, SwapLeg, PositionPnL, WalletScore, WalletFlags
├── clients/
│   ├── base.py         # token-bucket rate limiting, retries, response cache
│   ├── helius.py       # parsed transaction history + swap-leg extraction
│   └── birdeye.py      # trade tape, token metadata, historical prices
├── ingest.py           # discovery, USD pricing of the quote leg, persistence
├── pnl.py              # weighted-average-cost realised P&L
├── scoring.py          # repeatability scorecard
├── patterns.py         # sniper / insider / sybil detection
├── backtest.py         # copy-trade simulation
└── demo.py             # synthetic universe for testing without API keys
```

---

## Limitations

Worth knowing before you trust the output:

* **Ingestion window bias.** Trades older than what you pulled look like
  airdrops (sells with no matching buy). They are quarantined as windfall
  rather than silently counted, but a wallet whose history you truncated will
  be under-scored. `--since` deliberately, and use `expand` generously.
* **Wallet ≠ trader.** One person may run many wallets (the cluster detector
  only catches the ones that trade in lockstep), and one wallet may be a
  custodial or bot account serving many people.
* **Attribution is fee-payer based.** For most direct swaps and aggregator
  routes this is the trader; for unusual programs it may not be.
* **Prices for the quote leg are hour-bucketed.** Fine for SOL, which barely
  moves relative to a memecoin inside an hour, but it is an approximation.
* **Past performance, small samples, survivorship.** You are looking at wallets
  that already exist in the tokens you chose. The detectors reduce the obvious
  traps; they do not make the sample unbiased.
* **The backtest is an upper bound** — see the "not modelled" list above.

Nothing here is financial advice, and nothing here places a trade.
