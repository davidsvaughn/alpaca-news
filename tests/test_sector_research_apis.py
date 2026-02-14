"""
Verification tests for untapped sector/industry research capabilities
across yfinance, finnhub, and schwabdev.

These are exploratory tests — they make real API calls to verify
what data is actually available for startup/sector research use cases.
"""

import os
import json
import pytest
import httpx

_FINNHUB_BASE = "https://finnhub.io/api/v1"


def _finnhub_get(path: str, params: dict | None = None) -> dict | list:
    """Simple Finnhub GET helper."""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        pytest.skip("FINNHUB_API_KEY not set")
    p = {"token": api_key}
    if params:
        p.update(params)
    r = httpx.get(f"{_FINNHUB_BASE}{path}", params=p, timeout=15)
    r.raise_for_status()
    return r.json()


# ─── yfinance: Sector & Industry research ───────────────────────────────

class TestYFinanceSectorResearch:
    """Test yfinance Sector/Industry classes for sector research."""

    def test_sector_overview(self):
        """Sector object: overview, industries, top companies."""
        import yfinance as yf

        sector = yf.Sector("technology")
        print(f"\n=== Sector: {sector.name} ===")
        print(f"  key: {sector.key}")
        print(f"  symbol: {sector.symbol}")

        overview = sector.overview
        print(f"\n  Overview type: {type(overview)}")
        if overview is not None:
            print(f"  Overview:\n{overview}")

        industries = sector.industries
        print(f"\n  Industries type: {type(industries)}")
        if industries is not None:
            print(f"  Industries ({len(industries)} rows):\n{industries.head(10)}")

        top = sector.top_companies
        print(f"\n  Top companies type: {type(top)}")
        if top is not None:
            print(f"  Top companies ({len(top)} rows):\n{top.head(10)}")

        assert sector.key == "technology"

    def test_sector_etfs_and_funds(self):
        """Sector object: top ETFs and mutual funds."""
        import yfinance as yf

        sector = yf.Sector("technology")

        etfs = sector.top_etfs
        print(f"\n=== Top ETFs for {sector.name} ===")
        print(f"  Type: {type(etfs)}")
        if etfs is not None:
            # May be dict or DataFrame
            if isinstance(etfs, dict):
                for k, v in list(etfs.items())[:10]:
                    print(f"  {k}: {v}")
            else:
                print(f"  {etfs}")

        funds = sector.top_mutual_funds
        print(f"\n=== Top Mutual Funds for {sector.name} ===")
        print(f"  Type: {type(funds)}")
        if funds is not None:
            if isinstance(funds, dict):
                for k, v in list(funds.items())[:10]:
                    print(f"  {k}: {v}")
            else:
                print(f"  {funds}")

    def test_sector_research_reports(self):
        """Sector object: research reports."""
        import yfinance as yf

        sector = yf.Sector("technology")

        reports = sector.research_reports
        print(f"\n=== Research Reports for {sector.name} ===")
        print(f"  Type: {type(reports)}")
        if reports is not None:
            if hasattr(reports, '__len__'):
                print(f"  Count: {len(reports)}")
                for r in (list(reports) if not isinstance(reports, list) else reports)[:3]:
                    print(f"  - {r}")

    def test_industry_details(self):
        """Industry object: top companies, growth companies."""
        import yfinance as yf

        industry = yf.Industry("software-infrastructure")
        print(f"\n=== Industry: {industry.name} ===")
        print(f"  key: {industry.key}")
        print(f"  sector_key: {industry.sector_key}")
        print(f"  sector_name: {industry.sector_name}")

        overview = industry.overview
        print(f"\n  Overview type: {type(overview)}")
        if overview is not None:
            print(f"  Overview:\n{overview}")

        top = industry.top_companies
        print(f"\n  Top companies type: {type(top)}")
        if top is not None:
            print(f"  Top companies ({len(top)} rows):\n{top.head(10)}")

        perf = industry.top_performing_companies
        print(f"\n  Top performing type: {type(perf)}")
        if perf is not None:
            print(f"  Top performing ({len(perf)} rows):\n{perf.head(10)}")

        growth = industry.top_growth_companies
        print(f"\n  Top growth type: {type(growth)}")
        if growth is not None:
            print(f"  Top growth ({len(growth)} rows):\n{growth.head(10)}")

        assert industry.key == "software-infrastructure"

    def test_multiple_sectors(self):
        """Verify we can query different sectors."""
        import yfinance as yf

        sector_keys = [
            "healthcare", "financial-services", "energy",
            "consumer-cyclical", "industrials"
        ]
        for key in sector_keys:
            sector = yf.Sector(key)
            industries = sector.industries
            count = len(industries) if industries is not None else 0
            top = sector.top_companies
            top_count = len(top) if top is not None else 0
            print(f"  {key}: {count} industries, {top_count} top companies")


# ─── Finnhub: Peers, Profile, Metrics (via httpx) ───────────────────────

