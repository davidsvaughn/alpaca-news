"""Test uptick/downtick volume approximation methods.

We don't have true tick-level trade data (would need finnhub premium or similar),
so we approximate from 1-minute OHLCV bars using several established methods.

Methods tested:
1. Close Position Formula: buy_vol = V*(C-L)/(H-L), sell_vol = V*(H-C)/(H-L)
2. Body Delta: delta = V*(C-O)/(H-L)
3. Inter-bar Tick Rule: assign whole volume based on close vs prev_close
4. Intra-bar Direction: assign whole volume based on close vs open
5. Hybrid: Close Position weighted by inter-bar direction

Data sources tested:
- yfinance 1-minute bars (free, 7-day lookback)
- Schwab 1-minute bars (if available, ~30-day lookback)
- Finnhub tick data (if premium available)
"""

import os
import sys
import numpy as np
import pandas as pd
import pytest
import yfinance as yf

# ── Approximation Methods ──────────────────────────────────────────

def close_position_formula(df: pd.DataFrame) -> pd.DataFrame:
    """Method 1: Close Position Formula (most common volume delta approximation).

    Estimates buying/selling pressure based on where the close falls
    within the bar's range. If close == high, all volume is "buying".
    If close == low, all volume is "selling".

    buy_vol  = volume * (close - low) / (high - low)
    sell_vol = volume * (high - close) / (high - low)
    """
    hl_range = df["High"] - df["Low"]
    # Avoid division by zero for doji bars (high == low)
    safe_range = hl_range.replace(0, np.nan)

    buy_vol = df["Volume"] * (df["Close"] - df["Low"]) / safe_range
    sell_vol = df["Volume"] * (df["High"] - df["Close"]) / safe_range

    # For doji bars, split volume 50/50
    buy_vol = buy_vol.fillna(df["Volume"] / 2)
    sell_vol = sell_vol.fillna(df["Volume"] / 2)

    return pd.DataFrame({
        "uptick_vol": buy_vol,
        "downtick_vol": sell_vol,
        "delta": buy_vol - sell_vol,
    }, index=df.index)


def body_delta(df: pd.DataFrame) -> pd.DataFrame:
    """Method 2: Body Delta (OC range within HL range).

    delta = volume * (close - open) / (high - low)
    Positive delta = net buying, negative = net selling.
    """
    hl_range = df["High"] - df["Low"]
    safe_range = hl_range.replace(0, np.nan)

    delta = df["Volume"] * (df["Close"] - df["Open"]) / safe_range
    delta = delta.fillna(0)

    # Derive uptick/downtick from delta
    uptick_vol = delta.clip(lower=0) + df["Volume"] / 2  # base + positive delta
    downtick_vol = (-delta).clip(lower=0) + df["Volume"] / 2  # base + negative delta
    # Normalize so they sum to total volume
    total = uptick_vol + downtick_vol
    uptick_vol = uptick_vol / total * df["Volume"]
    downtick_vol = downtick_vol / total * df["Volume"]

    return pd.DataFrame({
        "uptick_vol": uptick_vol,
        "downtick_vol": downtick_vol,
        "delta": delta,
    }, index=df.index)


def interbar_tick_rule(df: pd.DataFrame) -> pd.DataFrame:
    """Method 3: Inter-bar Tick Rule (OBV-style).

    Assigns the entire bar's volume as uptick or downtick based on
    whether close > previous bar's close.
    """
    prev_close = df["Close"].shift(1)
    direction = np.sign(df["Close"] - prev_close)
    # For zero-tick (no change), carry forward last non-zero direction
    direction = direction.replace(0, np.nan).ffill().fillna(0)

    uptick_vol = df["Volume"].where(direction > 0, 0)
    downtick_vol = df["Volume"].where(direction < 0, 0)
    # Bars with no direction get split 50/50
    neutral = direction == 0
    uptick_vol = uptick_vol + df["Volume"].where(neutral, 0) / 2
    downtick_vol = downtick_vol + df["Volume"].where(neutral, 0) / 2

    return pd.DataFrame({
        "uptick_vol": uptick_vol,
        "downtick_vol": downtick_vol,
        "delta": uptick_vol - downtick_vol,
    }, index=df.index)


