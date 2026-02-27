"""Discover all short-duration / recurring market series on Polymarket.

Scans the Gamma API for markets with:
- Series slugs containing recurrence patterns (5m, 15m, 30m, 1h)
- eventStartTime set (marks recurring events)
- Short-duration question patterns (up/down, over/under)
- Non-standard fee types (crypto_15_min, sports_fees, etc.)

Usage:  .venv\\Scripts\\python.exe diag_short_series.py
"""

import json
import requests
from collections import Counter, defaultdict
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"


def fetch_all_recent(limit: int = 1000) -> list[dict]:
    """Fetch recent active markets sorted by creation time."""
    resp = requests.get(f"{GAMMA}/markets", params={
        "limit": limit,
        "active": True,
        "closed": False,
        "order": "createdAt",
        "ascending": False,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def classify_market(m: dict) -> dict | None:
    """Return classification dict if market is short-duration, else None."""
    series = m.get("seriesSlug") or ""
    question = m.get("question", "").lower()
    fee_type = m.get("feeType") or ""
    recurrence = m.get("recurrence") or ""
    event_start = m.get("eventStartTime") or ""
    end_date = m.get("endDate") or ""
    liquidity = float(m.get("liquidity") or 0)
    volume = float(m.get("volume") or 0)

    reasons = []

    # Recurrence suffix in series slug
    if any(pat in series for pat in ["-5m", "-15m", "-30m", "-1h", "-2h"]):
        reasons.append(f"series_recurrence={series}")
    
    # Explicit recurrence field
    if recurrence and recurrence != "0":
        reasons.append(f"recurrence={recurrence}")

    # Question patterns
    short_patterns = ["up or down", "over or under", "5-minute", "15-minute", "30-minute"]
    for pat in short_patterns:
        if pat in question:
            reasons.append(f"question_pattern='{pat}'")
            break

    # Non-standard fee type
    if fee_type and fee_type not in ("", "standard"):
        reasons.append(f"fee_type={fee_type}")

    # Has eventStartTime (marks scheduled recurring events)
    if event_start:
        reasons.append("has_event_start_time")

    if not reasons:
        return None

    # Compute hours to resolution
    hours_to_res = None
    if end_date:
        try:
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            hours_to_res = (dt - datetime.now(timezone.utc)).total_seconds() / 3600.0
        except Exception:
            pass

    return {
        "condition_id": m.get("conditionId", "")[:16],
        "question": m.get("question", ""),
        "series_slug": series,
        "recurrence": recurrence,
        "fee_type": fee_type,
        "event_start_time": event_start,
        "end_date": end_date,
        "hours_to_resolution": round(hours_to_res, 2) if hours_to_res is not None else None,
        "liquidity": liquidity,
        "volume": volume,
        "outcomes": m.get("outcomes"),
        "reasons": reasons,
    }


def main():
    print("=" * 80)
    print("SHORT-DURATION MARKET SERIES DISCOVERY")
    print("=" * 80)

    markets = fetch_all_recent(limit=1000)
    print(f"\nFetched {len(markets)} recent active markets\n")

    classified = []
    for m in markets:
        info = classify_market(m)
        if info:
            classified.append(info)

    if not classified:
        print("No short-duration markets found!")
        return

    # Group by category (auto-detect from question + fee type)
    by_category = defaultdict(list)
    for c in classified:
        question = c["question"].lower()
        fee = c["fee_type"]
        
        # Classify into meaningful buckets
        if any(kw in question for kw in ["bitcoin", "btc"]) and "up or down" in question:
            cat = "Crypto: BTC Up/Down"
        elif any(kw in question for kw in ["ethereum", "eth"]) and "up or down" in question:
            cat = "Crypto: ETH Up/Down"
        elif any(kw in question for kw in ["solana", "sol"]) and "up or down" in question:
            cat = "Crypto: SOL Up/Down"
        elif any(kw in question for kw in ["xrp", "ripple"]) and "up or down" in question:
            cat = "Crypto: XRP Up/Down"
        elif "up or down" in question:
            cat = f"Crypto: Other Up/Down ({fee})"
        elif "o/u" in question or "over or under" in question:
            cat = f"Sports: Over/Under ({fee})"
        elif fee == "sports_fees":
            cat = f"Sports: Other ({fee})"
        elif fee == "crypto_15_min":
            cat = f"Crypto: Other ({fee})"
        else:
            cat = f"Other ({fee or 'standard'})"
        by_category[cat].append(c)

    print(f"Found {len(classified)} short-duration markets in {len(by_category)} categories:\n")

    for cat, items in sorted(by_category.items(), key=lambda x: -len(x[1])):
        sample = items[0]
        live_count = sum(1 for i in items if i["hours_to_resolution"] is not None and i["hours_to_resolution"] > 0)
        expired_count = sum(1 for i in items if i["hours_to_resolution"] is not None and i["hours_to_resolution"] <= 0)

        print(f"📊 {cat}")
        print(f"   Count: {len(items)} markets (live={live_count}, expired={expired_count})")
        print(f"   Fee type: {sample['fee_type']}")
        print(f"   Recurrence: {sample['recurrence'] or 'none'}")
        print(f"   Outcomes: {sample['outcomes']}")
        print(f"   Detection: {', '.join(sample['reasons'])}")

        # Show liquidity range
        liqs = [i["liquidity"] for i in items]
        if liqs:
            print(f"   Liquidity range: ${min(liqs):,.0f} - ${max(liqs):,.0f}")

        # Show volume range  
        vols = [i["volume"] for i in items]
        if vols:
            print(f"   Volume range: ${min(vols):,.0f} - ${max(vols):,.0f}")

        # Show sample questions
        questions = sorted(set(i["question"] for i in items))
        for q in questions[:3]:
            print(f"   Sample: {q}")
        if len(questions) > 3:
            print(f"   ... and {len(questions) - 3} more")

        # Show upcoming resolution times
        upcoming = sorted(
            [i for i in items if i["hours_to_resolution"] is not None and i["hours_to_resolution"] > 0],
            key=lambda x: x["hours_to_resolution"],
        )
        if upcoming:
            next_res = upcoming[0]
            print(f"   Next resolution: {next_res['hours_to_resolution']:.1f}h — {next_res['question'][:60]}")
        
        print()

    # Summary of fee types
    fee_types = Counter(c["fee_type"] for c in classified if c["fee_type"])
    if fee_types:
        print("─" * 60)
        print("FEE TYPE SUMMARY:")
        for ft, cnt in fee_types.most_common():
            print(f"  {ft}: {cnt} markets")

    # Summary of all categories
    print("\n─" * 60)
    print("ALL CATEGORIES:")
    for cat, items in sorted(by_category.items(), key=lambda x: x[0]):
        live_count = sum(1 for i in items if i["hours_to_resolution"] is not None and i["hours_to_resolution"] > 0)
        print(f"  {cat}: {len(items)} total, {live_count} live")


if __name__ == "__main__":
    main()
