from trader.market.data_service import MarketDataService


class _DummySchwab:
    available = False


class _DummyYF:
    def get_quote(self, symbol: str) -> dict:
        return {"last_price": 10.0}

    def get_fundamentals(self, symbol: str) -> dict:
        return {"eps": -2.0, "avg_volume": None, "market_cap": None}


def test_quotes_with_fundamentals_sets_zero_pe_for_non_positive_eps(monkeypatch) -> None:
    market = object.__new__(MarketDataService)
    market._schwab = _DummySchwab()
    market._yfinance = _DummyYF()
    monkeypatch.setattr(market, "_compute_avg_volume_from_history", lambda symbol, as_of: 123.0)

    result = market.get_quotes_with_fundamentals(
        ["TEST"],
        fill_avg_volume_from_history=True,
    )

    assert result["TEST"]["pe_ratio"] == 0.0
    assert result["TEST"]["avg_10d_volume"] == 123.0