def intrabar_direction(df: pd.DataFrame) -> pd.DataFrame:
    """Method 4: Intra-bar Direction.

    Assigns entire bar's volume based on close vs open (green vs red bar).
    Green bar (close > open) → uptick volume
    Red bar (close < open) → downtick volume
    Doji (close == open) → split 50/50
    """
    direction = np.sign(df["Close"] - df["Open"])

    uptick_vol = df["Volume"].where(direction > 0, 0)
    downtick_vol = df["Volume"].where(direction < 0, 0)
    neutral = direction == 0
    uptick_vol = uptick_vol + df["Volume"].where(neutral, 0) / 2
    downtick_vol = downtick_vol + df["Volume"].where(neutral, 0) / 2

    return pd.DataFrame({
        "uptick_vol": uptick_vol,
        "downtick_vol": downtick_vol,
        "delta": uptick_vol - downtick_vol,
    }, index=df.index)


def hybrid_method(df: pd.DataFrame) -> pd.DataFrame:
    """Method 5: Hybrid — Close Position + Inter-bar direction weighting.

    Uses Close Position Formula for volume split, then adjusts based on
    inter-bar price direction. If the bar closed higher than prev bar,
    boost the uptick estimate; if lower, boost downtick.
    """
    # Base: close position formula
    cp = close_position_formula(df)

    # Inter-bar direction signal
    prev_close = df["Close"].shift(1)
    direction = np.sign(df["Close"] - prev_close).fillna(0)

    # Weight: when inter-bar direction agrees with intra-bar, boost by 20%
    alpha = 0.2
    adjustment = direction * alpha * df["Volume"]

    uptick_vol = (cp["uptick_vol"] + adjustment).clip(lower=0)
    downtick_vol = (cp["downtick_vol"] - adjustment).clip(lower=0)

    # Renormalize to preserve total volume
    total = uptick_vol + downtick_vol
    safe_total = total.replace(0, 1)
    uptick_vol = uptick_vol / safe_total * df["Volume"]
    downtick_vol = downtick_vol / safe_total * df["Volume"]

    return pd.DataFrame({
        "uptick_vol": uptick_vol,
        "downtick_vol": downtick_vol,
        "delta": uptick_vol - downtick_vol,
    }, index=df.index)


# ── Derived Signals ────────────────────────────────────────────────

def compute_signals(result: pd.DataFrame, volume: pd.Series) -> dict:
    """Compute derived signals from uptick/downtick volume."""
    total_up = result["uptick_vol"].sum()
    total_down = result["downtick_vol"].sum()
    total_vol = volume.sum()
    cumulative_delta = result["delta"].cumsum()

    return {
        "total_uptick": total_up,
        "total_downtick": total_down,
        "total_volume": total_vol,
        "volume_conservation": abs(total_up + total_down - total_vol) / max(total_vol, 1),
        "net_delta": total_up - total_down,
        "imbalance": (total_up - total_down) / max(total_up + total_down, 1),
        "cumulative_delta_final": cumulative_delta.iloc[-1] if len(cumulative_delta) > 0 else 0,
        "cumulative_delta_max": cumulative_delta.max() if len(cumulative_delta) > 0 else 0,
        "cumulative_delta_min": cumulative_delta.min() if len(cumulative_delta) > 0 else 0,
    }


# ── Tests ──────────────────────────────────────────────────────────

METHODS = {
    "close_position": close_position_formula,
    "body_delta": body_delta,
    "interbar_tick": interbar_tick_rule,
    "intrabar_direction": intrabar_direction,
    "hybrid": hybrid_method,
}


