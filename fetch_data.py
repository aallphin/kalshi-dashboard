"""
Kalshi Data Fetcher for GitHub Actions
Updated for Kalshi API fixed-point migration (March 2026)
- count → count_fp (fractional contracts)
- price (cents) → price_dollars
- Robust datetime parsing for microseconds > 6 digits
- Market status fetched via raw HTTP to bypass SDK pydantic enum validation
  (Kalshi returns 'finalized' which the SDK model rejects)
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import kalshi_python


def categorize_sport(ticker):
    t = ticker.upper()
    if any(k in t for k in ['PARLAY', 'BUNDLE', 'MULTIGAME']):
        return 'Parlays'
    if any(k in t for k in ['GOAL', 'POINT', 'ASSIST', 'TOUCHDOWN', 'YARD']):
        return 'Prop Bets'
    if 'NHL' in t:
        return 'NHL'
    if 'NFL' in t:
        return 'NFL'
    if 'NBA' in t:
        return 'NBA'
    if 'MLB' in t:
        return 'MLB'
    if 'NCAAF' in t or 'CFB' in t:
        return 'College Football'
    if 'NCAAB' in t or 'CBB' in t:
        return 'College Basketball'
    if 'UFC' in t or 'MMA' in t:
        return 'UFC/MMA'
    return 'Other'


def parse_date(created_time):
    """
    Robust ISO datetime parser.
    Handles Kalshi's microseconds that sometimes exceed 6 digits,
    e.g. '2026-01-20T03:12:39.96724+00:00' (only 5 decimal digits)
    or   '2026-03-01T12:00:00.1234567+00:00' (7 decimal digits).
    """
    if not created_time:
        return None
    try:
        s = str(created_time).replace('Z', '+00:00')
        # Normalize fractional seconds to exactly 6 digits
        if '.' in s:
            dot_idx = s.index('.')
            # Find where the fractional part ends ('+', '-', or end of string)
            end_idx = dot_idx + 1
            while end_idx < len(s) and s[end_idx].isdigit():
                end_idx += 1
            frac = s[dot_idx + 1:end_idx]
            frac_normalized = frac[:6].ljust(6, '0')  # truncate or pad to 6
            s = s[:dot_idx + 1] + frac_normalized + s[end_idx:]
        return datetime.fromisoformat(s)
    except Exception:
        return None


def get_fill_value(fill, attr, default=0):
    """
    Safely read a field from a fill object (works for both object and dict).
    Prefers the *_dollars / *_fp variants introduced in the March 2026 migration.
    Falls back to legacy field names so the script also works on older SDK versions.
    """
    if hasattr(fill, attr):
        return getattr(fill, attr, default)
    if isinstance(fill, dict):
        return fill.get(attr, default)
    return default


def extract_fill_fields(fill):
    """
    Extract count and price from a fill, handling both new fixed-point fields
    and legacy fields for backwards compatibility.
    """
    # count: prefer count_fp (fractional), fall back to count
    count = get_fill_value(fill, 'count_fp') or get_fill_value(fill, 'count', 0)
    try:
        count = float(count)
    except (TypeError, ValueError):
        count = 0.0

    # price: prefer price_dollars (already in $), fall back to price (cents)
    price_dollars = get_fill_value(fill, 'price_dollars', None)
    if price_dollars is not None:
        try:
            price = float(price_dollars)
        except (TypeError, ValueError):
            price = 0.0
    else:
        # Legacy: price was in cents
        price_cents = get_fill_value(fill, 'price', 0)
        try:
            price = float(price_cents) / 100.0
        except (TypeError, ValueError):
            price = 0.0

    return count, price


def calc_outcome(trade, market_dict):
    """
    market_dict is a plain Python dict from the raw API (not a pydantic SDK object),
    so it handles any status string including 'finalized'.
    Payout on Kalshi = $1.00 per contract won, so profit = count - cost.
    """
    if not market_dict:
        return {'status': 'unknown', 'payout': 0, 'profit': 0}
    try:
        status = (market_dict.get('status') or '').lower()
        result = (market_dict.get('result') or '').lower()
    except Exception:
        return {'status': 'unknown', 'payout': 0, 'profit': 0}

    if status in ['initialized', 'active', 'open']:
        return {'status': 'open', 'payout': 0, 'profit': 0}
    if status in ['closed', 'settled', 'finalized', 'determined']:
        if result and result == trade['side'].lower():
            # Each contract pays $1.00
            payout = trade['count'] * 1.0
            return {'status': 'won', 'payout': payout, 'profit': payout - trade['cost']}
        elif result:
            return {'status': 'lost', 'payout': 0, 'profit': -trade['cost']}
        else:
            # Settled but no result yet (e.g. voided)
            return {'status': 'unknown', 'payout': 0, 'profit': 0}
    return {'status': 'unknown', 'payout': 0, 'profit': 0}


def fetch_and_save_data():
    api_key_id = os.environ.get('KALSHI_API_KEY_ID')
    private_key = os.environ.get('KALSHI_PRIVATE_KEY')
    if not api_key_id or not private_key:
        raise ValueError("Missing credentials: KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY not set")

    private_key = private_key.replace('\\n', '\n')
    print("Connecting to Kalshi API...")

    config = kalshi_python.Configuration()
    config.host = 'https://api.elections.kalshi.com/trade-api/v2'
    config.api_key_id = api_key_id
    config.private_key_pem = private_key

    client = kalshi_python.KalshiClient(config)
    portfolio_api = kalshi_python.PortfolioApi(api_client=client)
    print("Connected!")

    # We use a raw requests session for market lookups so that Kalshi's
    # 'finalized' status doesn't get rejected by the SDK's pydantic model.
    # The SDK handles auth header signing for us via get_fills; for market GETs
    # we sign manually using the same key material.
    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend

    def get_raw_market(ticker, api_key_id, private_key_pem):
        """Fetch a market dict via raw HTTP, bypassing SDK pydantic validation."""
        base_url = 'https://api.elections.kalshi.com/trade-api/v2'
        path = f'/markets/{ticker}'
        timestamp_ms = str(int(time.time() * 1000))
        msg = timestamp_ms + 'GET' + path
        try:
            key = serialization.load_pem_private_key(
                private_key_pem.encode(), password=None, backend=default_backend()
            )
            sig = key.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
            sig_b64 = base64.b64encode(sig).decode()
        except Exception as e:
            print(f"    Signing error for {ticker}: {e}")
            return None
        headers = {
            'Content-Type': 'application/json',
            'KALSHI-ACCESS-KEY': api_key_id,
            'KALSHI-ACCESS-TIMESTAMP': timestamp_ms,
            'KALSHI-ACCESS-SIGNATURE': sig_b64,
        }
        try:
            r = requests.get(base_url + path, headers=headers, timeout=10)
            if r.status_code == 200:
                return r.json().get('market', {})
            else:
                print(f"    HTTP {r.status_code} for {ticker}")
                return None
        except Exception as e:
            print(f"    Request error for {ticker}: {e}")
            return None

    all_trades = []
    cursor = None
    page = 1
    while True:
        print(f"  Fetching fills page {page}...")
        try:
            if cursor:
                response = portfolio_api.get_fills(cursor=cursor, limit=100)
            else:
                response = portfolio_api.get_fills(limit=100)
        except Exception as e:
            print(f"  Error fetching fills: {e}")
            break

        fills = response.fills or []
        if not fills:
            break

        for fill in fills:
            ticker = get_fill_value(fill, 'ticker', '')
            if hasattr(fill, 'ticker'):
                ticker = fill.ticker
            elif isinstance(fill, dict):
                ticker = fill.get('ticker', '')

            side = get_fill_value(fill, 'side', 'yes')
            if hasattr(fill, 'side'):
                side = fill.side
            elif isinstance(fill, dict):
                side = fill.get('side', 'yes')

            created_time_raw = get_fill_value(fill, 'created_time', None)
            if hasattr(fill, 'created_time'):
                created_time_raw = fill.created_time

            trade_date = parse_date(created_time_raw)
            count, price = extract_fill_fields(fill)

            all_trades.append({
                'ticker': ticker,
                'side': side,
                'count': count,
                'price_dollars': price,
                'cost': price * count,
                'created_time': str(created_time_raw or ''),
                'trade_date': trade_date.isoformat() if trade_date else None,
                'sport': categorize_sport(ticker)
            })

        if hasattr(response, 'cursor') and response.cursor:
            cursor = response.cursor
            page += 1
        else:
            break

    print(f"Found {len(all_trades)} trades")

    print("Fetching market outcomes...")
    market_cache = {}
    for i, trade in enumerate(all_trades):
        if (i + 1) % 20 == 0:
            print(f"  {i + 1}/{len(all_trades)}")
        ticker = trade['ticker']
        if ticker not in market_cache:
            market_cache[ticker] = get_raw_market(ticker, api_key_id, private_key)
            time.sleep(0.05)

        outcome = calc_outcome(trade, market_cache[ticker])
        trade['outcome_status'] = outcome['status']
        trade['payout'] = outcome['payout']
        trade['profit'] = outcome['profit']

        if trade['trade_date']:
            try:
                dt = datetime.fromisoformat(trade['trade_date'])
                trade['month'] = dt.strftime('%b %Y')
                trade['month_sort'] = dt.strftime('%Y-%m')
            except Exception:
                trade['month'] = 'Unknown'
                trade['month_sort'] = 'Unknown'
        else:
            trade['month'] = 'Unknown'
            trade['month_sort'] = 'Unknown'

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

    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent = []
    for t in all_trades:
        if t.get('trade_date'):
            try:
                dt = datetime.fromisoformat(t['trade_date'])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt >= seven_days_ago:
                    recent.append(t)
            except Exception:
                pass

    r_won = len([t for t in recent if t['outcome_status'] == 'won'])
    r_lost = len([t for t in recent if t['outcome_status'] == 'lost'])
    r_open = len([t for t in recent if t['outcome_status'] == 'open'])
    r_cost = sum(t['cost'] for t in recent)
    r_profit = sum(t['profit'] for t in recent)
    r_win_rate = (r_won / (r_won + r_lost) * 100) if (r_won + r_lost) > 0 else 0

    sport_stats = []
    sorted_sports = sorted(by_sport.items(), key=lambda x: sum(t['profit'] for t in x[1]), reverse=True)
    for sport, trades in sorted_sports:
        sw = len([t for t in trades if t['outcome_status'] == 'won'])
        sl = len([t for t in trades if t['outcome_status'] == 'lost'])
        sc = sum(t['cost'] for t in trades)
        sp = sum(t['profit'] for t in trades)
        sport_stats.append({
            'sport': sport,
            'trades': len(trades),
            'won': sw,
            'lost': sl,
            'win_rate': round((sw / (sw + sl) * 100) if (sw + sl) > 0 else 0, 1),
            'invested': round(sc, 2),
            'profit': round(sp, 2),
            'avg_bet': round(sc / len(trades), 2) if trades else 0,
            'trades_list': sorted(trades, key=lambda x: x.get('trade_date') or '', reverse=True)
        })

    month_stats = []
    for ms in sorted(by_month.keys()):
        trades = by_month[ms]
        mw = len([t for t in trades if t['outcome_status'] == 'won'])
        ml = len([t for t in trades if t['outcome_status'] == 'lost'])
        mc = sum(t['cost'] for t in trades)
        mp = sum(t['profit'] for t in trades)
        month_stats.append({
            'month': trades[0].get('month', ms) if trades else ms,
            'month_sort': ms,
            'trades': len(trades),
            'won': mw,
            'lost': ml,
            'win_rate': round((mw / (mw + ml) * 100) if (mw + ml) > 0 else 0, 1),
            'invested': round(mc, 2),
            'profit': round(mp, 2),
            'avg_bet': round(mc / len(trades), 2) if trades else 0,
            'trades_list': sorted(trades, key=lambda x: x.get('trade_date') or '', reverse=True)
        })

    recent_sorted = sorted(recent, key=lambda x: x.get('trade_date') or '', reverse=True)
    all_sorted = sorted(all_trades, key=lambda x: x.get('trade_date') or '', reverse=True)
    open_trades = [t for t in all_trades if t['outcome_status'] == 'open']

    data = {
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
        'recent_7_days': {
            'trades': len(recent),
            'won': r_won,
            'lost': r_lost,
            'open': r_open,
            'win_rate': round(r_win_rate, 1),
            'invested': round(r_cost, 2),
            'profit': round(r_profit, 2),
            'trades_list': recent_sorted[:20]
        },
        'open_trades': open_trades,
        'by_sport': sport_stats,
        'by_month': month_stats,
        'all_trades': all_sorted
    }

    with open('data.json', 'w') as f:
        json.dump(data, f, indent=2, default=str)

    print(f"\nDone! Record: {won}W-{lost}L ({win_rate:.1f}%) | Profit: ${total_profit:+,.2f}")


if __name__ == "__main__":
    fetch_and_save_data()
