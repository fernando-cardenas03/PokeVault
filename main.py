"""
PokéVault — Pokémon ETB & Booster Box Value Tracker
Data source: TCGCSV.com (free, no API key, updated daily from TCGPlayer)

How it works:
  1. On startup, fetch all Pokémon set groups from tcgcsv.com
  2. For each group, fetch products + prices and keep only ETBs & Booster Boxes
  3. Join products to prices by productId
  4. Cache everything in memory — refresh once per day
  5. All searches are instant local queries — zero external calls per search

No API key needed. No rate limits. $0 cost.
"""

import os
import time
import asyncio
import re
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import uvicorn

# ── Config ────────────────────────────────────────────────────────────────────
TCGCSV_BASE  = "https://tcgcsv.com/tcgplayer"
POKEMON_CAT  = 3          # TCGPlayer categoryId for Pokémon
REFRESH_SECS = 86400      # Re-download data once per day
MAX_PARALLEL = 8          # Max concurrent group fetches

# Keywords that identify a product as an ETB or Booster Box
ETB_KEYWORDS     = ["elite trainer", " etb"]
BOX_KEYWORDS     = ["booster box", "booster display", "36 pack", "36-pack", "display box"]
EXCLUDE_KEYWORDS = ["single", "lot", "bundle", "code", "sleeve", "binder",
                    "deck", "tin", "blister", "mini", "collection box",
                    "figure", "pin", "coin", "repack", "opened", "empty"]

# ── In-memory store ───────────────────────────────────────────────────────────
store = {
    "products":      [],   # list of normalised product dicts
    "last_refresh":  0,
    "loading":       False,
    "load_error":    None,
    "groups_loaded": 0,
    "groups_total":  0,
}


# ── Data loading ──────────────────────────────────────────────────────────────

async def fetch_json(client: httpx.AsyncClient, url: str) -> dict | list | None:
    try:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] Failed to fetch {url}: {e}")
        return None


async def load_group(client: httpx.AsyncClient, group: dict) -> list[dict]:
    """Fetch products + prices for one set group, return matching ETBs/Boxes."""
    gid   = group["groupId"]
    gname = group.get("name", f"Group {gid}")

    products_data = await fetch_json(client, f"{TCGCSV_BASE}/{POKEMON_CAT}/{gid}/products")
    prices_data   = await fetch_json(client, f"{TCGCSV_BASE}/{POKEMON_CAT}/{gid}/prices")

    if not products_data or not prices_data:
        return []

    # Build price lookup: productId → price info
    price_map: dict[int, dict] = {}
    prices_list = prices_data if isinstance(prices_data, list) else prices_data.get("results", [])
    for p in prices_list:
        pid = p.get("productId")
        if pid:
            # Keep the "Normal" printing row (vs Holofoil etc.) when available
            existing = price_map.get(pid)
            if existing is None or p.get("subTypeName") == "Normal":
                price_map[pid] = p

    # Filter products to ETBs and Booster Boxes
    products_list = products_data if isinstance(products_data, list) else products_data.get("results", [])
    results = []
    for prod in products_list:
        name = (prod.get("name") or "").strip()
        name_lower = name.lower()

        if any(excl in name_lower for excl in EXCLUDE_KEYWORDS):
            continue

        ptype = None
        if any(kw in name_lower for kw in ETB_KEYWORDS):
            ptype = "etb"
        elif any(kw in name_lower for kw in BOX_KEYWORDS):
            ptype = "booster_box"

        if ptype is None:
            continue

        pid   = prod.get("productId")
        price = price_map.get(pid, {})

        market = _f(price.get("marketPrice"))
        low    = _f(price.get("lowPrice"))
        high   = _f(price.get("highPrice") or price.get("marketPrice"))
        mid    = _f(price.get("midPrice"))

        # Build TCGPlayer URL from productId
        url = f"https://www.tcgplayer.com/product/{pid}" if pid else ""

        results.append({
            "id":           str(pid or name),
            "name":         name,
            "set_name":     gname,
            "product_type": ptype,
            "price_market": market or mid,
            "price_low":    low,
            "price_high":   high,
            "url":          url,
            "source":       "TCGPlayer via TCGCSV",
        })

    return results


def _f(val) -> Optional[float]:
    try:
        return round(float(val), 2) if val is not None else None
    except (TypeError, ValueError):
        return None


