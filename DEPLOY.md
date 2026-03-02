# PokéVault — Deployment Guide

## No API Key Required!
Prices come from TCGCSV.com, a free public mirror of TCGPlayer data.
No signup, no key, no rate limits, no cost.

---

## Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Run (no env vars needed)
python main.py

# Open http://localhost:8080
# The app will take ~30-60 seconds to load all product data on first start
# Watch the terminal for progress
```

---

## Deploy to Google Cloud Run

### One-time setup
```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com
```

### Deploy (from inside the pokemon-tracker/ folder)
```bash
gcloud run deploy pokevault \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 3 \
  --port 8080
```

No `--set-env-vars` needed — there are no secrets!

### Redeploy after code changes
```bash
gcloud run deploy pokevault --source . --region us-central1
```

---

## How it works

1. On startup the app fetches all Pokémon set groups from TCGCSV.com
2. For each set it downloads products + prices and filters to ETBs and Booster Boxes
3. Everything is stored in memory — all searches are instant local queries
4. Data refreshes automatically every 24 hours
5. Your collection is saved to browser localStorage

## Startup time
The first load fetches ~200+ sets in parallel batches. Expect:
- Local: ~20-40 seconds
- Cloud Run cold start: ~30-60 seconds (only happens after periods of inactivity)

The frontend shows a progress bar while loading.
