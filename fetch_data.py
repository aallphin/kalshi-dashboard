"""
Kalshi Data Fetcher for GitHub Actions
"""

import os
import json
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import kalshi_python


def categorize_sport(ticker):
    t = ticker.upper()
    if any(k in t for k in ['PARLAY', 'BUNDLE', 'MULTIGAME', 'MULTI', 'COMBO']):
        return 'Parlays'
    if any(k in t for k in ['GOAL', 'POINT', 'ASSIST', 'REBOUND', 'TOUCHDOWN', 'YARD', 'RECEPTION', 'RUSH', 'PASS', 'HIT', 'RBI', 'STRIKEOUT', 'SAVE', 'SHOT', 'PLAYER']):
        return 'Prop Bets'
    if 'NHL' in t: return 'NHL'
    if 'NFL' in t: return 'NFL'
    if 'NBA' in t: return 'NBA'
    if 'MLB' in t: return 'MLB'
    if 'NCAAF' in t or 'CFB' in t: return 'College Football'
    if 'NCAAB' in t or 'CBB' in t: return 'College Basketball'
    if 'SOCCER' in t or 'EPL' in t: return 'Soccer'
    if 'UFC' in t or 'MMA' in t: return 'UFC/MMA'
    if 'PGA' in t or 'GOLF' in t: return 'Golf'
    return 'Other'


def parse_date(created_time):
    if not created_time:
        return None
    try:
        s = str(created_time).replace('Z', '+00:00')
        return datetime.fromisoformat(s)
    except:
        return None


def calc_outcome(trade, market_info):
    if not market_info:
        return {'status': 'unknown', 'payout': 0, 'profit': 0}
    try:
        market = market_info.market if hasattr(market_info, 'market') else market_info.get('market', {})
        status = (getattr(market, 'status', '') or market.get('status', '')).lower()
        result = (getattr(market, 'result', '') or market.get('result', '') or '').lower()
    except:
        return {'status': 'unknown', 'payout': 0, 'profit': 0}
    
    if status in ['open', 'active']:
        return {'status': 'open', 'payout': 0, 'profit': 0}
    if status in ['closed', 'settled', 'finalized']:
        if result == trade['side'].lower():
            payout = trade['count'] * 1.0
            return {'status': 'won', 'payout': payout, 'profit': payout - trade['cost']}
        else:
            return {'status': 'lost', 'payout': 0, 'profit': -trade['cost']}
    return {'status': 'unknown', 'payout': 0, 'profit': 0}