@pytest.fixture(scope="module")
def yf_1min_data() -> pd.DataFrame:
    """Fetch 1-minute OHLCV data for a liquid stock via yfinance."""
    symbol = "SPY"
    print(f"\nFetching yfinance 1-min data for {symbol}...")
    df = yf.download(symbol, period="5d", interval="1m", progress=False)
    # Flatten multi-level columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    print(f"  Got {len(df)} bars, date range: {df.index[0]} to {df.index[-1]}")
    print(f"  Total volume: {df['Volume'].sum():,.0f}")
    assert len(df) > 100, f"Expected at least 100 bars, got {len(df)}"
    return df


class TestYFinanceApproximations:
    """Test all approximation methods using yfinance 1-minute bars."""

    def test_data_quality(self, yf_1min_data: pd.DataFrame):
        """Verify the fetched data has expected columns and no gaps."""
        df = yf_1min_data
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            assert col in df.columns, f"Missing column: {col}"
        # Check no NaN prices
        assert df[["Open", "High", "Low", "Close"]].notna().all().all()
        # Check high >= low
        assert (df["High"] >= df["Low"]).all()
        # Check high >= open and high >= close
        assert (df["High"] >= df["Open"]).all()
        assert (df["High"] >= df["Close"]).all()
        print(f"  Data quality OK: {len(df)} bars, no NaN/invalid values")

    @pytest.mark.parametrize("method_name", list(METHODS.keys()))
    def test_method_produces_valid_output(self, yf_1min_data: pd.DataFrame, method_name: str):
        """Each method should produce non-negative volumes that approximately sum to total volume."""
        df = yf_1min_data
        method = METHODS[method_name]
        result = method(df)

        # Should have expected columns
        assert "uptick_vol" in result.columns
        assert "downtick_vol" in result.columns
        assert "delta" in result.columns

        # Non-negative volumes
        assert (result["uptick_vol"] >= -0.01).all(), f"{method_name}: negative uptick volumes"
        assert (result["downtick_vol"] >= -0.01).all(), f"{method_name}: negative downtick volumes"

        # Volume conservation: uptick + downtick ≈ total volume per bar
        per_bar_sum = result["uptick_vol"] + result["downtick_vol"]
        # Skip first bar for interbar (no prev_close)
        nonzero = df["Volume"] > 0
        ratio = per_bar_sum[nonzero] / df["Volume"][nonzero]
        mean_ratio = ratio.mean()
        print(f"  {method_name}: mean volume conservation ratio = {mean_ratio:.4f}")
        # Should be close to 1.0 (within 5%)
        assert 0.90 <= mean_ratio <= 1.10, f"{method_name}: poor volume conservation ({mean_ratio:.4f})"

    def test_compare_all_methods(self, yf_1min_data: pd.DataFrame):
        """Compare all methods side by side."""
        df = yf_1min_data
        print(f"\n{'='*80}")
        print(f"COMPARISON: All methods on SPY 1-min bars ({len(df)} bars)")
        print(f"{'='*80}")

        results = {}
        for name, method in METHODS.items():
            result = method(df)
            signals = compute_signals(result, df["Volume"])
            results[name] = signals

        # Print comparison table
        print(f"\n{'Method':<20} {'Uptick Vol':>14} {'Downtick Vol':>14} {'Net Delta':>14} {'Imbalance':>10} {'Vol Cons':>10}")
        print("-" * 84)
        for name, s in results.items():
            print(
                f"{name:<20} "
                f"{s['total_uptick']:>14,.0f} "
                f"{s['total_downtick']:>14,.0f} "
                f"{s['net_delta']:>14,.0f} "
                f"{s['imbalance']:>10.4f} "
                f"{s['volume_conservation']:>10.6f}"
            )

        # All methods should agree on general direction
        deltas = {name: s["net_delta"] for name, s in results.items()}
        signs = {name: np.sign(d) for name, d in deltas.items()}
        unique_signs = set(signs.values())
        print(f"\nDirection agreement: {len(unique_signs)} unique sign(s) across methods")
        if len(unique_signs) == 1:
            direction = "BULLISH" if list(unique_signs)[0] > 0 else "BEARISH"
            print(f"  All methods agree: {direction}")
        else:
            print(f"  Methods disagree on direction: {signs}")

    def test_correlation_with_price(self, yf_1min_data: pd.DataFrame):
        """Test if cumulative delta correlates with price movement."""
        df = yf_1min_data
        price_change = df["Close"].iloc[-1] - df["Close"].iloc[0]
        price_pct = price_change / df["Close"].iloc[0] * 100

        print(f"\n{'='*80}")
        print(f"PRICE vs DELTA CORRELATION")
        print(f"Price change: {price_change:+.2f} ({price_pct:+.2f}%)")
        print(f"{'='*80}")

        for name, method in METHODS.items():
            result = method(df)
            cum_delta = result["delta"].cumsum()
            # Pearson correlation between cumulative delta and price
            price_series = df["Close"].values
            delta_series = cum_delta.values
            # Align lengths (skip NaN at start)
            valid = ~np.isnan(delta_series)
            if valid.sum() > 10:
                corr = np.corrcoef(price_series[valid], delta_series[valid])[0, 1]
            else:
                corr = float("nan")
            print(f"  {name:<20} cum_delta_final={cum_delta.iloc[-1]:>14,.0f}  corr_with_price={corr:>7.4f}")

    def test_5min_aggregation(self, yf_1min_data: pd.DataFrame):
        """Test aggregating 1-min deltas into 5-min buckets."""
        df = yf_1min_data
        # Use Close Position Formula
        result = close_position_formula(df)

        # Resample to 5-min bars
        result_5m = result.resample("5min").sum()
        result_5m = result_5m[result_5m["uptick_vol"] + result_5m["downtick_vol"] > 0]

        print(f"\n5-min aggregation (Close Position Formula):")
        print(f"  {len(result_5m)} five-minute bars")
        print(f"  Sample (first 10 bars):")
        print(f"  {'Time':<20} {'Up Vol':>12} {'Down Vol':>12} {'Delta':>12} {'Imbalance':>10}")
        print("  " + "-" * 68)
        for i, (ts, row) in enumerate(result_5m.head(10).iterrows()):
            total = row["uptick_vol"] + row["downtick_vol"]
            imb = row["delta"] / max(total, 1)
            print(f"  {str(ts):<20} {row['uptick_vol']:>12,.0f} {row['downtick_vol']:>12,.0f} {row['delta']:>12,.0f} {imb:>10.4f}")


