"""Per-leg transaction cost model: brokerage, STT/CTT, exchange transaction
charges, SEBI fee, stamp duty and GST.

**Rate values in `CostRateSchedule` are placeholders (pending the user's own
Angel One brokerage-calculator figures and real contract notes).** Nothing
in this module hardcodes a rate — they all flow through `CostRateSchedule`,
so dropping in the real numbers is a one-place change. Only the calculation
*structure* (which charge applies on which side, what GST is computed on,
rounding) is verified until then — see tests/test_costs.py.
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
    """All rates as decimal fractions (e.g. `Decimal("0.0002")` for 0.02%).

    PLACEHOLDER VALUES — every field defaults to 0 and must be replaced with
    the verified current rate before any output here can be trusted "to the
    paisa". See the module docstring.
    """

    # Brokerage: flat fee per executed order, or a percentage of turnover,
    # whichever is lower (standard discount-broker model).
    brokerage_flat_per_order: Decimal = Decimal("0")
    brokerage_pct_of_turnover: Decimal = Decimal("0")

    # Securities Transaction Tax — NSE F&O only, sell side only.
    stt_futures_sell_pct: Decimal = Decimal("0")
    stt_options_sell_pct_of_premium: Decimal = Decimal("0")

    # Commodities Transaction Tax — MCX only, sell side only.
    ctt_futures_sell_pct: Decimal = Decimal("0")
    ctt_options_sell_pct_of_premium: Decimal = Decimal("0")

    # Exchange transaction charges — both sides.
    exchange_txn_pct_futures: Decimal = Decimal("0")
    exchange_txn_pct_options_of_premium: Decimal = Decimal("0")

    # SEBI turnover fee — both sides, same rate for every segment.
    sebi_fee_pct: Decimal = Decimal("0")

    # Stamp duty — buy side only.
    stamp_duty_buy_pct_futures: Decimal = Decimal("0")
    stamp_duty_buy_pct_options_of_premium: Decimal = Decimal("0")

    # GST — applied to (brokerage + exchange transaction charge + SEBI fee)
    # only. Never applied to STT, CTT or stamp duty.
    gst_pct: Decimal = Decimal("0")


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
        exchange_txn = turnover * schedule.exchange_txn_pct_futures
        stamp_duty = turnover * schedule.stamp_duty_buy_pct_futures if is_buy else Decimal(0)
    elif segment == InstrumentSegment.NSE_OPTIONS:
        stt = turnover * schedule.stt_options_sell_pct_of_premium if is_sell else Decimal(0)
        ctt = Decimal(0)
        exchange_txn = turnover * schedule.exchange_txn_pct_options_of_premium
        stamp_duty = (
            turnover * schedule.stamp_duty_buy_pct_options_of_premium if is_buy else Decimal(0)
        )
    elif segment == InstrumentSegment.MCX_FUTURES:
        stt = Decimal(0)
        ctt = turnover * schedule.ctt_futures_sell_pct if is_sell else Decimal(0)
        exchange_txn = turnover * schedule.exchange_txn_pct_futures
        stamp_duty = turnover * schedule.stamp_duty_buy_pct_futures if is_buy else Decimal(0)
    else:  # MCX_OPTIONS
        stt = Decimal(0)
        ctt = turnover * schedule.ctt_options_sell_pct_of_premium if is_sell else Decimal(0)
        exchange_txn = turnover * schedule.exchange_txn_pct_options_of_premium
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
