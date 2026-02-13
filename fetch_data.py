"""
Kalshi Data Fetcher for GitHub Actions
Fetches trading data and saves as JSON for the dashboard
Uses kalshi-python package for proper authentication
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import kalshi_python


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


def calculate_outcome(trade: dict, market_info) -> dict:
    """Calculate if trade won, lost, or is open"""
    if not market_info:
        return {'status': 'unknown', 'payout': 0, 'profit': 0}

    # Handle both dict and object responses
    if hasattr(market_info, 'market'):
        market = market_info.market
        status = getattr(market, 'status', '').lower()
        result = getattr(market, 'result', '') or ''
        result = result.lower()
    elif isinstance(market_info, dict):
        market = market_info.get('market', {})
        status = market.get('status', '').lower()
        result = (market.get('result') or '').lower()
    else:
        return {'status': 'unknown', 'payout': 0, 'profit': 0}

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

    # Get credentials from environment variables
    api_key_id = os.environ.get('KALSHI_API_KEY_ID')
    private_key = os.environ.get('KALSHI_PRIVATE_KEY')

    if not api_key_id or not private_key:
        raise ValueError("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY environment variables")

    # Fix potential newline issues in private key from environment variable
    private_key = private_key.replace('\\n', '\n')

    print("Connecting to Kalshi API...")

    # Configure the API using kalshi_python
    config = kalshi_python.Configuration()
    config.host = 'https://api.elections.kalshi.com/trade-api/v2'
    config.api_key_id = api_key_id
    config.private_key_pem = private_key

    # Create client
    client = kalshi_python.KalshiClient(config)
    portfolio_api = kalshi_python.PortfolioApi(api_client=client)
    markets_api = kalshi_python.MarketsApi(api_client=client)

    print("Successfully connected!")

    # Fetch all trades
    print("Fetching trades...")
    all_trades = []
    cursor = None
    page = 1

    while True:
        print(f"  Page {page}...")

        if cursor:
            response = portfolio_api.get_fills(cursor=cursor, limit=100)
        else:
            response = portfolio_api.get_fills(limit=100)

        fills = response.fills or []

        if not fills:
            break

        for fill in fills:
            ticker = getattr(fill, 'ticker', '')
            side = getattr(fill, 'side', 'yes')
            count = getattr(fill, 'count', 0)
            price = getattr(fill, 'price', 0)
            created_time = getattr(fill, 'created_time', None)

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

        if hasattr(response, 'cursor') and response.cursor:
            cursor = response.cursor
            page += 1
        else:
            break

    print(f"Found {len(all_trades)} trades")

    # Fetch market outcomes
    print("Fetching market outcomes...")
    market_cache = {}

    for i, trade in enumerate(all_trades):
        if (i + 1) % 20 == 0:
            print(f"  Progress: {i + 1}/{len(all_trades)}")

        ticker = trade['ticker']
        if ticker not in market_cache:
            try:
                market_cache[ticker] = markets_api.get_market(ticker)
                time.sleep(0.05)  # Rate limiting
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
                trade['month'] = dt.str
