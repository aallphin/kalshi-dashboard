"""
Kalshi Data Fetcher for GitHub Actions
======================================

Pulls fills from /portfolio/fills, groups them into POSITIONS (so multiple fills
of the same order count as one trade), looks up each market's settlement
status, and writes a denormalized data.json the dashboard reads.

Major correctness fixes vs. the previous version:
- Multi-fill orders no longer inflate trade count or distort avg_bet.
- Buy vs. sell fills are tracked separately. Closing fills (sells) are credited
  as proceeds instead of being double-counted as new buys.
- Voided / no_contest markets are surfaced as their own bucket (capital is
  refunded) instead of silently corrupting totals.
- Headline ROI is computed on SETTLED positions only. Open exposure is shown
  in its own card so live trades don't drag the headline number down.
- price_dollars is used directly. The old "if price > 1.0 divide by 100"
  heuristic is gone (it could double-divide on legitimate $1.00 fills).
- next_cursor pagination no longer breaks early on an empty page.
- generated_at is now an explicit UTC timestamp.

Output schema additions:
- summary.settled_*           : invested / payout / profit / roi for closed bets only
- summary.open_exposure       : capital currently tied up in open positions
- summary.void                : count of refunded markets
- by_sport[i].roi             : per-sport ROI %
- by_bet_type                 : new aggregate, parallel to by_sport (Moneyline,
                                Spread, Total, Parlay, Futures, etc.)
- trade.bet_type              : bet category derived from the ticker prefix
- trade.fills_count           : how many fills make up this position
- trade.gross_cost            : sum of buy fills (price * count)
- trade.sell_proceeds         : sum of sell fills (price * count)
- trade.net_contracts         : contracts still held into settlement
"""

import os
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import kalshi_python


# ---------------------------------------------------------------------------
# Ticker taxonomy
# ---------------------------------------------------------------------------
# Kalshi tickers look like KX{LEAGUE}{BETTYPE}-{event}-{outcome}, e.g.
#   KXNHLGAME-26APR20OTTCAR-OTT          -> NHL moneyline
#   KXNBA1HSPREAD-26APR07HOUPHX-HOU3     -> NBA 1H spread
#   KXMVESPORTSMULTIGAMEEXTENDED-...     -> Esports parlay
#   KXMVECROSSCATEGORY-...               -> Cross-sport parlay
# We parse the first segment (before the first '-') to extract sport and
# bet type. Order of patterns matters: longer prefixes must be checked first.

# (substring, pretty_name) — checked in order, first match wins
SPORT_PATTERNS = [
    ('NCAAHOCKEY',   'College Hockey'),
    ('NCAAFB',       'College Football'),
    ('NCAAF',        'College Football'),
    ('CFB',          'College Football'),
    ('NCAAMB',       'College Basketball'),
    ('NCAAWB',       "Women's College Basketball"),
    ('CBB',          'College Basketball'),
    ('WOMHOCKEY',    "Women's Hockey"),
    ('WNBA',         'WNBA'),
    ('NHL',          'NHL'),
    ('NFL',          'NFL'),
    ('NBA',          'NBA'),
    ('MLB',          'MLB'),
    ('UFC',          'UFC/MMA'),
    ('MMA',          'UFC/MMA'),
    ('PGATOUR',      'Golf'),
    ('PGA',          'Golf'),
    ('LIV',          'Golf'),
    ('TENNIS',       'Tennis'),
    ('ATP',          'Tennis'),
    ('WTA',          'Tennis'),
    ('SOCCER',       'Soccer'),
    ('EPL',          'Soccer'),
    ('MLS',          'Soccer'),
    ('UCL',          'Soccer'),
    ('F1',           'Formula 1'),
    ('NASCAR',       'NASCAR'),
    ('ESPORTS',      'Esports'),
    ('CSGO',         'Esports'),
    ('LOL',          'Esports'),
    ('VALORANT',     'Esports'),
]

