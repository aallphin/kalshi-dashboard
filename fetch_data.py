"""
Kalshi Data Fetcher for GitHub Actions
Fetches trading data and saves as JSON for the dashboard
"""

import os
import requests
import json
import time
import hashlib
import base64
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.backends import default_backend


class KalshiAPI:
    """Simple Kalshi API client using direct REST calls"""

    def __init__(self, api_key_id: str, private_key_pem: str):
        self.api_key_id = api_key_id
        self.base_url = "https://api.elections.kalshi.com/trade-api/v2"

        # Load the private key
        self.private_key = serialization.load_pem_private_key(
            private_key_pem.encode(),
            password=None,
            backend=default_backend()
        )

    def _sign_request(self, method: str, path: str, timestamp: str) -> str:
        """Sign a request using RSA-SHA256"""
        message = f"{timestamp}{method}{path}".encode()
        signature = self.private_key.sign(
            message,
            padding.PKCS1v15(),
            hashes.SHA256()
        )
        return base64.b64encode(signature).decode()

    def _make_request(self, method: str, endpoint: str, params: dict = None) -> dict:
        """Make an authenticated request to the Kalshi API"""
        timestamp = str(int(time.time() * 1000))
        path = f"/trade-api/v2{endpoint}"

        # Add query params to path for signature if present
        if params:
            query_string = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if query_string:
                path = f"{path}?{query_string}"

        signature = self._sign_request(method, path, timestamp)

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }

        url = f"{self.base_url}{endpoint}"

        if method == "GET":
            response = requests.get(url, headers=headers, params=params)
        else:
            response = requests.post(url, headers=headers, json=params)

        response.raise_for_status()
        return response.json()

    def get_fills(self, cursor: str = None, limit: int = 100) -> dict:
        """Get trade fills with pagination"""
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._make_request("GET", "/portfolio/fills", params)

    def get_market(self, ticker: str) -> dict:
        """Get market details"""
        return self._make_request("GET", f"/markets/{ticker}")


def categorize_sport(ticker: str) -> str:
    """Determine sport from ticker"""
    ticker_upper = ticker.upper()

    if any(kw in ticker_upper for kw in ['PARLAY', 'BUNDLE', 'MULTIGAME', 'MULTI', 'COMBO']):
        return 'Parlays'

    if any(kw in ticker_upper for kw in ['GOAL', 'POINT', 'ASSIST', 'REBOUND', 'TOUCHDOWN',
                                          'YARD', 'RECEPTION', 'RUSH', 'PASS', 'HIT', 'RBI',
                                          'STRIKEOUT', 'SAVE', 'SHOT', 'ESPORTS', 'GAMING', 'PLAYER']):
        return 'Prop Bets'

    if 'NHL' in ticker_upper:
        return 'NHL'
    elif 'NFL' in ticker_upper:
        return 'NFL'
    elif 'NBA' in ticker_upper:
        return 'NBA'
    elif 'MLB' in ticker_upper:
        return 'MLB'
    elif 'NCAAF' in ticker_upper or 'CFB' in ticker_upper:
        return 'College Football'
    elif 'NCAAB' in ticker_upper or 'CBB' in ticker_upper:
        return 'College Basketball'
    elif 'SOCCER' in ticker_upper or 'EPL' in ticker_upper or 'PREMIER' in ticker_upper:
        return 'Soccer'
    elif 'UFC' in ticker_upper or 'MMA' in ticker_upper or 'FIGHT' in ticker_upper:
        return 'UFC/MMA'
    elif 'PGA' in ticker_upper or 'GOLF' in ticker_upper:
        return 'Golf'
    else:
        return 'Other'


def parse_trade_date(created_time) -> datetime:
    """Parse trade timestamp"""
    if not created_time:
        return None
    try:
        time_str = str(created_time)
        if 'Z' in time_str:
            time_str = time_str.replace('Z', '+00:00')
        if '.' in time_str and '+' in time_str:
            parts = time_str.split('.')
            microseconds = parts[1].split('+')[0][:6].ljust(6, '0')
            time_str = f"{parts[0]}.{microseconds}+00:00"
        return datetime.fromisoformat(time_str)
    except:
        return None


