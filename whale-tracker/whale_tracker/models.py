"""Core data structures shared across ingestion, scoring and backtesting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

BUY = "buy"
SELL = "sell"


@dataclass(slots=True)
class Trade:
    """One normalised swap leg: a wallet moving in or out of a single mint.

    A DEX swap is reduced to the memecoin side plus the value of its quote leg,
    so `price_usd` is always "USD per whole token" regardless of whether the
    trade was routed through SOL, USDC or USDT.
    """

    signature: str
    wallet: str
    mint: str
    side: str
    token_amount: float
    quote_mint: str
    quote_amount: float
    price_usd: float
    value_usd: float
    ts: int
    slot: int
    dex: str = ""
    source: str = ""

    def as_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TokenMeta:
    """What we know about a traded mint."""

    mint: str
    symbol: str = ""
    name: str = ""
    decimals: int = 0
    launch_ts: Optional[int] = None
    launch_slot: Optional[int] = None
    first_trade_ts: Optional[int] = None
    last_trade_ts: Optional[int] = None
    trade_count: int = 0
    wallet_count: int = 0


@dataclass(slots=True)
class PositionPnL:
    """Realised P&L for one (wallet, mint) pair.

    Uses weighted-average cost basis: every buy raises the average entry, every
    sell realises `(exit_price - avg_entry) * qty` against it. Tokens that
    arrive without a buy (airdrops, dev allocations, transfers in) have zero
    cost basis and are tracked separately so they cannot manufacture an
    infinite ROI.
    """

    wallet: str
    mint: str
    buys: int = 0
    sells: int = 0
    tokens_bought: float = 0.0
    tokens_sold: float = 0.0
    cost_usd: float = 0.0           # USD spent on tokens actually sold
    proceeds_usd: float = 0.0       # USD received from those sells
    total_buy_usd: float = 0.0      # USD spent across all buys
    total_sell_usd: float = 0.0     # USD received across all sells
    realised_pnl_usd: float = 0.0
    roi: Optional[float] = None     # realised_pnl / cost of the sold portion
    avg_entry_price: Optional[float] = None
    avg_exit_price: Optional[float] = None
    first_buy_ts: Optional[int] = None
    first_buy_slot: Optional[int] = None
    last_sell_ts: Optional[int] = None
    hold_seconds: Optional[float] = None       # cost-weighted holding period
    remaining_tokens: float = 0.0
    remaining_cost_usd: float = 0.0
    unrealised_pnl_usd: Optional[float] = None  # marked at last tape price
    zero_cost_tokens: float = 0.0   # tokens sold that were never bought
    zero_cost_proceeds_usd: float = 0.0  # USD from those tokens (real, but not repeatable)
    is_closed: bool = False         # position fully exited (dust tolerance)

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["is_closed"] = int(row["is_closed"])
        return row


@dataclass(slots=True)
class WalletScore:
    """Repeatability-oriented scorecard for a wallet."""

    wallet: str
    tokens_traded: int = 0
    closed_positions: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    win_rate_shrunk: float = 0.0
    median_roi: float = 0.0
    mean_roi: float = 0.0
    roi_stdev: float = 0.0
    gross_profit_usd: float = 0.0
    gross_loss_usd: float = 0.0
    realised_pnl_usd: float = 0.0
    windfall_pnl_usd: float = 0.0
    profit_factor: Optional[float] = None
    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0
    median_hold_seconds: Optional[float] = None
    first_trade_ts: Optional[int] = None
    last_trade_ts: Optional[int] = None
    days_since_last_trade: Optional[float] = None
    top1_profit_share: float = 0.0
    top3_profit_share: float = 0.0
    profit_hhi: float = 0.0
    single_outlier: bool = False
    pnl_without_best_usd: float = 0.0
    consistency_score: float = 0.0
    raw_score: float = 0.0
    penalty: float = 0.0
    score: float = 0.0
    components_json: str = "{}"

    def as_row(self) -> dict[str, Any]:
        row = asdict(self)
        row["single_outlier"] = int(row["single_outlier"])
        return row


@dataclass(slots=True)
class WalletFlags:
    """Bait / insider / sybil signals for a wallet."""

    wallet: str
    launch_block_buys: int = 0
    launch_window_buys: int = 0
    sniper_rate: float = 0.0
    median_entry_lag_seconds: Optional[float] = None
    insider_p_value: Optional[float] = None
    insider_suspicion: float = 0.0
    cluster_id: Optional[int] = None
    cluster_size: int = 1
    lockstep_score: float = 0.0
    lockstep_peers: int = 0
    penalty: float = 0.0
    reasons: list[str] = field(default_factory=list)

    def as_row(self) -> dict[str, Any]:
        import json

        row = asdict(self)
        row["reasons_json"] = json.dumps(row.pop("reasons"))
        return row


@dataclass(slots=True)
class SwapLeg:
    """A provider-agnostic swap as seen from one wallet, before USD pricing.

    `token_delta` is signed: positive means the wallet ended up with more of
    `mint` (a buy), negative means it sold. `quote_delta` is the signed change
    in the quote currency and should have the opposite sign.
    """

    signature: str
    wallet: str
    mint: str
    token_delta: float
    quote_mint: str
    quote_delta: float
    ts: int
    slot: int = 0
    dex: str = ""
    source: str = ""
    #: USD per whole token, when the provider states it (Birdeye does).
    price_usd_hint: Optional[float] = None

    @property
    def side(self) -> str:
        return BUY if self.token_delta > 0 else SELL
