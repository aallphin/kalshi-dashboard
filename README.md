# Kalshi Performance Dashboard

A live web dashboard for tracking your Kalshi trading performance. Hosted on GitHub Pages with automatic data updates via GitHub Actions.

## 🚀 Setup Instructions

### Step 1: Create GitHub Repository

1. Go to [github.com/new](https://github.com/new)
2. Name it `kalshi-dashboard` (or whatever you prefer)
3. Make it **Private** (important - this will contain your trading data)
4. Click **Create repository**

### Step 2: Upload Files

Upload all files from this folder to your new repository:
- `index.html`
- `fetch_data.py`
- `requirements.txt`
- `.github/workflows/update-data.yml`

You can drag and drop them directly on the GitHub page, or use:
```bash
cd kalshi-dashboard-site
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/kalshi-dashboard.git
git push -u origin main
```

### Step 3: Add Your API Credentials as Secrets

1. Go to your repository on GitHub
2. Click **Settings** → **Secrets and variables** → **Actions**
3. Click **New repository secret**
4. Add these two secrets:

**Secret 1:**
- Name: `KALSHI_API_KEY_ID`
- Value: Your Kalshi API Key ID (e.g., `5475d422-1cb7-4148-94cd-aeb60a5e5b16`)

**Secret 2:**
- Name: `KALSHI_PRIVATE_KEY`
- Value: Your entire private key, including the `-----BEGIN RSA PRIVATE KEY-----` and `-----END RSA PRIVATE KEY-----` lines

### Step 4: Enable GitHub Pages

1. Go to **Settings** → **Pages**
2. Under "Source", select **Deploy from a branch**
3. Select **main** branch and **/ (root)** folder
4. Click **Save**

Your dashboard will be live at: `https://YOUR_USERNAME.github.io/kalshi-dashboard/`

### Step 5: Run First Data Sync

1. Go to **Actions** tab
2. Click **Update Kalshi Data** workflow
3. Click **Run workflow** → **Run workflow**
4. Wait for it to complete (usually 2-3 minutes)

## 📊 How It Works

- **GitHub Actions** runs every 6 hours to fetch your latest trades from Kalshi
- Data is saved to `data.json` and committed to the repository
- **GitHub Pages** serves your dashboard as a static website
- The dashboard reads `data.json` when you open it

## 🔄 Manual Updates

To update data manually:
1. Go to **Actions** → **Update Kalshi Data**
2. Click **Run workflow**

## 🔒 Security

- Your API credentials are stored as GitHub Secrets (encrypted, never visible in code)
- The repository should be **Private** to keep your trading data confidential
- GitHub Pages can still serve private repos to you when you're logged in

## 🛠 Troubleshooting

**"Could not load data" error:**
- The workflow hasn't run yet. Go to Actions and run it manually.

**Workflow fails:**
- Check that your API credentials are correct in Settings → Secrets
- Make sure the private key includes the full PEM format with headers

**Data not updating:**
- Check the Actions tab for any failed runs
- GitHub Actions may be disabled - enable them in Settings → Actions