class TestFinnhubSectorResearch:
    """Test Finnhub endpoints for sector/competitor research via httpx."""

    def test_company_peers_default(self):
        """Get peers by subIndustry (default grouping)."""
        peers = _finnhub_get("/stock/peers", {"symbol": "AAPL"})
        print(f"\n=== AAPL Peers (subIndustry) ===")
        print(f"  {peers}")
        assert isinstance(peers, list)
        assert len(peers) > 0

    def test_company_peers_by_sector(self):
        """Get peers by sector grouping (broader)."""
        peers = _finnhub_get("/stock/peers", {"symbol": "AAPL", "grouping": "sector"})
        print(f"\n=== AAPL Peers (sector) ===")
        print(f"  Count: {len(peers)}")
        print(f"  First 20: {peers[:20]}")
        assert isinstance(peers, list)
        assert len(peers) > 0

    def test_company_peers_by_industry(self):
        """Get peers by industry grouping."""
        peers = _finnhub_get("/stock/peers", {"symbol": "AAPL", "grouping": "industry"})
        print(f"\n=== AAPL Peers (industry) ===")
        print(f"  Count: {len(peers)}")
        print(f"  {peers}")
        assert isinstance(peers, list)

    def test_company_profile(self):
        """Get company profile with industry classification."""
        profile = _finnhub_get("/stock/profile2", {"symbol": "AAPL"})
        print(f"\n=== AAPL Profile ===")
        for k, v in profile.items():
            print(f"  {k}: {v}")
        assert "finnhubIndustry" in profile
        assert "marketCapitalization" in profile
        assert "name" in profile

    def test_basic_financials(self):
        """Get financial metrics and ratios."""
        metrics = _finnhub_get("/stock/metric", {"symbol": "AAPL", "metric": "all"})
        print(f"\n=== AAPL Basic Financials ===")

        if "metric" in metrics:
            m = metrics["metric"]
            print(f"  Metric keys ({len(m)}): {sorted(m.keys())[:30]}...")
            for key in ["peNormalizedAnnual", "peTTM", "pbAnnual",
                        "currentRatioAnnual", "netMarginTTM",
                        "roeTTM", "revenueGrowthTTMYoy",
                        "52WeekHigh", "52WeekLow", "beta"]:
                if key in m:
                    print(f"  {key}: {m[key]}")

        if "series" in metrics and "annual" in metrics.get("series", {}):
            annual = metrics["series"]["annual"]
            print(f"\n  Annual series keys: {sorted(annual.keys())[:20]}...")

        assert "metric" in metrics

    def test_insider_sentiment(self):
        """Get insider sentiment (MSPR score)."""
        sentiment = _finnhub_get("/stock/insider-sentiment", {
            "symbol": "TSLA", "from": "2024-01-01", "to": "2025-01-01"
        })
        print(f"\n=== TSLA Insider Sentiment ===")
        print(f"  Type: {type(sentiment)}")
        if isinstance(sentiment, dict) and "data" in sentiment:
            for entry in sentiment["data"][:5]:
                print(f"  {entry}")

    def test_peers_for_different_companies(self):
        """Get peers for companies in different sectors."""
        symbols = {
            "AAPL": "Technology",
            "JPM": "Financial Services",
            "JNJ": "Healthcare",
            "XOM": "Energy",
            "AMZN": "Consumer Cyclical",
        }
        print(f"\n=== Peers Across Sectors ===")
        for sym, sector in symbols.items():
            peers = _finnhub_get("/stock/peers", {"symbol": sym, "grouping": "industry"})
            profile = _finnhub_get("/stock/profile2", {"symbol": sym})
            industry = profile.get("finnhubIndustry", "?")
            print(f"  {sym} ({sector} / {industry}): {len(peers)} peers → {peers[:8]}")

    def test_industry_classification(self):
        """Finnhub market-wide industry list / sector classification."""
        # Market news by category
        news = _finnhub_get("/news", {"category": "technology"})
        print(f"\n=== Market News (technology category) ===")
        print(f"  Count: {len(news)}")
        for article in news[:3]:
            print(f"  - [{article.get('source')}] {article.get('headline', '')[:80]}")

    def test_supply_chain(self):
        """Finnhub supply chain relationships (free tier)."""
        data = _finnhub_get("/stock/supply-chain", {"symbol": "AAPL"})
        print(f"\n=== AAPL Supply Chain ===")
        print(f"  Type: {type(data)}")
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, list):
                    print(f"  {k}: {len(v)} items")
                    for item in v[:5]:
                        print(f"    - {item}")
                else:
                    print(f"  {k}: {v}")


# ─── Schwab: What else is available? ────────────────────────────────────

class TestSchwabSectorResearch:
    """Test Schwab endpoints for sector research (instruments/fundamentals)."""

    @pytest.fixture
    def client(self):
        """Get Schwab client if available."""
        try:
            from trader.market.schwab_client import SchwabMarketClient
            c = SchwabMarketClient()
            if not c.client:
                pytest.skip("Schwab client not initialized")
            return c
        except Exception as e:
            pytest.skip(f"Schwab client unavailable: {e}")

    def test_instruments_fundamentals(self, client):
        """Schwab instruments() returns sector/industry + fundamentals."""
        data = client.get_fundamentals("AAPL")
        print(f"\n=== AAPL Schwab Fundamentals ===")
        if data:
            for k, v in sorted(data.items()):
                print(f"  {k}: {v}")
        else:
            print("  No data returned")