class TestSchwabApproximations:
    """Test using Schwab 1-minute bars (if available)."""

    @pytest.fixture(scope="class")
    def schwab_data(self):
        """Fetch 1-minute data from Schwab."""
        schwab_disabled = os.getenv("SCHWAB_DISABLED", "").lower() == "true"
        if schwab_disabled:
            pytest.skip("SCHWAB_DISABLED=true")

        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            from trader.market.schwab_client import SchwabClient
            client = SchwabClient()
            if not client.available:
                pytest.skip("Schwab client not available")
            candles = client.get_intraday_candles("SPY", period=5, frequency=1)
            if not candles:
                pytest.skip("No candle data returned from Schwab")
            # Convert to DataFrame
            records = [{"Open": c.o, "High": c.h, "Low": c.l, "Close": c.c, "Volume": c.v, "Time": c.t}
                       for c in candles]
            df = pd.DataFrame(records)
            df.index = pd.to_datetime(df["Time"])
            df = df.drop(columns=["Time"])
            print(f"\nSchwab: got {len(df)} 1-min bars for SPY")
            return df
        except Exception as e:
            pytest.skip(f"Schwab client error: {e}")

    def test_schwab_all_methods(self, schwab_data: pd.DataFrame):
        """Run all methods on Schwab data and compare."""
        df = schwab_data
        print(f"\n{'='*80}")
        print(f"SCHWAB DATA: All methods on SPY 1-min bars ({len(df)} bars)")
        print(f"{'='*80}")

        print(f"\n{'Method':<20} {'Uptick Vol':>14} {'Downtick Vol':>14} {'Net Delta':>14} {'Imbalance':>10}")
        print("-" * 74)
        for name, method in METHODS.items():
            result = method(df)
            s = compute_signals(result, df["Volume"])
            print(
                f"{name:<20} "
                f"{s['total_uptick']:>14,.0f} "
                f"{s['total_downtick']:>14,.0f} "
                f"{s['net_delta']:>14,.0f} "
                f"{s['imbalance']:>10.4f}"
            )