# (substring, pretty_name) — applied to the ticker remainder after the sport
BET_TYPE_PATTERNS = [
    ('1HSPREAD',     '1H Spread'),
    ('2HSPREAD',     '2H Spread'),
    ('1HTOTAL',      '1H Total'),
    ('2HTOTAL',      '2H Total'),
    ('1QSPREAD',     '1Q Spread'),
    ('SPREAD',       'Spread'),
    ('TOTAL',        'Total'),
    ('PARLAY',       'Parlay'),
    ('MULTIGAME',    'Parlay'),
    ('BUNDLE',       'Parlay'),
    ('MW',           'Tournament Winner'),
    ('MVP',          'MVP/Award'),
    ('GOAL',         'Prop'),
    ('POINT',        'Prop'),
    ('ASSIST',       'Prop'),
    ('TOUCHDOWN',    'Prop'),
    ('YARD',         'Prop'),
    ('STRIKEOUT',    'Prop'),
    ('REBOUND',      'Prop'),
    ('GAME',         'Moneyline'),
    ('MONEYLINE',    'Moneyline'),
]

# Sports where, if no other bet type matched, the trade is treated as an
# outright/futures bet on a player or team to win the event.
OUTRIGHT_SPORTS = {'Golf', 'Tennis', 'NASCAR', 'Formula 1', 'UFC/MMA'}


def parse_ticker(ticker):
    """Returns (sport, bet_type) inferred from a Kalshi ticker.

    Falls back to ('Other', 'Other') for tickers we don't recognize so the
    caller never has to handle None.
    """
    if not ticker:
        return ('Other', 'Other')

    t = ticker.upper()
    body = t[2:] if t.startswith('KX') else t
    first_seg = body.split('-')[0]

    # Multi-market vector events (KXMVE...) and Market Vectors (KXMV...) are
    # almost always parlays / futures rather than single-game wagers.
    if first_seg.startswith('MVE'):
        if 'CROSSCATEGORY' in first_seg:
            return ('Multi-Sport', 'Cross-Category Parlay')
        if 'ESPORTS' in first_seg:
            return ('Esports', 'Parlay')
        for sp_key, sp_name in SPORT_PATTERNS:
            if sp_key in first_seg:
                return (sp_name, 'Parlay')
        return ('Multi-Sport', 'Parlay')

    if first_seg.startswith('MV'):
        # Market Vector: typically futures / season-long markets.
        for sp_key, sp_name in SPORT_PATTERNS:
            if sp_key in first_seg:
                return (sp_name, 'Futures')
        return ('Other', 'Futures')

    # Standard sport markets — find the league prefix, then look at the
    # remainder for the bet type.
    sport = 'Other'
    remainder = first_seg
    for sp_key, sp_name in SPORT_PATTERNS:
        if sp_key in first_seg:
            sport = sp_name
            # Strip the matched key from wherever it appears so the bet-type
            # scan only sees the remainder.
            idx = first_seg.find(sp_key)
            remainder = first_seg[:idx] + first_seg[idx + len(sp_key):]
            break

    bet_type = None
    for bt_key, bt_name in BET_TYPE_PATTERNS:
        if bt_key in remainder:
            bet_type = bt_name
            break

    if not bet_type:
        if sport in OUTRIGHT_SPORTS:
            bet_type = 'Outright Winner'
        else:
            bet_type = 'Other'

    return (sport, bet_type)


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

def parse_date(created_time):
    """Robust ISO datetime parser. Handles Kalshi's variable-length microseconds."""
    if not created_time:
        return None
    try:
        s = str(created_time).replace('Z', '+00:00')
        if '.' in s:
            dot_idx = s.index('.')
            end_idx = dot_idx + 1
            while end_idx < len(s) and s[end_idx].isdigit():
                end_idx += 1
            frac = s[dot_idx + 1:end_idx]
            frac_normalized = frac[:6].ljust(6, '0')
            s = s[:dot_idx + 1] + frac_normalized + s[end_idx:]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fill field accessors
