"""Tick direction classifier: uptick / downtick / zero-tick."""

from __future__ import annotations


class TickClassifier:
    """Classify each trade as uptick (+1), downtick (-1), or zero-tick (0).

    Uses simple tick rule: compare current price to previous trade price
    for the same symbol. First trade of the session gets direction 0.
    """

    def __init__(self) -> None:
        self._last_price: dict[str, float] = {}

    def classify(self, symbol: str, price: float) -> int:
        prev = self._last_price.get(symbol)
        self._last_price[symbol] = price
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
