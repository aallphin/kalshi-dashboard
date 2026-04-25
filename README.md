# Kalshi Performance Dashboard

A live web dashboard for tracking your Kalshi trading performance.
A GitHub Actions cron job pulls your fills from Kalshi every six hours,
groups them into positions, computes settled vs. open P/L, and writes
`data.json`. The static `index.html` reads that file.

## What changed in this version

- Fills are grouped into positions by `(ticker, side)` — multi-fill orders
  no longer count as multiple trades.
- Buy and sell fills are tracked separately. Closing fills now correctly
  show up as proceeds instead of being double-counted as new buys.
- Voided / no-contest markets are tracked in a `void` bucket and refunded
  instead of being silently treated as losses.
- Headline ROI is computed on **settled** positions only. Open exposure
  is shown in its own card so live capital doesn't drag the headline down.
- Each trade now has both a `sport` and `bet_type`, derived from the
  Kalshi ticker prefix (Moneyline, Spread, 1H Spread, Total, Parlay,
  Cross-Category Parlay, Outright Winner, MVP/Award, etc.).
- New "By Bet Type" tab with the same table format as "By Sport".
- Per-sport / per-bet-type / per-month tables now include `ROI`.

## Setup

### 1. Create a private GitHub repository

The repo MUST be private — `data.json` contains every trade you've made.

### 2. Upload the files

Upload these to the root of the repo:

- `index.html`
- `fetch_data.py`
- `requirements.txt`
- `.github/workflows/update-data.yml`

### 3. Add API credentials as Actions secrets

Settings → Secrets and variables → Actions → New repository secret.

- `KALSHI_API_KEY_ID` — your Kalshi API key id (UUID).
- `KALSHI_PRIVATE_KEY` — the full PEM, including the
  `-----BEGIN ... PRIVATE KEY-----` / `-----END ... PRIVATE KEY-----` lines.

### 4. Run the workflow once

Actions tab → "Update Kalshi Data" → Run workflow. After it finishes
you'll have a `data.json` committed to `main`.

### 5. Pick where to host

You have a few options. The previous README assumed GitHub Pages, but
that's not the cleanest fit if you're on a free GitHub plan.

#### Option A: GitHub Pages (only if you have GitHub Pro/Team)

Settings → Pages → Source: "Deploy from a branch" → Branch: `main`,
folder: `/ (root)`. Your dashboard goes live at
`https://<username>.github.io/kalshi-dashboard/`.

Pages on a *private* repo requires a paid GitHub plan. On the free plan,
the only way to use Pages is to make the repo public — which would make
your `data.json` (every trade, every dollar) world-readable. **Don't.**

#### Option B: Cloudflare Pages (recommended on free)

Free tier supports private GitHub repos and gives you a fast global CDN.

1. Go to https://pages.cloudflare.com/ and create an account.
2. "Create a project" → "Connect to Git" → authorize Cloudflare on your
   GitHub account → pick `kalshi-dashboard`.
3. Build settings: leave the build command **empty**, output directory
   `/`. We aren't building anything — just serving static files.
4. Deploy. You get a URL like `kalshi-dashboard.pages.dev`.

Cloudflare automatically redeploys whenever the GitHub Actions cron job
commits a new `data.json`, so the dashboard stays fresh without any
extra wiring.

#### Option C: Vercel or Netlify

Same idea as Cloudflare Pages: free tier, private repos OK, GitHub
integration. Use either if you already have an account there.

#### Option D: Don't host it at all

If you only want to look at the dashboard from your own laptop, just
clone the repo and open `index.html` in a browser. No hosting needed.

## How it works

1. `.github/workflows/update-data.yml` runs `fetch_data.py` every 6 hours.
2. `fetch_data.py` calls `/portfolio/fills`, paginates through every fill,
   groups them by `(ticker, side)`, looks up each market's settlement
   status, computes per-position P/L, and writes `data.json`.
3. The Action commits `data.json` back to `main`.
4. Your hosting provider (Cloudflare Pages / Vercel / GitHub Pages)
   redeploys, and the dashboard reads the new `data.json`.

## Manual update

Actions → "Update Kalshi Data" → Run workflow.

## Known limitations

- **Trading fees are not yet subtracted from P/L.** Kalshi charges a
  small per-fill fee that lands on settlements. The dashboard shows a
  warning banner about this. If you want exact net P/L, plan to wire in
  `/portfolio/settlements` and subtract `revenue - cost` per market.
- The ticker → sport / bet-type taxonomy is based on prefix matching.
  Unknown leagues fall into the `Other` bucket; if you see one, the
  patterns at the top of `fetch_data.py` are easy to extend.

## Troubleshooting

**Workflow fails authenticating to Kalshi:** confirm the private key
secret was pasted with the BEGIN/END lines intact and no surrounding
whitespace. Kalshi switched to RSA-PSS signing — `fetch_data.py`
handles that internally; just make sure the key material is correct.

**"Could not load data" in the browser:** the workflow hasn't run yet,
or your hosting provider hasn't redeployed since the last commit.
Check the Actions tab and your hosting dashboard.

**Bucket says `Other` for a sport you trade often:** add the league
prefix (without the leading `KX`) to `SPORT_PATTERNS` in
`fetch_data.py`, then re-run the workflow.
