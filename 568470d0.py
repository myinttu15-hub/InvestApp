
import os
from datetime import datetime, timedelta, timezone
import pandas as pd
import streamlit as st
import finnhub

st.set_page_config(page_title='Hourly Stock Ranker', layout='wide')

API_KEY = st.secrets.get('FINNHUB_API_KEY', os.getenv('FINNHUB_API_KEY', ''))
if not API_KEY:
    st.error('Missing FINNHUB_API_KEY. Add it to Streamlit secrets or environment variables.')
    st.stop()

client = finnhub.Client(api_key=API_KEY)

DEFAULT_SYMBOLS = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'AMZN', 'META', 'GOOGL', 'AMD', 'NFLX', 'BABA']


def momentum_score(change_pct, side):
    if side == 'Long':
        if change_pct >= 10: return 100
        if change_pct >= 7: return 85
        if change_pct >= 5: return 70
        if change_pct >= 3: return 55
        if change_pct >= 1: return 35
        if change_pct >= 0: return 10
        return 0
    drop = -change_pct
    if drop >= 10: return 100
    if drop >= 7: return 85
    if drop >= 5: return 70
    if drop >= 3: return 55
    if drop >= 1: return 35
    if drop >= 0: return 10
    return 0


def relative_volume_score(rv):
    if rv >= 10: return 100
    if rv >= 7: return 85
    if rv >= 5: return 70
    if rv >= 3: return 55
    if rv >= 2: return 35
    if rv >= 1: return 15
    return 0


def liquidity_score(dollar_vol):
    if dollar_vol >= 50_000_000: return 100
    if dollar_vol >= 20_000_000: return 80
    if dollar_vol >= 10_000_000: return 60
    if dollar_vol >= 5_000_000: return 40
    if dollar_vol >= 2_000_000: return 20
    return 0


def catalyst_score(text):
    t = (text or '').lower()
    rules = [
        ('earnings beat + raised guidance', 100),
        ('fda approval', 100),
        ('major regulatory win', 100),
        ('acquisition', 90),
        ('major contract', 80),
        ('analyst upgrade', 70),
        ('price target raise', 70),
        ('earnings beat', 65),
        ('guidance unchanged', 65),
        ('insider buying', 60),
        ('sector news', 40),
        ('macro tailwind', 40),
        ('rumor', 20),
    ]
    for key, val in rules:
        if key in t:
            return val
    return 0


def risk_penalty(row):
    p = 0
    if row.get('short_interest_pct', 0) > 20: p -= 15
    if row.get('earnings_within_2h', False): p -= 20
    if row.get('std_above_vwap', 0) > 3: p -= 15
    if row.get('market_down_pct', 0) < -1: p -= 10
    if row.get('halted_today_count', 0) > 0: p -= 25
    if row.get('spread_pct', 0) > 1.5: p -= 20
    if row.get('today_change_pct', 0) > 30: p -= 15
    return p


def exclude_reasons(row):
    reasons = []
    if row['price'] < 5: reasons.append('Price under $5')
    if row['dollar_vol'] < 2_000_000: reasons.append('Dollar volume under $2M')
    if row['catalyst_score_raw'] == 0: reasons.append('No catalyst')
    if row.get('halted_today_count', 0) > 1: reasons.append('Halted more than once today')
    if row.get('spread_pct', 0) > 2: reasons.append('Bid-ask spread over 2%')
    if row.get('market_cap', 0) < 300_000_000: reasons.append('Market cap under $300M')
    return '; '.join(reasons)


@st.cache_data(ttl=3600)
def fetch_symbol_data(symbol):
    profile = client.company_profile2(symbol=symbol) or {}
    quote = client.quote(symbol) or {}
    end = int(datetime.now(timezone.utc).timestamp())
    start = int((datetime.now(timezone.utc) - timedelta(days=21)).timestamp())
    candles = client.stock_candles(symbol, '60', start, end) or {}

    price = float(quote.get('c') or 0)
    prev_close = float(quote.get('pc') or 0)
    volume = float(quote.get('v') or 0)
    change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0
    dollar_vol = price * volume
    vols = candles.get('v') or []
    avg_hourly_vol = sum(vols[:-1]) / max(len(vols[:-1]), 1) if vols else 0
    rel_vol = volume / avg_hourly_vol if avg_hourly_vol else 0

    catalyst_text = 'sector news'
    catalyst_raw = catalyst_score(catalyst_text)
    row = {
        'symbol': symbol,
        'price': price,
        'change_pct': change_pct,
        'volume': volume,
        'avg_hourly_vol': avg_hourly_vol,
        'rel_volume': rel_vol,
        'dollar_vol': dollar_vol,
        'market_cap': float(profile.get('marketCapitalization') or 0) * 1_000_000,
        'spread_pct': 0,
        'short_interest_pct': 0,
        'earnings_within_2h': False,
        'std_above_vwap': 0,
        'market_down_pct': 0,
        'halted_today_count': 0,
        'today_change_pct': change_pct,
        'momentum_score': momentum_score(change_pct, 'Long'),
        'volume_score': relative_volume_score(rel_vol),
        'liquidity_score': liquidity_score(dollar_vol),
        'catalyst_score_raw': catalyst_raw,
        'catalyst_score': catalyst_raw,
    }
    row['risk_penalty'] = risk_penalty(row)
    row['final_score'] = round(row['momentum_score'] * 0.40 + row['volume_score'] * 0.30 + row['liquidity_score'] * 0.20 + row['catalyst_score'] * 0.10 + row['risk_penalty'], 2)
    row['exclude_reasons'] = exclude_reasons(row)
    row['eligible'] = (row['exclude_reasons'] == '') and (row['final_score'] >= 50)
    return row


st.title('Hourly Top 10 Stock Ranker')
side = st.sidebar.selectbox('Side', ['Long', 'Short', 'Both'])
raw = st.sidebar.text_area('Universe', ','.join(DEFAULT_SYMBOLS))
symbols = [s.strip().upper() for s in raw.split(',') if s.strip()]
refresh_minutes = st.sidebar.number_input('Refresh minutes', 1, 240, 60)

if st.sidebar.button('Run scan') or 'results' not in st.session_state:
    rows = []
    for sym in symbols:
        try:
            if side == 'Both':
                rows.append(fetch_symbol_data(sym))
                row = fetch_symbol_data(sym).copy()
                row['momentum_score'] = momentum_score(row['change_pct'], 'Short')
                row['final_score'] = round(row['momentum_score'] * 0.40 + row['volume_score'] * 0.30 + row['liquidity_score'] * 0.20 + row['catalyst_score'] * 0.10 + row['risk_penalty'], 2)
                rows.append(row)
            else:
                row = fetch_symbol_data(sym)
                if side == 'Short':
                    row['momentum_score'] = momentum_score(row['change_pct'], 'Short')
                    row['final_score'] = round(row['momentum_score'] * 0.40 + row['volume_score'] * 0.30 + row['liquidity_score'] * 0.20 + row['catalyst_score'] * 0.10 + row['risk_penalty'], 2)
                rows.append(row)
        except Exception as e:
            rows.append({'symbol': sym, 'error': str(e)})
    df = pd.DataFrame(rows)
    if not df.empty and 'final_score' in df.columns:
        st.session_state['results'] = df.sort_values('final_score', ascending=False)
    else:
        st.session_state['results'] = df

out = st.session_state.get('results', pd.DataFrame())
if not out.empty:
    top = out.head(10)
    st.subheader('Top 10')
    st.dataframe(top, use_container_width=True)
    st.subheader('All results')
    st.dataframe(out, use_container_width=True)