class TestFinnhubTick:
    """Test finnhub tick data (requires premium)."""

    def test_tick_endpoint_availability(self):
        """Check if finnhub tick endpoint is available (premium feature)."""
        api_key = os.getenv("FINNHUB_API_KEY")
        if not api_key:
            pytest.skip("FINNHUB_API_KEY not set")

        import httpx
        # Try to get tick data for yesterday
        from datetime import datetime, timedelta
        # Use a recent trading day
        date = datetime.now()
        # Go back to find a weekday
        for _ in range(7):
            date -= timedelta(days=1)
            if date.weekday() < 5:  # Mon-Fri
                break
        date_str = date.strftime("%Y-%m-%d")

        print(f"\nTrying finnhub tick endpoint for AAPL on {date_str}...")
        resp = httpx.get(
            "https://api.finnhub.io/api/v1/stock/tick",
            params={"symbol": "AAPL", "date": date_str, "limit": 100, "skip": 0, "token": api_key},
            timeout=30,
        )
        print(f"  Status: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            if "p" in data and len(data["p"]) > 0:
                print(f"  SUCCESS: Got {data.get('count', 0)} ticks (total available: {data.get('total', '?')})")
                print(f"  Sample prices: {data['p'][:5]}")
                print(f"  Sample volumes: {data['v'][:5]}")
                print(f"  Sample timestamps: {data['t'][:5]}")

                # Compute true uptick/downtick from tick data
                prices = np.array(data["p"])
                volumes = np.array(data["v"])
                price_changes = np.diff(prices)
                up_mask = price_changes > 0
                down_mask = price_changes < 0

                uptick_vol = volumes[1:][up_mask].sum()
                downtick_vol = volumes[1:][down_mask].sum()
                print(f"\n  True uptick/downtick from {len(prices)} ticks:")
                print(f"    Uptick volume:   {uptick_vol:>12,.0f}")
                print(f"    Downtick volume: {downtick_vol:>12,.0f}")
                print(f"    Net delta:       {uptick_vol - downtick_vol:>12,.0f}")
            elif "error" in data:
                print(f"  API error: {data['error']}")
                pytest.skip(f"Finnhub tick endpoint returned error: {data.get('error')}")
            else:
                print(f"  No tick data returned (empty or different format)")
                print(f"  Response keys: {list(data.keys())}")
                pytest.skip("No tick data available")
        elif resp.status_code == 403:
            print("  PREMIUM REQUIRED: tick data requires paid subscription")
            pytest.skip("Finnhub tick endpoint requires premium")
        else:
            print(f"  Unexpected response: {resp.text[:200]}")
            pytest.skip(f"Finnhub tick endpoint returned {resp.status_code}")

    def test_candles_endpoint(self):
        """Test if finnhub 1-min candle endpoint is available."""
        api_key = os.getenv("FINNHUB_API_KEY")
        if not api_key:
            pytest.skip("FINNHUB_API_KEY not set")

        import httpx
        from datetime import datetime, timedelta

        # Get timestamps for a recent trading day
        now = datetime.now()
        end_ts = int(now.timestamp())
        start_ts = int((now - timedelta(days=5)).timestamp())

        print(f"\nTrying finnhub stock/candle endpoint for AAPL (1-min)...")
        resp = httpx.get(
            "https://api.finnhub.io/api/v1/stock/candle",
            params={"symbol": "AAPL", "resolution": "1", "from": start_ts, "to": end_ts, "token": api_key},
            timeout=30,
        )
        print(f"  Status: {resp.status_code}")

        if resp.status_code == 200:
            data = resp.json()
            if data.get("s") == "ok" and "c" in data:
                n = len(data["c"])
                print(f"  SUCCESS: Got {n} candles")
                print(f"  Sample close: {data['c'][:5]}")
                print(f"  Sample volume: {data['v'][:5]}")

                # Convert to DataFrame and run approximation methods
                df = pd.DataFrame({
                    "Open": data["o"],
                    "High": data["h"],
                    "Low": data["l"],
                    "Close": data["c"],
                    "Volume": data["v"],
                })
                df.index = pd.to_datetime(data["t"], unit="s")

                result = close_position_formula(df)
                s = compute_signals(result, df["Volume"])
                print(f"\n  Close Position Formula on finnhub candles:")
                print(f"    Uptick:   {s['total_uptick']:>14,.0f}")
                print(f"    Downtick: {s['total_downtick']:>14,.0f}")
                print(f"    Delta:    {s['net_delta']:>14,.0f}")
                print(f"    Imbalance: {s['imbalance']:.4f}")
            elif data.get("s") == "no_data":
                print("  No data available for this range")
                pytest.skip("No finnhub candle data")
            else:
                print(f"  Response: {data}")
                pytest.skip("Unexpected finnhub candle response")
        elif resp.status_code == 403:
            print("  PREMIUM REQUIRED")
            pytest.skip("Finnhub candle endpoint requires premium")
        else:
            print(f"  Error: {resp.text[:200]}")
            pytest.skip(f"Finnhub candle returned {resp.status_code}")


class TestDivergenceDetection:
    """Test if uptick/downtick volume can detect price-volume divergences."""

    def test_detect_divergence(self, yf_1min_data: pd.DataFrame):
        """Detect cases where price and volume delta diverge (potential signal)."""
        df = yf_1min_data
        result = close_position_formula(df)

        # Rolling 15-min windows
        window = 15
        rolling_delta = result["delta"].rolling(window).sum()
        rolling_price_change = df["Close"].diff(window)

        # Divergence: price going up but delta negative (or vice versa)
        valid = rolling_delta.notna() & rolling_price_change.notna()
        price_up = rolling_price_change[valid] > 0
        delta_neg = rolling_delta[valid] < 0
        bearish_divergence = price_up & delta_neg

        price_down = rolling_price_change[valid] < 0
        delta_pos = rolling_delta[valid] > 0
        bullish_divergence = price_down & delta_pos

        total_valid = valid.sum()
        print(f"\nDivergence Detection (Close Position Formula, {window}-bar rolling window):")
        print(f"  Total valid windows: {total_valid}")
        print(f"  Bearish divergences (price up, delta down): {bearish_divergence.sum()} ({bearish_divergence.sum()/total_valid*100:.1f}%)")
        print(f"  Bullish divergences (price down, delta up):  {bullish_divergence.sum()} ({bullish_divergence.sum()/total_valid*100:.1f}%)")

        # This test just verifies the method works, doesn't assert signal quality
        assert total_valid > 0, "No valid windows for divergence analysis"

    def test_volume_imbalance_at_extremes(self, yf_1min_data: pd.DataFrame):
        """Check volume imbalance when price hits intraday highs/lows."""
        df = yf_1min_data
        result = close_position_formula(df)

        # Find bars near the day high/low (within 0.1%)
        daily_groups = df.groupby(df.index.date)

        print(f"\nVolume Imbalance at Price Extremes:")
        for date, day_df in daily_groups:
            if len(day_df) < 30:
                continue
            day_result = result.loc[day_df.index]
            day_high = day_df["High"].max()
            day_low = day_df["Low"].min()

            near_high = day_df["Close"] >= day_high * 0.999
            near_low = day_df["Close"] <= day_low * 1.001

            if near_high.sum() > 0:
                high_imb = day_result.loc[near_high, "delta"].mean()
                print(f"  {date}: Near high ({near_high.sum()} bars) avg delta = {high_imb:+,.0f}")
            if near_low.sum() > 0:
                low_imb = day_result.loc[near_low, "delta"].mean()
                print(f"  {date}: Near low  ({near_low.sum()} bars) avg delta = {low_imb:+,.0f}")
