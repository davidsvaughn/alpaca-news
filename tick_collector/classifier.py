"""Tick direction classifier: uptick / downtick / zero-tick."""

from __future__ import annotations


class TickClassifier:
    """Classify each trade as buyer-initiated (+1), seller-initiated (-1), or neutral (0).

    Uses Lee-Ready algorithm: compare trade price to bid/ask midpoint.
    Falls back to simple tick rule (compare to previous price) when
    bid/ask is unavailable or price equals midpoint.
    """

    def __init__(self) -> None:
        self._last_price: dict[str, float] = {}

    def classify(
        self,
        symbol: str,
        price: float,
        bid: float | None = None,
        ask: float | None = None,
    ) -> int:
        # Lee-Ready: compare to bid/ask midpoint
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            midpoint = (bid + ask) / 2
            if price > midpoint:
                direction = 1
            elif price < midpoint:
                direction = -1
            else:
                # At midpoint — fall back to tick rule
                direction = self._tick_rule(symbol, price)
        else:
            # No bid/ask — pure tick rule
            direction = self._tick_rule(symbol, price)

        self._last_price[symbol] = price
        return direction

    def _tick_rule(self, symbol: str, price: float) -> int:
        """Simple tick rule: compare to previous trade price."""
        prev = self._last_price.get(symbol)
        if prev is None:
            return 0
        if price > prev:
            return 1
        if price < prev:
            return -1
        return 0

    def reset(self, symbol: str | None = None) -> None:
        if symbol:
            self._last_price.pop(symbol, None)
        else:
            self._last_price.clear()
