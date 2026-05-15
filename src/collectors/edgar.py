"""SEC EDGAR Form 4 collector — insider buys and sales per ticker.

Form 4 (Statement of Changes in Beneficial Ownership) is filed by company
insiders within 2 business days of any transaction. Only two transaction
codes carry directional signal:

  P — Open-market purchase (insider voluntarily buying with their own cash)
  S — Open-market sale     (voluntary sell, weaker signal — could be tax
                            planning, diversification, or honest bearishness)

Codes we IGNORE because they're not voluntary directional bets:
  A — Grant/award (RSU vest, stock award)
  M — Options exercise
  F — Tax withholding on vest
  G — Gift

SEC requires a User-Agent with contact email and a 10 req/sec rate limit;
we space requests at ~120ms.

This collector is intentionally read-mostly: it caches the ~600KB ticker→CIK
mapping at module import. Per-ticker work fetches one submissions JSON +
one Form 4 XML per recent filing.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)


CONTACT_EMAIL = os.getenv("SEC_CONTACT_EMAIL", "quinnstone99@gmail.com")
USER_AGENT = f"MiniHedgeFund/1.0 ({CONTACT_EMAIL})"

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_clean}"
REQUEST_DELAY_S = 0.13   # ~7.5 req/s, under SEC's 10/s limit

# Form 4 transaction codes that represent real directional signal
DIRECTIONAL_CODES = {"P", "S"}


@dataclass
class InsiderTransaction:
    ticker: str
    filer_name: str
    relationship: str           # e.g. "CFO", "10% Owner", "Director"
    transaction_code: str       # "P" or "S"
    direction: str              # "buy" or "sell"
    shares: float
    price_per_share: float
    value_usd: float
    transaction_date: date
    filing_date: date
    accession: str


@dataclass
class InsiderActivity:
    """Aggregated insider activity for one ticker over a window."""
    ticker: str
    buy_count: int = 0
    sell_count: int = 0
    distinct_buyers: int = 0
    distinct_sellers: int = 0
    buy_value_usd: float = 0.0
    sell_value_usd: float = 0.0
    net_value_usd: float = 0.0
    cluster_buy: bool = False           # ≥3 distinct buyers within 7 days
    transactions: list[InsiderTransaction] = field(default_factory=list)

    @property
    def composite_score(self) -> float:
        """0–100. 50 = no activity. Buys push up, sells push down. Cluster buys
        get a meaningful boost because they're rare and high-signal."""
        score = 50.0
        net = self.buy_value_usd - self.sell_value_usd
        # ±$1M caps the linear range
        capped = max(-1_000_000, min(1_000_000, net))
        score += capped / 1_000_000 * 25.0
        if self.cluster_buy:
            score += 15.0
        return round(max(0.0, min(100.0, score)), 1)


# ─── module-level CIK cache ─────────────────────────────────────────────

_ticker_to_cik: Optional[dict[str, str]] = None