def calculate_outcome(trade: dict, market_info: dict) -> dict:
    """Calculate if trade won, lost, or is open"""
    if not market_info:
        return {'status': 'unknown', 'payout': 0, 'profit': 0}

    market = market_info.get('market', {})
    status = market.get('status', '').lower()
    result = (market.get('result') or '').lower()

    if status in ['open', 'active']:
        return {'status': 'open', 'payout': 0, 'profit': 0}

    if status in ['closed', 'settled', 'finalized']:
        trade_side = trade['side'].lower()
        count = trade['count']
        cost = trade['cost']

        if result == trade_side:
            payout = count * 1.0
            profit = payout - cost
            return {'status': 'won', 'payout': payout, 'profit': profit}
        else:
            return {'status': 'lost', 'payout': 0, 'profit': -cost}

    return {'status': 'unknown', 'payout': 0, 'profit': 0}


def fetch_and_save_data():
    """Main function to fetch data and save JSON"""

    # Get credentials from environment variables (GitHub Secrets)
    api_key_id = os.environ.get('KALSHI_API_KEY_ID')
    private_key = os.environ.get('KALSHI_PRIVATE_KEY')

    if not api_key_id or not private_key:
        raise ValueError("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY environment variables")

    print("Connecting to Kalshi API...")
    api = KalshiAPI(api_key_id, private_key)

    # Fetch all trades
    print("Fetching trades...")
    all_trades = []
    cursor = None
    page = 1

    while True:
        print(f"  Page {page}...")
        response = api.get_fills(cursor=cursor, limit=100)
        fills = response.get('fills', [])

        if not fills:
            break

        for fill in fills:
            ticker = fill.get('ticker', '')
            side = fill.get('side', 'yes')
            count = fill.get('count', 0)
            price = fill.get('price', 0)
            created_time = fill.get('created_time')

            trade_date = parse_trade_date(created_time)
            cost = price * count

            all_trades.append({
                'ticker': ticker,
                'side': side,
                'count': count,
                'price_dollars': price,
                'cost': cost,
                'created_time': str(created_time) if created_time else None,
                'trade_date': trade_date.isoformat() if trade_date else None,
                'sport': categorize_sport(ticker)
            })

        cursor = response.get('cursor')
        if not cursor:
            break
        page += 1

    print(f"Found {len(all_trades)} trades")

    # Fetch market outcomes (with caching)
    print("Fetching market outcomes...")
    market_cache = {}

    for i, trade in enumerate(all_trades):
        if (i + 1) % 20 == 0:
            print(f"  Progress: {i + 1}/{len(all_trades)}")

        ticker = trade['ticker']
        if ticker not in market_cache:
            try:
                market_cache[ticker] = api.get_market(ticker)
                time.sleep(0.1)  # Rate limiting
            except Exception as e:
                print(f"  Warning: Could not fetch market {ticker}: {e}")
                market_cache[ticker] = None

        market_info = market_cache[ticker]
        outcome = calculate_outcome(trade, market_info)

        trade['outcome_status'] = outcome['status']
        trade['payout'] = outcome['payout']
        trade['profit'] = outcome['profit']

        if trade['trade_date']:
            try:
                dt = datetime.fromisoformat(trade['trade_date'])
                trade['month'] = dt.strftime('%b %Y')
                trade['month_sort'] = dt.strftime('%Y-%m')
            except:
                trade['month'] = 'Unknown'
                trade['month_sort'] = 'Unknown'
        else:
            trade['month'] = 'Unknown'
            trade['month_sort'] = 'Unknown'

    # Organize data
    by_sport = defaultdict(list)
    by_month = defaultdict(list)

    for trade in all_trades:
        by_sport[trade['sport']].append(trade)
        if trade['month_sort'] != 'Unknown':
            by_month[trade['month_sort']].append(trade)

    # Calculate summary stats
    total_cost = sum(t['cost'] for t in all_trades)
    total_payout = sum(t['payout'] for t in all_trades)
    total_profit = sum(t['profit'] for t in all_trades)

    won = len([t for t in all_trades if t['outcome_status'] == 'won'])
    lost = len([t for t in all_trades if t['outcome_status'] == 'lost'])
    open_count = len([t for t in all_trades if t['outcome_status'] == 'open'])

    win_rate = (won / (won + lost) * 100) if (won + lost) > 0 else 0
    roi = (total_profit / total_cost * 100) if total_cost > 0 else 0
    avg_bet = total_cost / len(all_trades) if all_trades else 0

    # Recent 7 days
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent_trades = []
    for t in all_trades:
        if t.get('trade_date'):
            try:
                dt = datetime.fromisoformat(t['trade_date'])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= seven_days_ago:
                    recent_trades.append(t)
            except:
                pass

    recent_won = len([t for t in recent_trades if t['outcome_status'] == 'won'])
    recent_lost = len([t for t in recent_trades if t['outcome_status'] == 'lost'])
    recent_stats = {
        'trades': len(recent_trades),
        'won': recent_won,
        'lost': recent_lost,
        'open': len([t for t in recent_trades if t['outcome_status'] == 'open']),
        'win_rate': round((recent_won / (recent_won + recent_lost) * 100) if (recent_won + recent_lost) > 0 else 0, 1),
        'invested': round(sum(t['cost'] for t in recent_trades), 2),
        'profit': round(sum(t['profit'] for t in recent_trades), 2),
        'trades_list': sorted(recent_trades, key=lambda x: x.get('trade_date') or '', reverse=True)[:20]
    }

    # Sport stats
    sport_stats = []
    for sport, trades in sorted(by_sport.items(), key=lambda x: sum(t['profit'] for t in x[1]), reverse=True):
        s_won = len([t for t in trades if t['outcome_status'] == 'won'])
        s_lost = len([t for t in trades if t['outcome_status'] == 'lost'])
        s_cost = sum(t['cost'] for t in trades)
        sport_stats.append({
            'sport': sport,
            'trades': len(trades),
            'won': s_won,
            'lost': s_lost,
            'win_rate': round((s_won / (s_won + s_lost) * 100) if (s_won + s_lost) > 0 else 0, 1),
            'invested': round(s_cost, 2),
            'profit': round(sum(t['profit'] for t in trades), 2),
            'avg_bet': round(s_cost / len(trades), 2) if trades else 0,
            'trades_list': sorted(trades, key=lambda x: x.get('trade_date') or '', reverse=True)
        })

    # Month stats
    month_stats = []
    for month_sort in sorted(by_month.keys()):
        trades = by_month[month_sort]
        m_won = len([t for t in trades if t['outcome_status'] == 'won'])
        m_lost = len([t for t in trades if t['outcome_status'] == 'lost'])
        m_cost = sum(t['cost'] for t in trades)
        month_stats.append({
            'month': trades[0].get('month', month_sort) if trades else month_sort,
            'month_sort': month_sort,
            'trades': len(trades),
            'won': m_won,
            'lost': m_lost,
            'win_rate': round((m_won / (m_won + m_lost) * 100) if (m_won + m_lost) > 0 else 0, 1),
            'invested': round(m_cost, 2),
            'profit': round(sum(t['profit'] for t in trades), 2),
            'avg_bet': round(m_cost / len(trades), 2) if trades else 0,
            'trades_list': sorted(trades, key=lambda x: x.get('trade_date') or '', reverse=True)
        })

    # Build final data
    dashboard_data = {
        'generated_at': datetime.now().isoformat(),
        'generated_at_display': datetime.now().strftime('%B %d, %Y at %I:%M %p'),
        'summary': {
            'total_trades': len(all_trades),
            'won': won,
            'lost': lost,
            'open': open_count,
            'win_rate': round(win_rate, 1),
            'total_invested': round(total_cost, 2),
            'total_payout': round(total_payout, 2),
            'total_profit': round(total_profit, 2),
            'roi': round(roi, 1),
            'avg_bet': round(avg_bet, 2)
        },
        'recent_7_days': recent_stats,
        'open_trades': [t for t in all_trades if t['outcome_status'] == 'open'],
        'by_sport': sport_stats,
        'by_month': month_stats,
        'all_trades': sorted(all_trades, key=lambda x: x.get('trade_date') or '', reverse=True)
    }

    # Save JSON
    with open('data.json', 'w') as f:
        json.dump(dashboard_data, f, indent=2, default=str)

    print(f"\n✓ Data saved to data.json")
    print(f"  Record: {won}W - {lost}L ({win_rate:.1f}%)")
    print(f"  Net Profit: ${total_profit:+,.2f}")


if __name__ == "__main__":
    fetch_and_save_data()
