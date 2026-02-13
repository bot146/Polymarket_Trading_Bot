"""Quick diagnostic: check if any multi-outcome arb opportunities exist right now."""
import requests, json, time, sys
from collections import defaultdict
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor

def main():
    resp = requests.get(
        "https://gamma-api.polymarket.com/markets",
        params={"active": True, "closed": False, "limit": 10000},
        timeout=30,
    )
    markets = resp.json()
    print(f"Fetched {len(markets)} markets from Gamma")

    groups = defaultdict(list)
    for m in markets:
        nrid = m.get("negRiskMarketID")
        if nrid:
            groups[nrid].append(m)

    print(f"Found {len(groups)} negRisk groups")

    big_groups = {gid: bkts for gid, bkts in groups.items() if len(bkts) >= 3}
    print(f"Groups with >= 3 brackets: {len(big_groups)}")

    # Collect YES token IDs
    tids = []
    for gid, bkts in big_groups.items():
        for b in bkts:
            raw = b.get("clobTokenIds", "[]")
            try:
                ctids = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                ctids = []
            if ctids:
                tids.append(str(ctids[0]))

    print(f"Need CLOB books for {len(tids)} YES tokens")

    # Fetch CLOB books
    sess = requests.Session()
    clob: dict[str, float] = {}

    def fetch_ask(tid):
        try:
            r = sess.get("https://clob.polymarket.com/book", params={"token_id": tid}, timeout=5)
            r.raise_for_status()
            book = r.json()
            asks = book.get("asks", [])
            if asks:
                clob[tid] = float(asks[-1]["price"])
        except Exception:
            pass

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(fetch_ask, tids))
    print(f"CLOB fetch: {len(clob)}/{len(tids)} in {time.time()-t0:.1f}s")

    TAKER_FEE = Decimal("0.02")
    arb_count = 0
    near_misses = []
    skip_reasons: dict[str, int] = defaultdict(int)

    for gid, bkts in big_groups.items():
        yes_asks = []
        yes_mids = []
        valid = True
        for b in bkts:
            raw = b.get("clobTokenIds", "[]")
            try:
                ctids = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                ctids = []
            if not ctids:
                valid = False
                break
            tid = str(ctids[0])
            ask = clob.get(tid)
            prices_raw = b.get("outcomePrices", "[0.5,0.5]")
            try:
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                mid = float(prices[0]) if prices else 0.5
            except Exception:
                mid = 0.5
            if ask is None or ask <= 0:
                valid = False
                break
            yes_asks.append(Decimal(str(ask)))
            yes_mids.append(Decimal(str(mid)))

        if not valid:
            skip_reasons["invalid_token"] += 1
            continue

        sum_mid = sum(yes_mids)
        if sum_mid < Decimal("0.90") or sum_mid > Decimal("1.10"):
            skip_reasons[f"sum_mid_out_of_range"] += 1
            continue

        sum_ask = sum(yes_asks)
        total_fees = sum_ask * TAKER_FEE
        edge = Decimal("1") - sum_ask - total_fees
        edge_cents = edge * 100

        if edge_cents >= Decimal("0.5"):
            arb_count += 1
            q = bkts[0].get("groupItemTitle", bkts[0].get("question", ""))[:50]
            print(f"  ARB: {gid[:12]}... {len(bkts)} bkts, SUM(ask)={sum_ask:.4f}, edge={edge_cents:.2f}c  [{q}]")
        elif edge_cents > Decimal("-5"):
            near_misses.append((gid, len(bkts), float(sum_ask), float(edge_cents)))

    print()
    print(f"=== RESULT: {arb_count} arb opportunities ===")
    print(f"Near misses (edge > -5c): {len(near_misses)}")
    for gid, n, sa, ec in sorted(near_misses, key=lambda x: -x[3])[:10]:
        print(f"  {gid[:12]}... {n} bkts, SUM(ask)={sa:.4f}, edge={ec:.2f}c")
    print(f"Skip reasons: {dict(skip_reasons)}")


if __name__ == "__main__":
    main()
