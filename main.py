"""
PokéVault — Pokémon ETB & Booster Box Value Tracker
Data source: TCGCSV.com (free, no API key, updated daily from TCGPlayer)
"""

import os
import time
import asyncio
from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import uvicorn

TCGCSV_BASE  = "https://tcgcsv.com/tcgplayer"
POKEMON_CAT  = 3
REFRESH_SECS = 86400
MAX_PARALLEL = 8

ETB_KEYWORDS     = ["elite trainer", " etb"]
BOX_KEYWORDS     = ["booster box", "booster display", "36 pack", "36-pack", "display box"]
EXCLUDE_KEYWORDS = ["single", "lot", "bundle", "code", "sleeve", "binder",
                    "deck", "tin", "blister", "mini", "collection box",
                    "figure", "pin", "coin", "repack", "opened", "empty"]

store = {
    "products":      [],
    "last_refresh":  0,
    "loading":       False,
    "load_error":    None,
    "groups_loaded": 0,
    "groups_total":  0,
}


async def fetch_json(client: httpx.AsyncClient, url: str):
    try:
        r = await client.get(url, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [WARN] Failed to fetch {url}: {e}")
        return None


async def load_group(client: httpx.AsyncClient, group: dict) -> list[dict]:
    gid   = group["groupId"]
    gname = group.get("name", f"Group {gid}")

    products_data = await fetch_json(client, f"{TCGCSV_BASE}/{POKEMON_CAT}/{gid}/products")
    prices_data   = await fetch_json(client, f"{TCGCSV_BASE}/{POKEMON_CAT}/{gid}/prices")

    if not products_data or not prices_data:
        return []

    # Build price lookup
    price_map: dict[int, dict] = {}
    prices_list = prices_data if isinstance(prices_data, list) else prices_data.get("results", [])
    for p in prices_list:
        pid = p.get("productId")
        if pid:
            existing = price_map.get(pid)
            if existing is None or p.get("subTypeName") == "Normal":
                price_map[pid] = p

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

        # Construct image URL directly from productId — reliable, always works
        # TCGPlayer CDN pattern: https://tcgplayer-cdn.tcgplayer.com/product/{id}_200w.jpg
        image_url = f"https://tcgplayer-cdn.tcgplayer.com/product/{pid}_200w.jpg" if pid else ""

        url = f"https://www.tcgplayer.com/product/{pid}" if pid else ""

        results.append({
            "id":           str(pid or name),
            "name":         name,
            "set_name":     gname,
            "product_type": ptype,
            "price_market": market or mid,
            "price_low":    low,
            "price_high":   high,
            "image_url":    image_url,
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
    if store["loading"]:
        return
    store["loading"]    = True
    store["load_error"] = None
    start = time.time()
    print("[TCGCSV] Starting data refresh…")

    try:
        limits = httpx.Limits(max_connections=MAX_PARALLEL, max_keepalive_connections=MAX_PARALLEL)
        async with httpx.AsyncClient(limits=limits, follow_redirects=True) as client:

            groups_raw = await fetch_json(client, f"{TCGCSV_BASE}/{POKEMON_CAT}/groups")
            if groups_raw is None:
                raise RuntimeError("Failed to fetch group list from TCGCSV")

            groups = groups_raw if isinstance(groups_raw, list) else groups_raw.get("results", [])
            store["groups_total"]  = len(groups)
            store["groups_loaded"] = 0
            print(f"[TCGCSV] Found {len(groups)} Pokémon set groups")

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
    await refresh_data()
    while True:
        await asyncio.sleep(REFRESH_SECS)
        await refresh_data()


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(refresh_loop())
    yield
    task.cancel()


app = FastAPI(title="PokéVault API", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def search_local(query: str) -> list[dict]:
    q     = query.lower().strip()
    terms = q.split()
    return [
        p for p in store["products"]
        if all(t in f"{p['name']} {p['set_name']}".lower() for t in terms)
    ][:30]


@app.get("/api/search")
async def search(q: str = Query(..., min_length=2)):
    if store["loading"] and not store["products"]:
        return JSONResponse({
            "results": [], "query": q, "loading": True,
            "message": f"Loading… ({store['groups_loaded']}/{store['groups_total']} sets)"
        })
    return JSONResponse({
        "results":        search_local(q),
        "query":          q,
        "total_products": len(store["products"]),
        "last_refresh":   int(store["last_refresh"]),
        "loading":        False,
    })


@app.get("/api/status")
async def status():
    age = round((time.time() - store["last_refresh"]) / 3600, 1) if store["last_refresh"] else None
    return {
        "loading":        store["loading"],
        "load_error":     store["load_error"],
        "products_ready": len(store["products"]),
        "groups_loaded":  store["groups_loaded"],
        "groups_total":   store["groups_total"],
        "data_age_hours": age,
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