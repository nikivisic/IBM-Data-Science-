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
| `HELIUS_API_KEY` | <https://dashboard.helius.dev> | **required** — trade history and wallet history |
| `BIRDEYE_API_KEY` | <https://bds.birdeye.so> | optional — only used when `ENABLE_BIRDEYE=true` |

Prices no longer need a key. The default chain is **Jupiter → DexScreener →
GeckoTerminal**, all keyless; see [Price sources](#price-sources).

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

Two commands worth running before you spend anything real:

```bash
whale-tracker price-check                     # can this machine reach the free price APIs?
whale-tracker ingest --tokens-file tokens.txt --dry-run   # what would this cost?
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

# 2. Price it before you buy it. No API calls are made.
whale-tracker ingest --tokens-file tokens.txt --dry-run

# 3. Pull everyone who traded them.
whale-tracker ingest --tokens-file tokens.txt

# 4. Pull the wider history of the candidates that showed up.
#    Repeatability cannot be judged from your seed tokens alone.
whale-tracker expand --dry-run --min-tokens 2        # check the cost first
whale-tracker expand --limit 100 --min-tokens 2

# 5. Rebuild P&L, run the detectors, score everyone.
whale-tracker analyse

# 6. Look at the table.
whale-tracker rank --limit 25 --min-tokens 4 --no-outliers --reasons

# 7. Inspect anything that interests you.
whale-tracker wallet <address> --trades

# 8. The go/no-go number.
whale-tracker backtest \
    --from-rank 10 --max-penalty 0.3 --no-outliers \
    --start 2025-06-01 --end 2025-09-01 \
    --latency 15-30 --slippage 1-3 \
    --size 250 --capital 5000
```

## API budget

Ingesting a token walks its entire swap history, so a busy mint is thousands of
requests. Nothing here is best-effort: caps are **hard**, checked before each
call leaves the process, and hitting one aborts the run naming the provider and
the limit. There is no mode that quietly continues with degraded data.

### See the cost before paying it

`--dry-run` works on `ingest` and `expand`. It makes **no** API calls:

```bash
whale-tracker ingest --token <mint> --token <mint> --dry-run
```

```
dry run — no API calls were made

WHAT IT WOULD DO
  tokens                         2
  transactions per token (cap)   5,000
  pages per token                50 × 100 per page
  provider                       helius
  price sources                  jupiter, dexscreener, geckoterminal

PROJECTED CONSUMPTION (upper bound)
  WHAT                      PROVIDER       CALLS  CREDITS EACH  CREDITS
  ------------------------  -------------  -----  ------------  -------
  parsed transaction pages  helius         100    100           10,000
  token metadata            dexscreener    2      0             0
  token metadata fallback   helius         2      10            20
  SOL price history         geckoterminal  23     0             0

  totals
    helius              102 requests       10,020 credits

BUDGET CHECK
  helius         credits/run    ok       10,020 of 100,000 after this run (10.0%)
  helius         credits/day    ok       10,020 of 250,000 after this run (4.0%)
  helius         credits/month  ok       10,020 of 1,000,000 after this run (1.0%)
```

How to read it:

* **Upper bound, always.** The projection assumes every token and every wallet
  runs into its cap. A token with a shorter history stops early, so the real
  figure is at most this. Bounding the other way round would defeat the point.
* **`credits/month`** is measured against `HELIUS_MONTHLY_CREDIT_BUDGET` and
  counts what you have already spent this month, from the database — so it is
  the number to check against the 1M free tier.
* **Exit code 1** means the run would breach a cap (`0` means it fits), so
  `whale-tracker ingest ... --dry-run || echo "too big"` works in a script.
  `--json` gives the same thing as structured data.

### What a call costs

Helius bills per method, so the estimate does too:

| Call | Credits | Set by |
|---|---|---|
| Ordinary RPC | 1 | `HELIUS_CREDITS_RPC` |
| `getSignaturesForAddress` | 1 | `HELIUS_CREDITS_SIGNATURES` |
| `getProgramAccounts` | 10 | `HELIUS_CREDITS_HEAVY_RPC` |
| DAS methods (`getAsset`, `searchAssets`, …) | 10 | `HELIUS_CREDITS_DAS` |
| Parsed Events API (beta) | 10 | `HELIUS_CREDITS_PARSED_EVENTS` |
| Bulk address history | 10 | `HELIUS_CREDITS_BULK_HISTORY` |
| Enhanced Transactions API | 100 | `HELIUS_CREDITS_ENHANCED_TX` |

**Confirm these against your own plan.** Nothing here can read Helius's price
list, so every rate is configuration; the history rate in particular dominates
every projection.

Token metadata is deliberately routed to the cheapest source that can answer:
DexScreener (free) first, Helius DAS (10 credits) only as a fallback.

### Which endpoint serves transaction history

History is the bulk of the spend, and the Enhanced Transactions API costs 100
credits a page — Helius now recommends migrating off it. The client therefore
picks an endpoint **cheapest first**, and only falls back when one is genuinely
unavailable:

| Order | Endpoint | Credits/page | Notes |
|---|---|---|---|
| 1 | Parsed Events API | 10 | Open beta; not on every account |
| 2 | Bulk address history (`getTransactionsForAddress`-style) | 10 | Paginated transaction fetch over the same signature range |
| 3 | Enhanced Transactions API | 100 | Last resort, so a plan with only this still works |

For a 5,000-transaction token that is **500 credits instead of 5,000**:

```bash
whale-tracker ingest --token <mint> --dry-run                            # 510 credits
whale-tracker ingest --token <mint> --history-strategy enhanced_tx --dry-run   # 5,010
```

The choice is a **runtime probe, not an assumption**. The first page either
comes back or says the endpoint is missing or not enabled (404, `-32601`
unknown method, a feature-gated 403), and the chain moves down, logging which
endpoint it settled on and why it skipped the others. Three things deliberately
do *not* trigger a fallback:

* **429 and 5xx** — those mean "try again", and the HTTP layer already retries
  them. Falling back would pay for the same page twice on a dearer endpoint.
* **A failure after the first successful page** — once a walk has started, the
  endpoint is locked in; re-walking elsewhere would re-pay for pages already
  fetched. That case raises instead.
* **A response in a shape the adapter cannot read** — treated as unavailable so
  the run moves on, rather than silently ingesting nothing.

Pin one endpoint with `--history-strategy` or `HELIUS_HISTORY_STRATEGY`. An
explicit choice still falls back if that endpoint is not enabled — aborting a
paid run because a beta endpoint went away helps nobody.

Endpoint paths and RPC method names are configuration
(`HELIUS_PARSED_EVENTS_PATH`, `HELIUS_BULK_HISTORY_METHOD`), so a rename or a
different rollout does not need a code change. Whatever answers is adapted into
one shape before it reaches the parser, so ingestion, scoring and the backtest
neither know nor care which endpoint was used.

A note on what is *not* cheaper: fetching a signature list and then calling
`getTransaction` once per signature costs ~1 credit per transaction, which is
the same ~100 credits per 100-transaction page as the Enhanced endpoint. The
saving comes from a *bulk* fetch, which is what the second strategy uses.

### Caps

| Setting | Default | Meaning |
|---|---|---|
| `BUDGET_ENFORCE` | `true` | Set `false` to meter without capping |
| `HELIUS_MAX_REQUESTS_PER_RUN` / `_DAY` | 5,000 / 50,000 | Request caps |
| `HELIUS_MAX_CREDITS_PER_RUN` / `_DAY` | 100,000 / 250,000 | Credit caps |
| `HELIUS_MONTHLY_CREDIT_BUDGET` | 1,000,000 | Reporting only — the free tier |
| `BIRDEYE_MAX_REQUESTS_PER_RUN` / `_DAY` | 2,000 / 20,000 | Request caps |
| `PRICE_MAX_REQUESTS_PER_RUN` / `_DAY` | 1,000 / 10,000 | Applied to **each** keyless source |

`0` means unlimited. Daily counters are UTC and live in the `api_usage` table,
so restarting the process does not hand out a fresh daily allowance. Per-run
overrides are available without editing `.env`:

```bash
whale-tracker ingest --tokens-file tokens.txt --max-credits 20000 --max-requests 500
```

When a cap is hit mid-run the command stops, prints which limit and how much
was used, exits **4**, and *keeps the pages it already pulled* — the next run
resumes from a larger base rather than starting over.

### Fan-out caps

`expand` multiplies: candidate wallets × pages per wallet. Both halves are
capped, low, by default.

| Setting | Default | Flag |
|---|---|---|
| `MAX_WALLETS_PER_TOKEN` | 50 | `--max-wallets-per-token` |
| `MAX_TXS_PER_WALLET` | 200 | `--max-txs-per-wallet` |

The wallet cap is applied **per seed token**, ranked by each wallet's USD
volume in that token, then merged. Without it a single busy mint with 40,000
traders would decide the whole expansion budget.

### Watching consumption

Running totals are logged per provider during a run, printed as a summary when
it finishes, and persisted. `whale-tracker status` shows month-to-date:

```
API usage — month to date (2026-09, UTC)
PROVIDER       REQUESTS  CREDITS  CACHE HITS  TODAY REQ  TODAY CR  MONTHLY CAP  USED
-------------  --------  -------  ----------  ---------  --------  -----------  ----
helius              120   12,000           4        120    12,000    1,000,000  1.2%
geckoterminal        23        0          61         23         0            -  -
```

Other ways to spend less:

* `--since 30d` (or `--since 2025-06-01`) limits how far back a walk goes;
* `--max-txs` caps a single ingest;
* every successful response is cached in SQLite for `HTTP_CACHE_TTL_SECONDS`
  (default 24 h), so re-runs and resumed runs are cheap — cache hits are
  counted, but cost nothing.

---

## Price sources

Prices come from a keyless fallback chain. Each request is logged with the
source that served it.

| Order | Source | History? | Free-tier limit | Notes |
|---|---|---|---|---|
| 1 | Jupiter | no | generous | Fast spot price |
| 2 | DexScreener | no | ~300/min | Spot from the deepest pool; also free token metadata |
| 3 | GeckoTerminal | **yes** | ~30/min | Hourly OHLCV — the only keyless historical source |
| 4 | Birdeye | yes | plan-dependent | Only when `ENABLE_BIRDEYE=true` |

Rules that make this safe to run on free tiers:

* **Spot-only sources are skipped for old timestamps.** Asking Jupiter for last
  month's price would silently return *today's*, which would corrupt the P&L.
  Only history-capable sources answer for anything older than a couple of hours.
* **Everything is cached in `price_points`, bucketed by hour.** One GeckoTerminal
  OHLCV call fills ~100 hourly buckets, so a whole ingest usually needs a
  handful of price calls in total.
* **A 429 puts that source in cooldown** (`PRICE_SOURCE_COOLDOWN_SECONDS`,
  default 300s) and the chain moves to the next one instead of retrying into
  the limit. Other failures — an unknown mint, say — do not disable a source.
* Only quote currencies are ever priced: SOL, and stablecoins at par.

Override per run:

```bash
whale-tracker ingest --tokens-file tokens.txt --price-sources dexscreener,geckoterminal
whale-tracker ingest --tokens-file tokens.txt --enable-birdeye
```

Check the chain works from your network before a real run:

```bash
whale-tracker price-check
```

```
price sources for So111...112   (history probe at 2026-09-08 00:00)
SOURCE         HISTORY?  SPOT USD     SPOT ms  POINTS  HIST ms  ERROR  HIST ERROR
-------------  --------  -----------  -------  ------  -------  -----  ----------
jupiter        no        142.310000       181  -       -
dexscreener    no        142.280000       143  -       -
geckoterminal  yes       142.300000       402  100     618
```

If no source returns historical points, old trades fall back to the nearest
cached price or to `--sol-price`; the command says so.

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
| `status` | Database contents, trade window, keys, and month-to-date API usage |
| `ingest --token M \| --tokens-file F` | Pull every wallet that traded those mints |
| `ingest ... --dry-run` | Project calls and credits without making any |
| `expand [--limit N]` | Pull candidates' wider history across all their tokens |
| `expand --dry-run` | Same projection for the expansion fan-out |
| `price-check` | Probe each price source and report which ones answer |
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

Budget and price flags, available on `ingest`, `expand` and `price-check`:

| Flag | Effect |
|---|---|
| `--dry-run` | Project cost, call nothing (`ingest`, `expand` only) |
| `--max-requests N` | Per-run request cap for every provider, this run only |
| `--max-credits N` | Per-run Helius credit cap, this run only |
| `--history-strategy S` | Pin the history endpoint (`auto`, `parsed_events`, `bulk_history`, `enhanced_tx`) |
| `--price-sources a,b` | Override the keyless chain and its order |
| `--enable-birdeye` | Append Birdeye to the chain (needs a key) |
| `--max-wallets-per-token N` | Fan-out cap per seed token (`expand`) |
| `--max-txs-per-wallet N` | Transactions per wallet (`expand`) |

Exit codes: `0` success, `1` a dry run that would breach a cap, `2` config
error, `3` API failure, `4` budget cap hit mid-run.

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
| `api_usage` | Requests, credits and cache hits per provider per UTC day |
| `token_pools` | Liquidity pool discovered per mint, so lookups are not repeated |
| `http_cache` | Cached provider responses, keyed by a hash |
| `ingest_runs` | Ingestion bookkeeping, including failures |

Query it directly whenever the CLI does not cut it:

```sql
SELECT wallet, score, win_rate, median_roi, realised_pnl_usd
FROM v_ranked_wallets
WHERE tokens_traded >= 5 AND penalty = 0 AND single_outlier = 0
ORDER BY score DESC LIMIT 20;

-- What has this month cost so far?
SELECT provider, SUM(requests), SUM(credits), SUM(cache_hits)
FROM api_usage WHERE day LIKE '2026-09-%' GROUP BY provider;
```

---

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

197 tests, no network access required. They cover the P&L arithmetic
(average-cost basis, partial exits, airdrops, dust), the scoring transforms and
their edge cases, all three detectors, the backtest fill model, provider
response parsing (including aggregator routes and balance-change fallbacks),
HTTP retry/cache behaviour, and an end-to-end run over the synthetic universe
that asserts a sniper never outranks a steady performer.

The budget and price layers are covered specifically:

* credit costs per method, per-run and per-day caps, daily totals surviving a
  restart, and a real `ingest` that stops at its cap — proving the refused call
  never reached the network and the pages already pulled were kept;
* price fallback order, per-source 429 cooldown and its expiry, spot-only
  sources being skipped for old timestamps, cache hits served from
  `price_points`, and a parametrised check that a 429 from *each* real source
  is handled the same way;
* history endpoint selection: the same swap in all three payload shapes
  producing identical legs through the **unchanged** parser, fallback on 404 /
  unknown method / unreadable shape, no fallback on 429 or 5xx, cursor
  pagination past skipped records, and each endpoint billed at its own rate.

---

## Project layout

```
whale_tracker/
├── cli.py              # argparse CLI, table/JSON/CSV rendering
├── config.py           # .env loading, known mints, no hardcoded secrets
├── logging_setup.py    # structured JSON / console logging
├── db.py               # SQLite schema, upserts, cache, ranked view
├── models.py           # Trade, SwapLeg, PositionPnL, WalletScore, WalletFlags
├── budget.py           # request/credit metering, hard caps, daily persistence
├── estimate.py         # --dry-run projections and budget checks
├── clients/
│   ├── base.py         # rate limiting, retries, response cache, budget metering
│   ├── helius.py       # Helius client + swap-leg extraction (the parser)
│   ├── helius_history.py  # cheapest-first history endpoints and their adapters
│   ├── prices.py       # Jupiter / DexScreener / GeckoTerminal fallback chain
│   └── birdeye.py      # trade tape, token metadata, historical prices (optional)
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
* **Credit costs are a configured assumption, not a live lookup.** The tool
  cannot read your Helius plan, so `--dry-run` is only as accurate as
  `HELIUS_CREDITS_*`. Verify the Enhanced Transactions rate against your
  dashboard once; it dominates every projection.
* **The Parsed Events shape is inferred, not verified.** That endpoint could
  not be reached from the machine this was built on, so its adapter is written
  to be shape-tolerant and treats anything it cannot read as "endpoint
  unavailable" — the run falls back rather than quietly ingesting nothing. The
  first live run will say in the log which endpoint it settled on; if it skips
  Parsed Events with "could not adapt", send me the payload and the adapter is
  a small fix. The bulk and raw-RPC adapters are built on the documented,
  stable JSON-RPC transaction shape and are tested against it.
* **Free price endpoints change without notice.** The parsers tolerate the
  response shapes each API is documented to return, and fall through to the
  next source when one does not answer. `whale-tracker price-check` is the
  quickest way to confirm the chain still works from your network.

Nothing here is financial advice, and nothing here places a trade.
