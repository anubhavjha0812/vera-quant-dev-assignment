"""Per-leg transaction cost model: brokerage, STT/CTT, exchange transaction
charges, SEBI fee, stamp duty and GST.

**`CostRateSchedule`'s defaults are sourced, dated rates — not guesses,
and not the zero-placeholders this module shipped with (step 5).** They
were pulled via live web search on 2026-10-01 (Claude's training data
predates the 1 Feb 2026 Union Budget, which hiked F&O STT materially
effective 1 Apr 2026 — exactly the kind of change that made guessing
unsafe; see `DECISIONS.md` #13). Sources are cited per field below.

**These still need your own final check** against Angel One's live
brokerage calculator and a few of your own real contract notes before
being trusted "to the paisa" for submission — that was always the
dev plan's own instruction (the strongest proof), and a web search
synthesis is a good starting point, not a replacement for it. Nothing in
this module hardcodes a rate outside `CostRateSchedule` — they all flow
through it, so correcting a figure is a one-place change.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from vera_quant.models import Instrument, TransactionType

_PAISA = Decimal("0.01")


class InstrumentSegment(str, Enum):
    NSE_FUTURES = "NSE_FUTURES"
    NSE_OPTIONS = "NSE_OPTIONS"
    MCX_FUTURES = "MCX_FUTURES"
    MCX_OPTIONS = "MCX_OPTIONS"


@dataclass(frozen=True)
class CostRateSchedule:
    """All rates as decimal fractions (e.g. `Decimal("0.0005")` = 0.05%).

    Sourced 2026-10-01 via web search (see module docstring); verify
    against Angel One's calculator + your own contract notes before
    trusting this "to the paisa."
    """

    # Brokerage — Angel One F&O/commodity is flat per executed order (no
    # percentage leg; that pricing only applies to the equity cash segment,
    # which this project doesn't trade). [angelone.in/exchange-transaction-charges]
    brokerage_flat_per_order: Decimal = Decimal("20")
    brokerage_pct_of_turnover: Decimal = Decimal("0")

    # Securities Transaction Tax — NSE F&O only, sell side only.
    # Hiked by Union Budget 2026, effective 1 Apr 2026: futures 0.02% ->
    # 0.05%; options premium 0.10% -> 0.15%. [lakshmishree.com/blog/securities-transaction-tax,
    # hdfc.bank.in/blogs/union-budget/stt-hike-on-f-o-trading]
    stt_futures_sell_pct: Decimal = Decimal("0.0005")
    stt_options_sell_pct_of_premium: Decimal = Decimal("0.0015")

    # Commodities Transaction Tax — MCX only, sell side only; agri
    # commodities are CTT-exempt (not modelled — no agri instruments here).
    # [mcxccl.com/clearing-settlement/commodities-transaction-tax]
    ctt_futures_sell_pct: Decimal = Decimal("0.0001")
    ctt_options_sell_pct_of_premium: Decimal = Decimal("0.0005")

    # Exchange transaction charges — both sides, split by exchange since
    # NSE and MCX differ (most visibly on options). [angelone.in/exchange-transaction-charges]
    exchange_txn_pct_nse_futures: Decimal = Decimal("0.000019")  # ~0.0019%
    exchange_txn_pct_nse_options_of_premium: Decimal = Decimal("0.0005")  # ~0.05%
    exchange_txn_pct_mcx_futures: Decimal = Decimal("0.000021")  # 0.0021%
    exchange_txn_pct_mcx_options_of_premium: Decimal = Decimal("0.000418")  # 0.0418%

    # SEBI turnover fee — ₹10/crore, both sides, same for every segment.
    # [nseindia.com/static/invest/first-time-investor-sebi-turnover-fees-stt-other-levies]
    sebi_fee_pct: Decimal = Decimal("0.0000001")

    # Stamp duty — buy side only. [centralised stamp duty rules; figures
    # per lakshmishree.com/blog/securities-transaction-tax]
    stamp_duty_buy_pct_futures: Decimal = Decimal("0.00002")  # 0.002%
    stamp_duty_buy_pct_options_of_premium: Decimal = Decimal("0.00003")  # 0.003%

    # GST — applied to (brokerage + exchange transaction charge + SEBI fee)
    # only. Never applied to STT, CTT or stamp duty.
    gst_pct: Decimal = Decimal("0.18")


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: Decimal
    stt: Decimal
    ctt: Decimal
    exchange_txn_charge: Decimal
    sebi_fee: Decimal
    stamp_duty: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage
            + self.stt
            + self.ctt
            + self.exchange_txn_charge
            + self.sebi_fee
            + self.stamp_duty
            + self.gst
        )


def segment_for(instrument: Instrument, *, is_option: bool) -> InstrumentSegment:
    if instrument.exchange in ("NFO", "NSE"):
        return InstrumentSegment.NSE_OPTIONS if is_option else InstrumentSegment.NSE_FUTURES
    if instrument.exchange == "MCX":
        return InstrumentSegment.MCX_OPTIONS if is_option else InstrumentSegment.MCX_FUTURES
    raise ValueError(f"no cost segment configured for exchange {instrument.exchange!r}")


def _round(amount: Decimal) -> Decimal:
    return amount.quantize(_PAISA)


def _brokerage(turnover: Decimal, schedule: CostRateSchedule) -> Decimal:
    pct_based = turnover * schedule.brokerage_pct_of_turnover
    if schedule.brokerage_flat_per_order > 0 and schedule.brokerage_pct_of_turnover > 0:
        return min(schedule.brokerage_flat_per_order, pct_based)
    if schedule.brokerage_flat_per_order > 0:
        return schedule.brokerage_flat_per_order
    return pct_based


def compute_trade_cost(
    *,
    instrument: Instrument,
    transaction_type: TransactionType,
    price: Decimal,
    quantity: int,
    is_option: bool,
    schedule: CostRateSchedule,
) -> CostBreakdown:
    """Per-leg transaction cost for one fill.

    `price` is the fill price (premium, for options); `quantity` is the
    number of units actually traded — lots * lot_size already applied by
    the caller, not the lot count.
    """
    turnover = price * quantity
    segment = segment_for(instrument, is_option=is_option)
    is_sell = transaction_type == TransactionType.SELL
    is_buy = not is_sell

    if segment == InstrumentSegment.NSE_FUTURES:
        stt = turnover * schedule.stt_futures_sell_pct if is_sell else Decimal(0)
        ctt = Decimal(0)
        exchange_txn = turnover * schedule.exchange_txn_pct_nse_futures
        stamp_duty = turnover * schedule.stamp_duty_buy_pct_futures if is_buy else Decimal(0)
    elif segment == InstrumentSegment.NSE_OPTIONS:
        stt = turnover * schedule.stt_options_sell_pct_of_premium if is_sell else Decimal(0)
        ctt = Decimal(0)
        exchange_txn = turnover * schedule.exchange_txn_pct_nse_options_of_premium
        stamp_duty = (
            turnover * schedule.stamp_duty_buy_pct_options_of_premium if is_buy else Decimal(0)
        )
    elif segment == InstrumentSegment.MCX_FUTURES:
        stt = Decimal(0)
        ctt = turnover * schedule.ctt_futures_sell_pct if is_sell else Decimal(0)
        exchange_txn = turnover * schedule.exchange_txn_pct_mcx_futures
        stamp_duty = turnover * schedule.stamp_duty_buy_pct_futures if is_buy else Decimal(0)
    else:  # MCX_OPTIONS
        stt = Decimal(0)
        ctt = turnover * schedule.ctt_options_sell_pct_of_premium if is_sell else Decimal(0)
        exchange_txn = turnover * schedule.exchange_txn_pct_mcx_options_of_premium
        stamp_duty = (
            turnover * schedule.stamp_duty_buy_pct_options_of_premium if is_buy else Decimal(0)
        )

    brokerage = _brokerage(turnover, schedule)
    sebi_fee = turnover * schedule.sebi_fee_pct
    gst = (brokerage + exchange_txn + sebi_fee) * schedule.gst_pct

    return CostBreakdown(
        brokerage=_round(brokerage),
        stt=_round(stt),
        ctt=_round(ctt),
        exchange_txn_charge=_round(exchange_txn),
        sebi_fee=_round(sebi_fee),
        stamp_duty=_round(stamp_duty),
        gst=_round(gst),
    )
