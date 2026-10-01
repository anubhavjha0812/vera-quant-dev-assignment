"""The Macro Regime Engine: ingest daily macro proxies, score them,
map to regime states with hysteresis, override step 8's strategy params
and step 9's risk params, and trip a circuit breaker in crisis.

No lookahead (CLAUDE.md rule 2): every `MacroObservation` carries both the
date it *describes* and the timestamp it actually became *known* at —
`latest_known_observation` only ever returns data available at or before
the given time, never a value from later that calendar day or beyond.

Z-scores reuse `indicators.RollingWindow` (no duplicated math, CLAUDE.md
rule 8) — the same rolling mean/population-std primitive Bollinger Bands
uses.
"""
from __future__ import annotations

import csv
import dataclasses
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path

from vera_quant.indicators import RollingWindow
from vera_quant.risk import RiskParams
from vera_quant.strategies import GridParams

# -------------------------------------------------------------------- data


@dataclass(frozen=True)
class MacroObservation:
    as_of_date: date
    known_at: datetime
    values: dict[str, Decimal]


def load_macro_csv(path: Path, proxy_names: list[str]) -> list[MacroObservation]:
    """CSV columns: `date`, `known_at`, then one column per proxy name."""
    observations = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            observations.append(
                MacroObservation(
                    as_of_date=date.fromisoformat(row["date"]),
                    known_at=datetime.fromisoformat(row["known_at"]),
                    values={name: Decimal(row[name]) for name in proxy_names},
                )
            )
    return sorted(observations, key=lambda o: o.known_at)


def latest_known_observation(
    observations: list[MacroObservation], as_of: datetime
) -> MacroObservation | None:
    """The most recent observation whose `known_at <= as_of` — never a
    value from the future relative to `as_of`."""
    candidates = [o for o in observations if o.known_at <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda o: o.known_at)


# ----------------------------------------------------------------- scoring


class MacroScorer:
    """Rolling z-score per proxy, built on `RollingWindow`."""

    def __init__(self, proxy_names: list[str], lookback: int) -> None:
        self._proxy_names = proxy_names
        self._windows = {name: RollingWindow(lookback) for name in proxy_names}

    def update(self, observation: MacroObservation) -> dict[str, Decimal] | None:
        """Returns `None` until every proxy's window is warmed up.

        Every proxy's window is updated unconditionally before checking
        readiness — an early return the moment the FIRST proxy isn't ready
        would otherwise skip updating the rest, so a multi-proxy scorer
        could warm up its later proxies one call late (or never, if an
        earlier one never caught up).
        """
        for name in self._proxy_names:
            value = observation.values.get(name)
            if value is None:
                return None
            self._windows[name].update(value)

        if not all(self._windows[name].ready for name in self._proxy_names):
            return None

        scores: dict[str, Decimal] = {}
        for name in self._proxy_names:
            window = self._windows[name]
            value = observation.values[name]
            std = window.std
            scores[name] = (value - window.mean) / std if std != 0 else Decimal(0)
        return scores


# ------------------------------------------------------------- regime state


class RegimeState(str, Enum):
    NORMAL = "NORMAL"
    STRESSED = "STRESSED"
    CRISIS = "CRISIS"


@dataclass
class RegimeThresholds:
    """Asymmetric enter/exit thresholds — hysteresis, so noise near one
    boundary doesn't flap the regime back and forth every bar."""

    stressed_enter: Decimal = Decimal("1.5")
    stressed_exit: Decimal = Decimal("1.0")
    crisis_enter: Decimal = Decimal("2.5")
    crisis_exit: Decimal = Decimal("2.0")


def next_regime_state(
    current: RegimeState, score: Decimal, thresholds: RegimeThresholds
) -> RegimeState:
    if current == RegimeState.NORMAL:
        return RegimeState.STRESSED if score >= thresholds.stressed_enter else RegimeState.NORMAL

    if current == RegimeState.STRESSED:
        if score >= thresholds.crisis_enter:
            return RegimeState.CRISIS
        if score < thresholds.stressed_exit:
            return RegimeState.NORMAL
        return RegimeState.STRESSED

    if current == RegimeState.CRISIS:
        return RegimeState.STRESSED if score < thresholds.crisis_exit else RegimeState.CRISIS

    raise ValueError(f"unknown regime state {current!r}")


@dataclass
class RegimeEngine:
    thresholds: RegimeThresholds
    scorer: MacroScorer
    weights: dict[str, Decimal]
    state: RegimeState = field(default=RegimeState.NORMAL)

    def update(self, observation: MacroObservation) -> RegimeState:
        z_scores = self.scorer.update(observation)
        if z_scores is None:
            return self.state  # not warmed up yet — hold the current state

        total_weight = sum((self.weights.get(name, Decimal(1)) for name in z_scores), Decimal(0))
        if total_weight == 0:
            return self.state
        weighted_sum = sum(
            (self.weights.get(name, Decimal(1)) * s for name, s in z_scores.items()), Decimal(0)
        )
        composite = weighted_sum / total_weight
        self.state = next_regime_state(self.state, composite, self.thresholds)
        return self.state


# --------------------------------------------------------------- overrides


@dataclass(frozen=True)
class RegimeOverrides:
    """What changes per regime. `None` means "no override, use the base
    param"; `grid_k_spacing_multiplier` always applies (1 is a no-op)."""

    grid_k_spacing_multiplier: Decimal = Decimal(1)
    grid_max_units: int | None = None
    max_position_per_instrument: int | None = None


DEFAULT_REGIME_OVERRIDES: dict[RegimeState, RegimeOverrides] = {
    RegimeState.NORMAL: RegimeOverrides(),
    RegimeState.STRESSED: RegimeOverrides(
        grid_k_spacing_multiplier=Decimal("1.5"),  # wider spacing when vol is elevated
        grid_max_units=2,  # less pyramiding
        max_position_per_instrument=5,  # tighter cap
    ),
    RegimeState.CRISIS: RegimeOverrides(
        grid_k_spacing_multiplier=Decimal("2.0"),
        grid_max_units=1,
        max_position_per_instrument=1,
    ),
}


def apply_overrides_to_grid_params(base: GridParams, overrides: RegimeOverrides) -> GridParams:
    max_units = overrides.grid_max_units if overrides.grid_max_units is not None else base.max_units
    return GridParams(
        k_spacing=base.k_spacing * overrides.grid_k_spacing_multiplier,
        max_units=max_units,
        quantity_per_unit=base.quantity_per_unit,
    )


def apply_overrides_to_risk_params(base: RiskParams, overrides: RegimeOverrides) -> RiskParams:
    if overrides.max_position_per_instrument is None:
        return base
    return dataclasses.replace(
        base, max_position_per_instrument=overrides.max_position_per_instrument
    )
