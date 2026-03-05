"""Compare volume delta computation methods: bar-based (backtest) vs tick-level.

This test quantifies the approximation error inherent in the inter-bar tick rule
used by the backtest engine. It fetches recent 1-minute bars, computes volume delta
using multiple methods, and reports the differences.

Key question: When we move from backtesting (1-min bar approximation) to live trading
(tick-level streaming), how different will the signals be?

Run:
    uv run python tests/test_volume_delta_comparison.py [--symbols SPY AAPL TSLA]
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yfinance as yf


# ── Method 1: Inter-bar Tick Rule (what backtest uses) ────────────

def interbar_tick_rule(df: pd.DataFrame) -> pd.DataFrame:
    """Backtest method: entire bar volume classified by close-to-close direction."""
    close = df["Close"].to_numpy(dtype=float)
    volume = df["Volume"].to_numpy(dtype=float)
    n = len(close)
    direction = np.zeros(n, dtype=float)
    if n > 1:
        step = np.sign(np.diff(close))
        raw = np.empty(n, dtype=float)
        raw[0] = 0.0
        raw[1:] = step
        prev_nonzero = np.where(raw != 0.0, np.arange(n), 0)
        np.maximum.accumulate(prev_nonzero, out=prev_nonzero)
        direction = raw[prev_nonzero]
    uptick = np.where(direction > 0, volume, 0)
    downtick = np.where(direction < 0, volume, 0)
    return pd.DataFrame({
        "uptick_vol": uptick,
        "downtick_vol": downtick,
        "delta": uptick - downtick,
        "direction": direction,
    }, index=df.index)


# ── Method 2: Close Position Formula ─────────────────────────────

def close_position_formula(df: pd.DataFrame) -> pd.DataFrame:
    """Distributes bar volume based on where close falls in H-L range."""
    hl_range = df["High"] - df["Low"]
    safe_range = hl_range.replace(0, np.nan)
    buy_vol = df["Volume"] * (df["Close"] - df["Low"]) / safe_range
    sell_vol = df["Volume"] * (df["High"] - df["Close"]) / safe_range
    buy_vol = buy_vol.fillna(df["Volume"] / 2)
    sell_vol = sell_vol.fillna(df["Volume"] / 2)
    return pd.DataFrame({
        "uptick_vol": buy_vol,
        "downtick_vol": sell_vol,
        "delta": buy_vol - sell_vol,
    }, index=df.index)


# ── Method 3: Body Delta ─────────────────────────────────────────

def body_delta_method(df: pd.DataFrame) -> pd.DataFrame:
    """Uses open-to-close range within H-L range."""
    hl_range = (df["High"] - df["Low"]).replace(0, np.nan)
    delta = df["Volume"] * (df["Close"] - df["Open"]) / hl_range
    delta = delta.fillna(0)
    uptick = delta.clip(lower=0) + df["Volume"] / 2
    downtick = (-delta).clip(lower=0) + df["Volume"] / 2
    total = uptick + downtick
    uptick = uptick / total * df["Volume"]
    downtick = downtick / total * df["Volume"]
    return pd.DataFrame({
        "uptick_vol": uptick,
        "downtick_vol": downtick,
        "delta": uptick - downtick,
    }, index=df.index)


# ── VDD Signal Detection ─────────────────────────────────────────

def compute_vdd_signals(close: pd.Series, cum_delta: pd.Series, lookback: int = 30):
    """Detect Volume Delta Divergence signals."""
    if lookback <= 0 or close.empty:
        return pd.Series(False, index=close.index)
    prev_roll_max = close.shift(1).rolling(lookback, min_periods=lookback).max()
    lag_cum_delta = cum_delta.shift(lookback)
    mask = ((close >= prev_roll_max) & (cum_delta < lag_cum_delta)).fillna(False)
    return mask


# ── Volume Imbalance ─────────────────────────────────────────────

def compute_imbalance(uptick: pd.Series, downtick: pd.Series, window: int = 30):
    """Rolling volume imbalance ratio."""
    roll_up = uptick.rolling(window).sum()
    roll_dn = downtick.rolling(window).sum()
    total = roll_up + roll_dn
    return (roll_up - roll_dn) / total.replace(0, np.nan)


# ── Analysis ─────────────────────────────────────────────────────

def analyze_symbol(symbol: str, period: str = "5d") -> dict:
    """Full analysis of volume delta methods for a symbol."""
    print(f"\n{'='*80}")
    print(f"  ANALYSIS: {symbol} (period={period})")
    print(f"{'='*80}")

    # Fetch data
    df = yf.download(symbol, period=period, interval="1m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if len(df) < 30:
        print(f"  Insufficient data ({len(df)} bars)")
        return {}

    print(f"  Bars: {len(df)}")
    print(f"  Range: {df.index[0]} to {df.index[-1]}")
    print(f"  Price: {df['Close'].iloc[0]:.2f} -> {df['Close'].iloc[-1]:.2f}")

    # Compute all methods
    itk = interbar_tick_rule(df)
    cpf = close_position_formula(df)
    bd = body_delta_method(df)

    methods = {
        "Inter-bar Tick Rule": itk,
        "Close Position Formula": cpf,
        "Body Delta": bd,
    }

    # ── Aggregate comparison ──────────────────────────────────────
    print(f"\n  AGGREGATE VOLUME DELTA COMPARISON:")
    print(f"  {'Method':<28} {'Uptick':>14} {'Downtick':>14} {'Net Delta':>14} {'Imbalance':>10}")
    print(f"  {'-'*82}")

    results = {}
    for name, m in methods.items():
        up = m["uptick_vol"].sum()
        dn = m["downtick_vol"].sum()
        net = up - dn
        imb = net / (up + dn) if (up + dn) > 0 else 0
        direction = "BULL" if net > 0 else "BEAR" if net < 0 else "FLAT"
        print(f"  {name:<28} {up:>14,.0f} {dn:>14,.0f} {net:>14,.0f} {imb:>10.4f}  [{direction}]")
        results[name] = {"uptick": up, "downtick": dn, "net": net, "imbalance": imb, "direction": direction}

    # ── Per-bar direction agreement ───────────────────────────────
    print(f"\n  PER-BAR DIRECTION AGREEMENT (inter-bar tick vs close-position):")

    itk_dir = np.sign(itk["delta"])
    cpf_dir = np.sign(cpf["delta"])
    bd_dir = np.sign(bd["delta"])

    agree_itk_cpf = (itk_dir == cpf_dir).sum()
    agree_itk_bd = (itk_dir == bd_dir).sum()
    agree_cpf_bd = (cpf_dir == bd_dir).sum()
    n_bars = len(df)

    print(f"  Inter-bar vs Close-Position: {agree_itk_cpf}/{n_bars} = {agree_itk_cpf/n_bars*100:.1f}%")
    print(f"  Inter-bar vs Body-Delta:     {agree_itk_bd}/{n_bars} = {agree_itk_bd/n_bars*100:.1f}%")
    print(f"  Close-Position vs Body:      {agree_cpf_bd}/{n_bars} = {agree_cpf_bd/n_bars*100:.1f}%")

    # ── VDD Signal comparison ─────────────────────────────────────
    lookback = 30
    print(f"\n  VDD SIGNAL COMPARISON (lookback={lookback}):")

    for name, m in methods.items():
        cum_delta = m["delta"].cumsum()
        signals = compute_vdd_signals(df["Close"], cum_delta, lookback)
        n_signals = signals.sum()
        print(f"  {name:<28} {n_signals} VDD signals")

    # ── Check where methods DISAGREE on VDD ───────────────────────
    itk_cum = itk["delta"].cumsum()
    cpf_cum = cpf["delta"].cumsum()
    itk_vdd = compute_vdd_signals(df["Close"], itk_cum, lookback)
    cpf_vdd = compute_vdd_signals(df["Close"], cpf_cum, lookback)

    only_itk = itk_vdd & ~cpf_vdd
    only_cpf = cpf_vdd & ~itk_vdd
    both = itk_vdd & cpf_vdd

    print(f"\n  VDD signal overlap (inter-bar vs close-position):")
    print(f"    Both agree:          {both.sum()}")
    print(f"    Only inter-bar:      {only_itk.sum()}")
    print(f"    Only close-position: {only_cpf.sum()}")

    # ── Rolling imbalance comparison ──────────────────────────────
    window = 30
    itk_imb = compute_imbalance(itk["uptick_vol"], itk["downtick_vol"], window)
    cpf_imb = compute_imbalance(cpf["uptick_vol"], cpf["downtick_vol"], window)

    valid = itk_imb.notna() & cpf_imb.notna()
    if valid.sum() > 0:
        corr = itk_imb[valid].corr(cpf_imb[valid])
        mae = (itk_imb[valid] - cpf_imb[valid]).abs().mean()
        dir_agree = (np.sign(itk_imb[valid]) == np.sign(cpf_imb[valid])).mean()

        print(f"\n  ROLLING IMBALANCE COMPARISON (window={window}):")
        print(f"    Correlation: {corr:.4f}")
        print(f"    Mean Absolute Error: {mae:.4f}")
        print(f"    Direction agreement: {dir_agree*100:.1f}%")

    # ── Daily breakdown ───────────────────────────────────────────
    print(f"\n  DAILY BREAKDOWN (inter-bar tick rule):")
    df_with_delta = df.copy()
    df_with_delta["delta"] = itk["delta"]
    df_with_delta["uptick"] = itk["uptick_vol"]
    df_with_delta["downtick"] = itk["downtick_vol"]
    df_with_delta["date"] = df_with_delta.index.date

    daily = df_with_delta.groupby("date").agg(
        bars=("Close", "count"),
        open=("Open", "first"),
        close=("Close", "last"),
        volume=("Volume", "sum"),
        uptick=("uptick", "sum"),
        downtick=("downtick", "sum"),
    )
    daily["net_delta"] = daily["uptick"] - daily["downtick"]
    daily["imbalance"] = daily["net_delta"] / (daily["uptick"] + daily["downtick"])
    daily["price_pct"] = (daily["close"] - daily["open"]) / daily["open"] * 100
    daily["direction"] = daily["net_delta"].apply(lambda x: "BULL" if x > 0 else "BEAR" if x < 0 else "FLAT")

    print(f"  {'Date':<12} {'Bars':>5} {'Volume':>14} {'Net Delta':>14} {'Imbalance':>10} {'Price %':>8} {'Dir':<6}")
    print(f"  {'-'*75}")
    for date, row in daily.iterrows():
        print(
            f"  {str(date):<12} {row['bars']:>5} {row['volume']:>14,.0f} "
            f"{row['net_delta']:>14,.0f} {row['imbalance']:>10.4f} "
            f"{row['price_pct']:>+7.2f}% {row['direction']:<6}"
        )

    # ── Key insight: inter-bar tick rule binary classification ────
    print(f"\n  BINARY CLASSIFICATION ANALYSIS:")
    # What fraction of bars does the inter-bar rule "flip" direction vs prior bar?
    dir_changes = (itk["direction"].diff().abs() > 0).sum()
    total_bars = len(itk)
    print(f"  Direction changes between consecutive bars: {dir_changes}/{total_bars} = {dir_changes/total_bars*100:.1f}%")

    # What's the avg volume per bar when direction matches vs disagrees with CPF?
    agree_mask = itk_dir == cpf_dir
    disagree_mask = ~agree_mask
    if agree_mask.sum() > 0:
        avg_vol_agree = df["Volume"][agree_mask].mean()
        avg_vol_disagree = df["Volume"][disagree_mask].mean() if disagree_mask.sum() > 0 else 0
        print(f"  Avg volume on agreement bars:    {avg_vol_agree:,.0f}")
        print(f"  Avg volume on disagreement bars: {avg_vol_disagree:,.0f}")
        print(f"  (higher disagreement volume = bigger approximation error)")

    return {
        "symbol": symbol,
        "bars": n_bars,
        "methods": results,
        "vdd_overlap": {"both": int(both.sum()), "only_itk": int(only_itk.sum()), "only_cpf": int(only_cpf.sum())},
    }


def analyze_vdd_timing_sensitivity(symbol: str):
    """Analyze how sensitive VDD signals are to the volume delta computation method.

    This is the KEY question: if real-time tick-level data gives us a different
    cumulative delta curve than the bar-based approximation, will VDD signals
    fire at different times?
    """
    print(f"\n{'='*80}")
    print(f"  VDD TIMING SENSITIVITY: {symbol}")
    print(f"{'='*80}")

    df = yf.download(symbol, period="5d", interval="1m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if len(df) < 60:
        print(f"  Insufficient data")
        return

    lookback = 30

    # Compute VDD with different methods
    itk = interbar_tick_rule(df)
    cpf = close_position_formula(df)

    itk_cum = itk["delta"].cumsum()
    cpf_cum = cpf["delta"].cumsum()

    itk_vdd = compute_vdd_signals(df["Close"], itk_cum, lookback)
    cpf_vdd = compute_vdd_signals(df["Close"], cpf_cum, lookback)

    # For each VDD signal from inter-bar, find closest CPF signal
    itk_signal_idxs = np.flatnonzero(itk_vdd.values)
    cpf_signal_idxs = np.flatnonzero(cpf_vdd.values)

    print(f"  Inter-bar VDD signals: {len(itk_signal_idxs)}")
    print(f"  Close-position VDD signals: {len(cpf_signal_idxs)}")

    if len(itk_signal_idxs) > 0 and len(cpf_signal_idxs) > 0:
        # For each ITK signal, find nearest CPF signal
        timing_diffs = []
        for idx in itk_signal_idxs:
            diffs = np.abs(cpf_signal_idxs - idx)
            nearest = cpf_signal_idxs[np.argmin(diffs)]
            timing_diffs.append(nearest - idx)

        timing_diffs = np.array(timing_diffs)
        print(f"\n  Timing difference (bars) from inter-bar to nearest close-position signal:")
        print(f"    Mean: {np.mean(timing_diffs):.1f} bars")
        print(f"    Median: {np.median(timing_diffs):.0f} bars")
        print(f"    Std: {np.std(timing_diffs):.1f} bars")
        print(f"    Max early: {np.min(timing_diffs)} bars")
        print(f"    Max late: {np.max(timing_diffs)} bars")
        print(f"    Within ±5 bars: {np.sum(np.abs(timing_diffs) <= 5)}/{len(timing_diffs)}")
        print(f"    Within ±15 bars: {np.sum(np.abs(timing_diffs) <= 15)}/{len(timing_diffs)}")

        # Show a few examples
        if len(itk_signal_idxs) > 0:
            print(f"\n  Signal examples (first 5):")
            print(f"  {'ITK Bar#':>10} {'ITK Time':<20} {'CPF Bar#':>10} {'CPF Time':<20} {'Diff':>6}")
            print(f"  {'-'*70}")
            for i, (itk_idx, diff) in enumerate(zip(itk_signal_idxs[:5], timing_diffs[:5])):
                cpf_idx = itk_idx + diff
                itk_time = df.index[itk_idx] if itk_idx < len(df) else "?"
                cpf_time = df.index[cpf_idx] if 0 <= cpf_idx < len(df) else "?"
                print(f"  {itk_idx:>10} {str(itk_time):<20} {cpf_idx:>10} {str(cpf_time):<20} {diff:>+6}")

    # ── Cumulative delta curve divergence ─────────────────────────
    print(f"\n  CUMULATIVE DELTA CURVE DIVERGENCE:")
    corr = itk_cum.corr(cpf_cum)
    print(f"    Correlation: {corr:.6f}")

    # Normalize to same scale for comparison
    itk_norm = itk_cum / itk_cum.abs().max() if itk_cum.abs().max() > 0 else itk_cum
    cpf_norm = cpf_cum / cpf_cum.abs().max() if cpf_cum.abs().max() > 0 else cpf_cum
    mae = (itk_norm - cpf_norm).abs().mean()
    print(f"    Normalized MAE: {mae:.4f}")
    print(f"    (0 = identical curves, 1 = maximally different)")


def main():
    parser = argparse.ArgumentParser(description="Volume delta method comparison")
    parser.add_argument("--symbols", nargs="+", default=["SPY", "AAPL", "TSLA", "NVDA"],
                        help="Symbols to analyze")
    parser.add_argument("--period", default="5d", help="Data period (default: 5d)")
    args = parser.parse_args()

    print("=" * 80)
    print("  VOLUME DELTA: BACKTEST vs REAL-TIME ANALYSIS")
    print("  Comparing inter-bar tick rule (backtest) against other methods")
    print("=" * 80)
    print(f"\n  The backtest uses the INTER-BAR TICK RULE:")
    print(f"    - Each bar's ENTIRE volume is classified as uptick or downtick")
    print(f"    - Classification based on close > or < previous bar's close")
    print(f"    - This is a BINARY approximation (all-or-nothing per bar)")
    print(f"\n  In REAL-TIME with tick-level data:")
    print(f"    - Each price update classifies its volume increment")
    print(f"    - Multiple direction changes within a single minute are captured")
    print(f"    - More granular, but fundamentally the same tick rule principle")
    print(f"\n  Close Position Formula provides a better approximation:")
    print(f"    - Uses the bar's OHLC to estimate intra-bar distribution")
    print(f"    - Closer to what tick-level data would show")
    print(f"    - Useful as proxy for 'truth' when tick data unavailable")

    all_results = []
    for symbol in args.symbols:
        result = analyze_symbol(symbol, args.period)
        if result:
            all_results.append(result)

    # VDD timing sensitivity
    for symbol in args.symbols[:2]:  # First 2 symbols only
        analyze_vdd_timing_sensitivity(symbol)

    # ── Summary ───────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  SUMMARY: KEY FINDINGS FOR LIVE TRADING")
    print(f"{'='*80}")
    print(f"""
  1. BINARY CLASSIFICATION ERROR:
     The inter-bar tick rule assigns ALL volume per bar as uptick OR downtick.
     In reality, a 1-minute bar may contain both buying and selling volume.
     The close-position formula provides a continuous distribution.

  2. VDD SIGNAL TIMING:
     Different methods may fire VDD signals at slightly different times.
     Check the timing sensitivity analysis above for exact bar differences.

  3. ROLLING IMBALANCE DIRECTION:
     Despite different absolute values, the DIRECTION of rolling imbalance
     tends to agree across methods (check correlation above).

  4. IMPLICATIONS FOR LIVE TRADING:
     - Real-time tick data will give us MORE ACCURATE volume delta
     - VDD signals may fire EARLIER with tick-level data (more responsive)
     - We should calibrate thresholds using tick-level data, not bar data
     - The guards (stop loss, take profit) are price-based and unaffected
     - The lookback parameter may need tuning for tick-level precision

  5. RECOMMENDED APPROACH:
     - Start with the SAME inter-bar tick rule on 1-min bars (parity with backtest)
     - Add tick-level accumulation in PARALLEL (shadow mode)
     - Compare signals over time to measure improvement
     - Switch to tick-level when we have confidence in the calibration
""")


if __name__ == "__main__":
    main()
