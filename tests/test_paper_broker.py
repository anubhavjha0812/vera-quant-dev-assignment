"""PaperBroker fill rules (dev plan, step 11):
- decide at bar close; market orders fill at the next bar's open
- stop/limit orders fill when a later bar crosses them, at the open if
  that bar gapped through
- if both sides trigger in one bar, assume the adverse (stop) one first
- slippage in ticks on market/stop fills, never on a true limit fill
- costs from step 5's cost model
"""
from datetime import datetime
from decimal import Decimal

from vera_quant.brokers.paper import PaperBroker
from vera_quant.costs import CostRateSchedule
from vera_quant.models import Instrument, Order, OrderStatus, OrderType, TransactionType


def _instrument() -> Instrument:
    return Instrument(
        exchange="NFO",
        trading_symbol="NIFTY25DECFUT",
        symbol_token="26000",
        tick_size=Decimal("0.05"),
        lot_size=25,
    )


def _bar(
    token: str, ts: datetime, open_: str, high: str, low: str, close: str, volume: int = 100
):
    from vera_quant.models import Bar

    return Bar(
        instrument_token=token,
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


def _order(
    order_type: OrderType,
    side: TransactionType,
    qty: int = 25,
    limit_price: Decimal | None = None,
    key: str = "k1",
) -> Order:
    return Order(
        idempotency_key=key,
        instrument=_instrument(),
        transaction_type=side,
        quantity=qty,
        order_type=order_type,
        limit_price=limit_price,
        status=OrderStatus.PENDING,
    )


def _broker(slippage_ticks: Decimal = Decimal(0)) -> PaperBroker:
    return PaperBroker(
        slippage_ticks=slippage_ticks, cost_schedule=CostRateSchedule(), is_option=False
    )


def test_market_order_fills_at_the_next_bars_open_not_the_submission_bar() -> None:
    # Real usage, per backtest.py's loop: on_bar(bar_i) resolves whatever was
    # queued from bar_{i-1}'s decision, THEN that bar's own decision submits
    # new orders, which only become eligible on the NEXT on_bar call.
    broker = _broker()
    bar0 = _bar("26000", datetime(2026, 1, 2, 9, 15), "100", "101", "99", "100.5")
    bar1 = _bar("26000", datetime(2026, 1, 2, 9, 20), "102", "103", "101", "102.5")

    assert broker.on_bar(bar0) == []  # nothing queued yet, resolving bar0
    broker.submit_order(_order(OrderType.MARKET, TransactionType.BUY))  # decided on bar0's close

    fills = broker.on_bar(bar1)  # first on_bar call after submission
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("102")  # bar1's open, not bar0's close or open


def test_limit_order_fills_at_the_limit_price_when_crossed_mid_bar() -> None:
    broker = _broker()
    broker.submit_order(
        _order(OrderType.LIMIT, TransactionType.BUY, limit_price=Decimal("99"))
    )
    bar = _bar("26000", datetime(2026, 1, 2, 9, 20), "101", "102", "98", "100")
    fills = broker.on_bar(bar)
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("99")  # exact limit, not the open or low


def test_limit_order_fills_at_the_open_when_the_bar_gaps_through_it() -> None:
    broker = _broker()
    broker.submit_order(
        _order(OrderType.LIMIT, TransactionType.BUY, limit_price=Decimal("99"))
    )
    bar = _bar("26000", datetime(2026, 1, 2, 9, 20), "95", "96", "94", "95.5")
    fills = broker.on_bar(bar)
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("95")  # better than the limit -> fills at the open


def test_limit_order_does_not_fill_when_never_crossed() -> None:
    broker = _broker()
    broker.submit_order(
        _order(OrderType.LIMIT, TransactionType.BUY, limit_price=Decimal("90"))
    )
    bar = _bar("26000", datetime(2026, 1, 2, 9, 20), "100", "102", "98", "101")
    assert broker.on_bar(bar) == []
    # still pending for the next bar
    bar2 = _bar("26000", datetime(2026, 1, 2, 9, 25), "99", "100", "89", "95")
    fills = broker.on_bar(bar2)
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("90")


def test_stop_order_fills_at_the_stop_price_when_crossed() -> None:
    broker = _broker()
    broker.submit_order(_order(OrderType.SL, TransactionType.SELL, limit_price=Decimal("95")))
    bar = _bar("26000", datetime(2026, 1, 2, 9, 20), "100", "101", "93", "96")
    fills = broker.on_bar(bar)
    assert len(fills) == 1
    assert fills[0].fill_price == Decimal("95")


def test_limit_fill_is_never_worse_than_the_limit_even_with_slippage_configured() -> None:
    broker = _broker(slippage_ticks=Decimal(2))  # slippage configured...
    broker.submit_order(
        _order(OrderType.LIMIT, TransactionType.BUY, limit_price=Decimal("99"))
    )
    bar = _bar("26000", datetime(2026, 1, 2, 9, 20), "101", "102", "98", "100")
    fills = broker.on_bar(bar)
    assert fills[0].fill_price == Decimal("99")  # ...but never applied to a true limit fill


def test_market_order_fill_includes_adverse_slippage() -> None:
    broker = _broker(slippage_ticks=Decimal(2))  # 2 ticks * 0.05 = 0.10
    bar0 = _bar("26000", datetime(2026, 1, 2, 9, 15), "100", "101", "99", "100.5")
    broker.on_bar(bar0)
    broker.submit_order(_order(OrderType.MARKET, TransactionType.BUY))
    bar1 = _bar("26000", datetime(2026, 1, 2, 9, 20), "102", "103", "101", "102.5")
    fills = broker.on_bar(bar1)
    assert fills[0].fill_price == Decimal("102.10")  # buy slips worse (higher)


def test_sell_market_order_slippage_is_worse_lower() -> None:
    broker = _broker(slippage_ticks=Decimal(2))
    bar0 = _bar("26000", datetime(2026, 1, 2, 9, 15), "100", "101", "99", "100.5")
    broker.on_bar(bar0)
    broker.submit_order(_order(OrderType.MARKET, TransactionType.SELL))
    bar1 = _bar("26000", datetime(2026, 1, 2, 9, 20), "102", "103", "101", "102.5")
    fills = broker.on_bar(bar1)
    assert fills[0].fill_price == Decimal("101.90")  # sell slips worse (lower)


def test_stop_triggers_before_limit_when_both_cross_in_the_same_bar() -> None:
    broker = _broker()
    broker.submit_order(
        _order(OrderType.LIMIT, TransactionType.SELL, limit_price=Decimal("105"), key="target")
    )
    broker.submit_order(
        _order(OrderType.SL, TransactionType.SELL, limit_price=Decimal("95"), key="stop")
    )
    # One wild bar whose range crosses both the 105 target and the 95 stop.
    bar = _bar("26000", datetime(2026, 1, 2, 9, 20), "100", "106", "94", "97")
    fills = broker.on_bar(bar)
    assert len(fills) == 2
    assert fills[0].order_idempotency_key == "stop"  # adverse one resolved first
    assert fills[1].order_idempotency_key == "target"


def test_fill_fees_come_from_the_cost_schedule() -> None:
    schedule = CostRateSchedule(brokerage_flat_per_order=Decimal("20"))
    broker = PaperBroker(slippage_ticks=Decimal(0), cost_schedule=schedule, is_option=False)
    bar0 = _bar("26000", datetime(2026, 1, 2, 9, 15), "100", "101", "99", "100.5")
    broker.on_bar(bar0)
    broker.submit_order(_order(OrderType.MARKET, TransactionType.BUY))
    bar1 = _bar("26000", datetime(2026, 1, 2, 9, 20), "102", "103", "101", "102.5")
    fills = broker.on_bar(bar1)
    assert fills[0].fees == Decimal("20.00")


def test_orders_for_a_different_instrument_are_left_untouched() -> None:
    broker = _broker()
    broker.submit_order(_order(OrderType.MARKET, TransactionType.BUY))
    other_bar = _bar("99999", datetime(2026, 1, 2, 9, 20), "50", "51", "49", "50.5")
    assert broker.on_bar(other_bar) == []