# ---------------------------------------------------------------------------

def _f(d, key, default=None):
    """Safe getter for either dict fills or SDK objects."""
    if isinstance(d, dict):
        return d.get(key, default)
    return getattr(d, key, default)


def fill_count(fill):
    """Return contract count as a float; prefers count_fp."""
    raw = _f(fill, 'count_fp') or _f(fill, 'count') or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def fill_price(fill):
    """Return per-contract price in dollars (0..1).

    Prefers the side-specific *_price_dollars field, then price_dollars, then
    legacy 'price' which was in cents. Does NOT apply the old
    'if price > 1.0 divide by 100' heuristic — that broke on legit $1.00 fills.
    """
    side = (_f(fill, 'side') or 'yes').lower()
    side_field = 'yes_price_dollars' if side == 'yes' else 'no_price_dollars'
    raw = _f(fill, side_field)
    if raw in (None, ''):
        raw = _f(fill, 'price_dollars')
    if raw in (None, ''):
        legacy = _f(fill, 'price')
        if legacy in (None, ''):
            return 0.0
        try:
            return float(legacy) / 100.0
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def fill_action(fill):
    """'buy' or 'sell'. Defaults to buy if absent (older API responses)."""
    a = (_f(fill, 'action') or 'buy').lower()
    return 'sell' if a == 'sell' else 'buy'


# ---------------------------------------------------------------------------
# Settlement classification
# ---------------------------------------------------------------------------

def classify_position(market, side, net_contracts):
    """
    Decide settlement status of the contracts STILL HELD at settlement.
    Returns (status, payout) where:
      status  in {'won','lost','open','void','unknown'}
      payout  is the $ value of the winning contracts (Kalshi pays $1 each)

    Only applied to the net contracts remaining after sells.
    """
    if not market:
        return ('unknown', 0.0)

    status = (market.get('status') or '').lower()
    result = (market.get('result') or '').lower()

    # Open / pre-settlement
    if status in ('initialized', 'active', 'open', 'unopened'):
        return ('open', 0.0)

    # Voided / no contest — capital is refunded
    if result in ('void', 'no_contest', 'no contest', 'unresolved', 'cancelled', 'canceled'):
        return ('void', 0.0)

    # Settled with a clear outcome
    if status in ('closed', 'settled', 'finalized', 'determined'):
        if not result:
            # Settled but no result string — treat as void (refund) rather than
            # silently dropping cost into the loss column.
            return ('void', 0.0)
        if result == side.lower():
            return ('won', net_contracts * 1.0)
        return ('lost', 0.0)

    return ('unknown', 0.0)


# ---------------------------------------------------------------------------
# Main fetcher
# ---------------------------------------------------------------------------