def fetch_and_save_data():
    api_key_id = os.environ.get('KALSHI_API_KEY_ID')
    private_key = os.environ.get('KALSHI_PRIVATE_KEY')
    if not api_key_id or not private_key:
        raise ValueError("Missing credentials")
    
    private_key = private_key.replace('\\n', '\n')
    print("Connecting to Kalshi API...")
    
    config = kalshi_python.Configuration()
    config.host = 'https://api.elections.kalshi.com/trade-api/v2'
    config.api_key_id = api_key_id
    config.private_key_pem = private_key
    
    client = kalshi_python.KalshiClient(config)
    portfolio_api = kalshi_python.PortfolioApi(api_client=client)
    markets_api = kalshi_python.MarketsApi(api_client=client)
    print("Connected!")

    # Fetch trades
    all_trades = []
    cursor = None
    page = 1
    while True:
        print(f"  Page {page}...")
        response = portfolio_api.get_fills(cursor=cursor, limit=100) if cursor else portfolio_api.get_fills(limit=100)
        fills = response.fills or []
        if not fills:
            break
        for fill in fills:
            ticker = getattr(fill, 'ticker', '')
            trade_date = parse_date(getattr(fill, 'created_time', None))
            price = getattr(fill, 'price', 0)
            count = getattr(fill, 'count', 0)
            all_trades.append({
                'ticker': ticker,
                'side': getattr(fill, 'side', 'yes'),
                'count': count,
                'price_dollars': price,
                'cost': price * count,
                'created_time': str(getattr(fill, 'created_time', '')),
                'trade_date': trade_date.isoformat() if trade_date else None,
                'sport': categorize_sport(ticker)
            })
        cursor = response.cursor if hasattr(response, 'cursor') and response.cursor else None
        if not cursor:
            break
        page += 1
    print(f"Found {len(all_trades)} trades")

    # Fetch outcomes
    print("Fetching outcomes...")
    market_cache = {}
    for i, trade in enumerate(all_trades):
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(all_trades)}")
        ticker = trade['ticker']
        if ticker not in market_cache:
            try:
                market_cache[ticker] = markets_api.get_market(ticker)
                time.sleep(0.05)
            except Exception as e:
                print(f"  Warning: {ticker}: {e}")
                market_cache[ticker] = None
        outcome = calc_outcome(trade, market_cache[ticker])
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

    # Stats
    by_sport = defaultdict(list)
    by_month = defaultdict(list)
    for t in all_trades:
        by_sport[t['sport']].append(t)
        if t['month_sort'] != 'Unknown':
            by_month[t['month_sort']].append(t)

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
    recent = [t for t in all_trades if t.get('trade_date') and datetime.fromisoformat(t['trade_date']).replace(tzinfo=timezone.utc) >= seven_days_ago]
    r_won = len([t for t in recent if t['outcome_status'] == 'won'])
    r_lost = len([t for t in recent if t['outcome_status'] == 'lost'])

    sport_stats = []
    for sport, trades in sorted(by_sport.items(), key=lambda x: sum(t['profit'] for t in x[1]), reverse=True):
        sw = len([t for t in trades if t['outcome_status'] == 'won'])
        sl = len([t for t in trades if t['outcome_status'] == 'lost'])
        sc = sum(t['cost'] for t in trades)
        sport_stats.append({'sport': sport, 'trades': len(trades), 'won': sw, 'lost': sl,
            'win_rate': round((sw / (sw + sl) * 100) if (sw + sl) > 0 else 0, 1),
            'invested': round(sc, 2), 'profit': round(sum(t['profit'] for t in trades), 2),
            'avg_bet': round(sc / len(trades), 2) if trades else 0,
            'trades_list': sorted(trades, key=lambda x: x.get('trade_date') or '', reverse=True)})

    month_stats = []
    for ms in sorted(by_month.keys()):
        trades = by_month[ms]
        mw = len([t for t in trades if t['outcome_status'] == 'won'])
        ml = len([t for t in trades if t['outcome_status'] == 'lost'])
        mc = sum(t['cost'] for t in trades)
        month_stats.append({'month': trades[0].get('month', ms), 'month_sort': ms, 'trades': len(trades),
            'won': mw, 'lost': ml, 'win_rate': round((mw / (mw + ml) * 100) if (mw + ml) > 0 else 0, 1),
            'invested': round(mc, 2), 'profit': round(sum(t['profit'] for t in trades), 2),
            'avg_bet': round(mc / len(trades), 2) if trades else 0,
            'trades_list': sorted(trades, key=lambda x: x.get('trade_date') or '', reverse=True)})

    data = {
        'generated_at': datetime.now().isoformat(),
        'generated_at_display': datetime.now().strftime('%B %d, %Y at %I:%M %p'),
        'summary': {'total_trades': len(all_trades), 'won': won, 'lost': lost, 'open': open_count,
            'win_rate': round(win_rate, 1), 'total_invested': round(total_cost, 2),
            'total_payout': round(total_payout, 2), 'total_profit': round(total_profit, 2),
            'roi': round(roi, 1), 'avg_bet': round(avg_bet, 2)},
        'recent_7_days': {'trades': len(recent), 'won': r_won, 'lost': r_lost,
            'open': len([t for t in recent if t['outcome_status'] == 'open']),
            'win_rate': round((r_won / (r_won + r_lost) * 100) if (r_won + r_lost) > 0 else 0, 1),
            'invested': round(sum(t['cost'] for t in recent), 2),
            'profit': round(sum(t['profit'] for t in recent),