def _load_ticker_cik_map(session: requests.Session) -> dict[str, str]:
    """Pulls the master ticker→CIK file from SEC. Result is ~10k entries."""
    global _ticker_to_cik
    if _ticker_to_cik is not None:
        return _ticker_to_cik
    try:
        resp = session.get(TICKERS_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("SEC ticker map fetch failed: %s", exc)
        return {}

    mapping: dict[str, str] = {}
    # Schema: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    for v in data.values():
        ticker = v.get("ticker", "").upper()
        cik_int = v.get("cik_str")
        if ticker and cik_int is not None:
            mapping[ticker] = str(cik_int).zfill(10)  # SEC wants zero-padded
    _ticker_to_cik = mapping
    return mapping


class EdgarCollector:
    def __init__(self, lookback_days: int = 7, contact_email: Optional[str] = None):
        self.lookback_days = lookback_days
        ua = f"MiniHedgeFund/1.0 ({contact_email or CONTACT_EMAIL})"
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": ua,
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
        })

    # ───────── public API ─────────

    def get_insider_activity(self, ticker: str) -> InsiderActivity:
        """Recent insider transactions aggregated for one ticker. Returns
        InsiderActivity with zero counts when there's no data."""
        cik_map = _load_ticker_cik_map(self._session)
        cik = cik_map.get(ticker.upper())
        if cik is None:
            logger.debug("no CIK for %s — likely ETF or non-public", ticker)
            return InsiderActivity(ticker=ticker.upper())

        recent_filings = self._recent_form4_accessions(cik)
        transactions: list[InsiderTransaction] = []
        for accession, filing_date in recent_filings:
            time.sleep(REQUEST_DELAY_S)
            transactions.extend(self._parse_form4(ticker, cik, accession, filing_date))

        return self._aggregate(ticker, transactions)

    def get_multiple(self, tickers: list[str]) -> dict[str, InsiderActivity]:
        out: dict[str, InsiderActivity] = {}
        for t in tickers:
            out[t.upper()] = self.get_insider_activity(t)
        return out

    # ───────── internal ─────────

    def _recent_form4_accessions(self, cik: str) -> list[tuple[str, date]]:
        """List of (accession_number, filing_date) for Form 4s within lookback."""
        url = SUBMISSIONS_URL.format(cik=cik)
        time.sleep(REQUEST_DELAY_S)
        try:
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("EDGAR submissions fetch failed for CIK %s: %s", cik, exc)
            return []

        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        filing_dates = recent.get("filingDate") or []

        cutoff = date.today() - timedelta(days=self.lookback_days)
        out: list[tuple[str, date]] = []
        for form, acc, fd in zip(forms, accessions, filing_dates):
            if form != "4":
                continue
            try:
                fd_parsed = date.fromisoformat(fd)
            except ValueError:
                continue
            if fd_parsed < cutoff:
                continue
            out.append((acc, fd_parsed))
        return out

    def _parse_form4(
        self, ticker: str, cik: str, accession: str, filing_date: date,
    ) -> list[InsiderTransaction]:
        """Fetch and parse the Form 4 XML. The XML filename is the accession
        with hyphens removed inside the archive directory."""
        accession_clean = accession.replace("-", "")
        cik_int = str(int(cik))   # archive URL wants un-padded CIK
        index_url = ARCHIVE_URL.format(cik_int=cik_int, accession_clean=accession_clean)

        # First find the actual XML attachment via the filing index
        try:
            idx_resp = self._session.get(f"{index_url}/", timeout=10)
            idx_resp.raise_for_status()
        except Exception as exc:
            logger.debug("Form 4 index fetch failed (%s/%s): %s", cik, accession, exc)
            return []

        xml_filename = self._find_form4_xml(idx_resp.text)
        if not xml_filename:
            return []

        xml_url = f"{index_url}/{xml_filename}"
        time.sleep(REQUEST_DELAY_S)
        try:
            xml_resp = self._session.get(xml_url, timeout=10)
            xml_resp.raise_for_status()
        except Exception as exc:
            logger.debug("Form 4 XML fetch failed (%s): %s", xml_url, exc)
            return []

        return self._extract_transactions(ticker, xml_resp.text, filing_date)

    @staticmethod
    def _find_form4_xml(index_html: str) -> Optional[str]:
        """Pick the primary Form 4 .xml filename from the filing index page."""
        # SEC indexes list filename like 'xslF345X05/wf-form4_173...xml' or 'doc4.xml'
        matches = re.findall(r'href="([^"]+\.xml)"', index_html, re.IGNORECASE)
        for m in matches:
            # Skip XBRL exhibits and the metadata-index xml
            if "xslF" in m or "primary_doc" in m:
                continue
            return m.split("/")[-1]
        return matches[0].split("/")[-1] if matches else None

    def _extract_transactions(
        self, ticker: str, xml_text: str, filing_date: date,
    ) -> list[InsiderTransaction]:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(xml_text, "lxml-xml")
        except Exception:
            soup = BeautifulSoup(xml_text, "xml")

        # Reporting owner
        rpt_owner = soup.find("reportingOwner")
        filer_name = ""
        relationship_bits = []
        if rpt_owner is not None:
            name_el = rpt_owner.find("rptOwnerName")
            if name_el is not None:
                filer_name = name_el.get_text(strip=True)
            rel = rpt_owner.find("reportingOwnerRelationship")
            if rel is not None:
                if (rel.find("isDirector") or BeautifulSoup("", "lxml-xml")).get_text(strip=True) in {"1", "true"}:
                    relationship_bits.append("Director")
                officer_title_el = rel.find("officerTitle")
                if officer_title_el is not None and officer_title_el.get_text(strip=True):
                    relationship_bits.append(officer_title_el.get_text(strip=True))
                if (rel.find("isTenPercentOwner") or BeautifulSoup("", "lxml-xml")).get_text(strip=True) in {"1", "true"}:
                    relationship_bits.append("10% Owner")
        relationship = ", ".join(relationship_bits) or "Insider"

        out: list[InsiderTransaction] = []
        for txn in soup.find_all("nonDerivativeTransaction"):
            code_el = txn.find("transactionCode")
            if code_el is None:
                continue
            code = code_el.get_text(strip=True)
            if code not in DIRECTIONAL_CODES:
                continue

            shares_el = txn.find("transactionShares")
            price_el = txn.find("transactionPricePerShare")
            ad_el = txn.find("transactionAcquiredDisposedCode")
            date_el = txn.find("transactionDate")

            try:
                shares = float((shares_el.find("value").get_text() if shares_el else 0) or 0)
                price = float((price_el.find("value").get_text() if price_el else 0) or 0)
                ad = (ad_el.find("value").get_text() if ad_el else "") or ""
                txn_date = date.fromisoformat(
                    date_el.find("value").get_text().strip()
                ) if date_el else filing_date
            except (AttributeError, ValueError):
                continue

            direction = "buy" if ad == "A" else "sell"
            out.append(InsiderTransaction(
                ticker=ticker.upper(),
                filer_name=filer_name,
                relationship=relationship,
                transaction_code=code,
                direction=direction,
                shares=shares,
                price_per_share=price,
                value_usd=shares * price,
                transaction_date=txn_date,
                filing_date=filing_date,
                accession=txn.get("accession", "") if hasattr(txn, "get") else "",
            ))
        return out

    @staticmethod
    def _aggregate(ticker: str, transactions: list[InsiderTransaction]) -> InsiderActivity:
        activity = InsiderActivity(ticker=ticker.upper(), transactions=transactions)
        buyers: set[str] = set()
        sellers: set[str] = set()
        for t in transactions:
            if t.direction == "buy":
                activity.buy_count += 1
                activity.buy_value_usd += t.value_usd
                buyers.add(t.filer_name)
            else:
                activity.sell_count += 1
                activity.sell_value_usd += t.value_usd
                sellers.add(t.filer_name)
        activity.distinct_buyers = len(buyers)
        activity.distinct_sellers = len(sellers)
        activity.net_value_usd = activity.buy_value_usd - activity.sell_value_usd
        activity.cluster_buy = activity.distinct_buyers >= 3
        return activity