async def refresh_data():
    """Download all Pokémon sealed products from TCGCSV. Called on startup and daily."""
    if store["loading"]:
        return
    store["loading"]   = True
    store["load_error"] = None
    start = time.time()
    print("[TCGCSV] Starting data refresh…")

    try:
        limits = httpx.Limits(max_connections=MAX_PARALLEL, max_keepalive_connections=MAX_PARALLEL)
        async with httpx.AsyncClient(limits=limits, follow_redirects=True) as client:

            # Step 1: get all Pokémon set groups
            groups_raw = await fetch_json(client, f"{TCGCSV_BASE}/{POKEMON_CAT}/groups")
            if groups_raw is None:
                raise RuntimeError("Failed to fetch group list from TCGCSV")

            groups = groups_raw if isinstance(groups_raw, list) else groups_raw.get("results", [])
            store["groups_total"]  = len(groups)
            store["groups_loaded"] = 0
            print(f"[TCGCSV] Found {len(groups)} Pokémon set groups")

            # Step 2: fetch each group's products+prices in parallel batches
            all_products: list[dict] = []
            sem = asyncio.Semaphore(MAX_PARALLEL)

            async def bounded(group):
                async with sem:
                    result = await load_group(client, group)
                    store["groups_loaded"] += 1
                    if store["groups_loaded"] % 20 == 0:
                        print(f"  … {store['groups_loaded']}/{store['groups_total']} groups loaded")
                    return result

            batches = await asyncio.gather(*[bounded(g) for g in groups])
            for batch in batches:
                all_products.extend(batch)

        # Sort newest sets first (groups come back roughly chronologically but reversed)
        # We can't sort by date easily, so just keep order (most recent groups tend to be last)
        all_products.reverse()

        store["products"]     = all_products
        store["last_refresh"] = time.time()
        elapsed = round(time.time() - start, 1)
        print(f"[TCGCSV] Done — {len(all_products)} ETBs/Booster Boxes loaded in {elapsed}s")

    except Exception as e:
        store["load_error"] = str(e)
        print(f"[TCGCSV] Refresh failed: {e}")
    finally:
        store["loading"] = False


async def refresh_loop():
    """Background task: refresh data on startup, then once per day."""
    await refresh_data()
    while True:
        await asyncio.sleep(REFRESH_SECS)
        await refresh_data()


# ── App lifecycle ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(refresh_loop())
    yield
    task.cancel()


app = FastAPI(title="PokéVault API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Search (fully local) ──────────────────────────────────────────────────────

def search_local(query: str) -> list[dict]:
    """Search the in-memory product list. Zero network calls."""
    q = query.lower().strip()
    terms = q.split()
    results = []
    for p in store["products"]:
        haystack = f"{p['name']} {p['set_name']}".lower()
        if all(t in haystack for t in terms):
            results.append(p)
    return results[:30]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def search(q: str = Query(..., min_length=2)):
    """Instant local search — no API calls, no rate limits."""
    if store["loading"] and not store["products"]:
        return JSONResponse({
            "results": [],
            "query": q,
            "loading": True,
            "message": f"Loading product data… ({store['groups_loaded']}/{store['groups_total']} sets)"
        })

    results = search_local(q)
    return JSONResponse({
        "results": results,
        "query": q,
        "total_products": len(store["products"]),
        "last_refresh": int(store["last_refresh"]),
        "loading": False,
    })


@app.get("/api/status")
async def status():
    """Check data load status — poll this on startup to know when data is ready."""
    age_hours = round((time.time() - store["last_refresh"]) / 3600, 1) if store["last_refresh"] else None
    return {
        "loading":       store["loading"],
        "load_error":    store["load_error"],
        "products_ready": len(store["products"]),
        "groups_loaded": store["groups_loaded"],
        "groups_total":  store["groups_total"],
        "data_age_hours": age_hours,
        "next_refresh_hours": round((REFRESH_SECS - (time.time() - store["last_refresh"])) / 3600, 1) if store["last_refresh"] else None,
    }


@app.get("/api/health")
async def health():
    return {"status": "ok", "products_loaded": len(store["products"])}


@app.get("/")
async def index():
    with open("index.html", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(content=html, media_type="text/html; charset=utf-8")


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)