def fetch_and_save_data():
    api_key_id = os.environ.get('KALSHI_API_KEY_ID')
    private_key = os.environ.get('KALSHI_PRIVATE_KEY')
    if not api_key_id or not private_key:
        raise ValueError("Missing credentials: KALSHI_API_KEY_ID and/or KALSHI_PRIVATE_KEY not set")

    private_key = private_key.replace('\\n', '\n')
    print("Connecting to Kalshi API...")

    # We rely on raw requests + manual signing; the SDK is configured only so
    # that future code (e.g. order placement) could share the client.
    config = kalshi_python.Configuration()
    config.host = 'https://api.elections.kalshi.com/trade-api/v2'
    config.api_key_id = api_key_id
    config.private_key_pem = private_key
    _ = kalshi_python.KalshiClient(config)  # noqa: F841 — kept for parity
    print("Connected!")

    import base64
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.backends import default_backend

    def sign_request(method, path):
        timestamp_ms = str(int(time.time() * 1000))
        path_no_query = path.split('?')[0]
        msg = (timestamp_ms + method + path_no_query).encode('utf-8')
        key = serialization.load_pem_private_key(
            private_key.encode(), password=None, backend=default_backend()
        )
        sig = key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            'Content-Type': 'application/json',
            'KALSHI-ACCESS-KEY': api_key_id,
            'KALSHI-ACCESS-TIMESTAMP': timestamp_ms,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
        }

    def get_raw_fills(cursor=None):
        path = '/trade-api/v2/portfolio/fills'
        params = 'limit=100'
        if cursor:
            params += f'&cursor={cursor}'
        url = f'https://api.elections.kalshi.com{path}?{params}'
        try:
            r = requests.get(url, headers=sign_request('GET', path), timeout=15)
            if r.status_code == 200:
                data = r.json()
                return data.get('fills', []), data.get('cursor')
            print(f"  HTTP {r.status_code} fetching fills: {r.text[:200]}")
            return [], None
        except Exception as e:
            print(f"  Request error fetching fills: {e}")
            return [], None

    def get_raw_market(ticker):
        path = f'/trade-api/v2/markets/{ticker}'
        url = f'https://api.elections.kalshi.com{path}'
        try:
            r = requests.get(url, headers=sign_request('GET', path), timeout=15)
            if r.status_code == 200:
                return r.json().get('market', {})
            print(f"    HTTP {r.status_code} for {ticker}")
            return None
        except Exception as e:
            print(f"    Request error for {ticker}: {e}")
            return None

    # --- 1. Pull every fill, paginate until cursor is None --------------
    all_fills = []
    cursor = None
    page = 1
    max_pages = 200  # safety cap; 200 * 100 = 20k fills
    while page <= max_pages:
        print(f"  Fetching fills page {page}...")
        fills, next_cursor = get_raw_fills(cursor)
        if fills:
            all_fills.extend(fills)
        # Keep going only if Kalshi explicitly returned another cursor.
        if not next_cursor:
            break
        cursor = next_cursor
        page += 1
    print(f"Pulled {len(all_fills)} raw fills across {page} page(s)")

    # --- 2. Group fills into positions keyed by (ticker, side) ----------
    # A "position" is the user's entire exposure to one side of one market,
    # regardless of how many partial fills it took to build.
    positions = defaultdict(lambda: {
        'ticker': None,
        'side': None,
        'sport': None,
        'bet_type': None,
        'fills_count': 0,
        'buy_count': 0.0,
        'buy_cost': 0.0,
        'sell_count': 0.0,
        'sell_proceeds': 0.0,
        'first_fill': None,   # earliest created_time
        'last_fill': None,    # latest created_time
    })

    for fill in all_fills:
        ticker = _f(fill, 'ticker') or _f(fill, 'market_ticker') or ''
        side = (_f(fill, 'side') or 'yes').lower()
        if not ticker:
            continue

        key = (ticker, side)
        pos = positions[key]
        if pos['ticker'] is None:
            sport, bet_type = parse_ticker(ticker)
            pos['ticker'] = ticker
            pos['side'] = side
            pos['sport'] = sport
            pos['bet_type'] = bet_type

        count = fill_count(fill)
        price = fill_price(fill)
        action = fill_action(fill)
        pos['fills_count'] += 1

        if action == 'buy':
            pos['buy_count'] += count
            pos['buy_cost'] += price * count
        else:
            pos['sell_count'] += count
            pos['sell_proceeds'] += price * count

        ft = parse_date(_f(fill, 'created_time'))
        if ft:
            if pos['first_fill'] is None or ft < pos['first_fill']:
                pos['first_fill'] = ft
            if pos['last_fill'] is None or ft > pos['last_fill']:
                pos['last_fill'] = ft

    print(f"Grouped into {len(positions)} positions")

    # --- 3. Look up each unique market once ------------------------------
    market_cache = {}
    unique_tickers = {p['ticker'] for p in positions.values()}
    for i, ticker in enumerate(sorted(unique_tickers)):
        if (i + 1) % 20 == 0:
            print(f"  market lookup {i + 1}/{len(unique_tickers)}")
        market_cache[ticker] = get_raw_market(ticker)
        time.sleep(0.05)

    # --- 4. Resolve each position into a denormalized trade record ------
    trades = []
    for (ticker, side), pos in positions.items():
        net_contracts = pos['buy_count'] - pos['sell_count']
        # Floating-point safety: contracts should be integers; treat tiny
        # residuals as zero.
        if abs(net_contracts) < 1e-6:
            net_contracts = 0.0

        market = market_cache.get(ticker)

        # Realized proceeds from any sells already happened.
        realized_proceeds = pos['sell_proceeds']

        if net_contracts > 0:
            status, settle_payout = classify_position(market, side, net_contracts)
        else:
            # User fully exited before settlement — purely realized P/L.
            settle_payout = 0.0
            if pos['sell_proceeds'] > pos['buy_cost']:
                status = 'won'
            elif pos['sell_proceeds'] < pos['buy_cost']:
                status = 'lost'
            else:
                status = 'void'  # break-even exit — bucket as void

        gross_cost = pos['buy_cost']
        # For void / refund cases the user gets their net cost back on the
        # contracts they still held.
        if status == 'void' and net_contracts > 0:
            settle_payout = net_contracts * (gross_cost / pos['buy_count']) if pos['buy_count'] else 0.0

        total_proceeds = realized_proceeds + settle_payout
        # For open positions, P/L is unrealized — report 0 so headline numbers
        # don't treat live capital as a loss. The cost is still tracked
        # separately as open_exposure.
        if status == 'open':
            profit = 0.0
        else:
            profit = total_proceeds - gross_cost

        weighted_buy_price = (gross_cost / pos['buy_count']) if pos['buy_count'] else 0.0
        first_fill = pos['first_fill'] or pos['last_fill']
        trade_date_iso = first_fill.isoformat() if first_fill else None

        trades.append({
            'ticker': ticker,
            'side': side,
            'sport': pos['sport'],
            'bet_type': pos['bet_type'],
            'count': pos['buy_count'],            # gross contracts bought
            'price_dollars': round(weighted_buy_price, 4),
            'cost': round(gross_cost, 2),         # gross dollars staked
            'fills_count': pos['fills_count'],
            'sell_proceeds': round(realized_proceeds, 2),
            'settle_payout': round(settle_payout, 2),
            'payout': round(total_proceeds, 2),   # back-compat alias
            'profit': round(profit, 2),
            'net_contracts': round(net_contracts, 4),
            'outcome_status': status,
            'trade_date': trade_date_iso,
            'month': first_fill.strftime('%b %Y') if first_fill else 'Unknown',
            'month_sort': first_fill.strftime('%Y-%m') if first_fill else 'Unknown',
        })

    # --- 5. Aggregate ----------------------------------------------------
    def summarize(items):
        won = sum(1 for t in items if t['outcome_status'] == 'won')
        lost = sum(1 for t in items if t['outcome_status'] == 'lost')
        open_ = sum(1 for t in items if t['outcome_status'] == 'open')
        void = sum(1 for t in items if t['outcome_status'] == 'void')
        cost = sum(t['cost'] for t in items)
        profit = sum(t['profit'] for t in items)
        payout = sum(t['payout'] for t in items)
        win_rate = (won / (won + lost) * 100) if (won + lost) > 0 else 0.0
        roi = (profit / cost * 100) if cost > 0 else 0.0
        avg = (cost / len(items)) if items else 0.0
        return {
            'trades': len(items),
            'won': won,
            'lost': lost,
            'open': open_,
            'void': void,
            'win_rate': round(win_rate, 1),
            'invested': round(cost, 2),
            'profit': round(profit, 2),
            'payout': round(payout, 2),
            'roi': round(roi, 1),
            'avg_bet': round(avg, 2),
        }

    settled_trades = [t for t in trades if t['outcome_status'] in ('won', 'lost', 'void')]
    open_trades = [t for t in trades if t['outcome_status'] == 'open']
    open_exposure = round(sum(t['cost'] for t in open_trades), 2)

    overall = summarize(trades)
    settled = summarize(settled_trades)

    by_sport = defaultdict(list)
    by_bet_type = defaultdict(list)
    by_month = defaultdict(list)
    for t in trades:
        by_sport[t['sport']].append(t)
        by_bet_type[t['bet_type']].append(t)
        if t['month_sort'] != 'Unknown':
            by_month[t['month_sort']].append(t)

    def stats_for_groups(groups, key_label):
        out = []
        for label, items in groups.items():
            s = summarize(items)
            s[key_label] = label
            s['trades_list'] = sorted(
                items, key=lambda x: x.get('trade_date') or '', reverse=True
            )
            out.append(s)
        # Sort by net profit descending — keeps "doing best" at the top.
        out.sort(key=lambda x: x['profit'], reverse=True)
        return out

    sport_stats = stats_for_groups(by_sport, 'sport')
    bet_type_stats = stats_for_groups(by_bet_type, 'bet_type')

    month_stats = []
    for ms in sorted(by_month.keys()):
        items = by_month[ms]
        s = summarize(items)
        s['month_sort'] = ms
        s['month'] = items[0].get('month', ms) if items else ms
        s['trades_list'] = sorted(
            items, key=lambda x: x.get('trade_date') or '', reverse=True
        )
        month_stats.append(s)

    # Recent 7 days uses each position's first fill timestamp.
    seven_days_ago = datetime.now(timezone.utc) - timedelta(days=7)
    recent = []
    for t in trades:
        td = parse_date(t.get('trade_date'))
        if td and td >= seven_days_ago:
            recent.append(t)
    recent_summary = summarize(recent)
    recent_summary['trades_list'] = sorted(
        recent, key=lambda x: x.get('trade_date') or '', reverse=True
    )[:20]

    now_utc = datetime.now(timezone.utc)
    summary = {
        # Headline numbers shown across the top of the dashboard. Settled-only
        # so live exposure doesn't make ROI look catastrophic.
        'total_trades': overall['trades'],
        'won': overall['won'],
        'lost': overall['lost'],
        'open': overall['open'],
        'void': overall['void'],
        'win_rate': overall['win_rate'],
        'settled_invested': settled['invested'],
        'settled_payout': settled['payout'],
        'settled_profit': settled['profit'],
        'settled_roi': settled['roi'],
        'open_exposure': open_exposure,
        # Back-compat aliases used by the old UI before this commit.
        'total_invested': overall['invested'],
        'total_payout': overall['payout'],
        'total_profit': overall['profit'],
        'roi': settled['roi'],  # IMPORTANT: now means SETTLED roi
        'avg_bet': overall['avg_bet'],
        'fees_note': 'Trading fees are not yet subtracted from P/L.',
    }

    all_sorted = sorted(trades, key=lambda x: x.get('trade_date') or '', reverse=True)

    data = {
        'generated_at': now_utc.isoformat(),
        'generated_at_display': now_utc.strftime('%B %d, %Y at %I:%M %p UTC'),
        'summary': summary,
        'recent_7_days': recent_summary,
        'open_trades': sorted(
            open_trades, key=lambda x: x.get('trade_date') or '', reverse=True
        ),
        'by_sport': sport_stats,
        'by_bet_type': bet_type_stats,
        'by_month': month_stats,
        'all_trades': all_sorted,
    }

    with open('data.json', 'w') as f:
        json.dump(data, f, indent=2, default=str)

    print(
        f"\nDone! "
        f"{overall['trades']} positions ({overall['won']}W-{overall['lost']}L"
        f"-{overall['open']}O-{overall['void']}V) | "
        f"Settled P/L ${settled['profit']:+,.2f} ROI {settled['roi']:+.1f}% | "
        f"Open exposure ${open_exposure:,.2f}"
    )


if __name__ == "__main__":
    fetch_and_save_data()

#"Rewrite data layer: positions, bet types, settled ROI"
