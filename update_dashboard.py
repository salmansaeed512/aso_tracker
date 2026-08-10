#!/usr/bin/env python3
"""
ASO Tracker Dashboard Automation
- Daily: fetches sign-ups/eFTDs/eFTTs from bydata → updates HTML → pushes to GitHub
- Mon/Wed: fetches ASC download data via App Store Connect API
- Manual: reads Google Play CSVs from /tmp/aso_tracker/gp_data/

Data convention: HTML always uses w30/w31 as prev/curr property names.
Labels are updated to show actual week numbers.

Requirements: pip install requests PyJWT cryptography
"""

import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_DIR = Path(__file__).parent
HTML_PATH = REPO_DIR / "index.html"
GP_DIR = REPO_DIR / "gp_data"

# --- Bydata Config ---
BYDATA_URL = "http://grc-ai-eaa-proxy.yijin.io:8080/api/v1/mcps/bydata-mcp/mcp"
BYDATA_API_KEY = os.environ.get("BYDATA_API_KEY", "e1a2b3f159bf4d438b0ce66634218804")

# --- ASC Config ---
ASC_ISSUER_ID = os.environ.get("ASC_ISSUER_ID", "bd1061b9-c226-4567-b145-d2d4fa94f5b0")
ASC_KEY_ID = os.environ.get("ASC_KEY_ID", "NV4PF67DWF")
ASC_KEY_PATH = os.environ.get("ASC_KEY_PATH", "/Users/pk00619ml/Downloads/AuthKey_NV4PF67DWF.p8")
ASC_APP_ID = "1488296980"

# --- GitHub ---
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

SITES = ['BYBIT', 'EU', 'IDN', 'GEO', 'KAZ', 'TUR']
SITE_COUNTRIES = {'IDN': 'ID', 'GEO': 'GE', 'KAZ': 'KZ', 'TUR': 'TR'}
FILTERS = """
  AND regist_channel = 'App'
  AND signup_low_user_flag = 0
  AND user_tag_brief IN ('RC','API','INS')
  AND user_status = 'Normal'
"""


# ============================================================
# BYDATA (MCP over HTTP with session)
# ============================================================

_bydata_session_id = None
_bydata_req_id = 0


def _init_bydata_session():
    global _bydata_session_id
    import requests
    headers = {"Content-Type": "application/json", "X-API-KEY": BYDATA_API_KEY}
    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "aso-tracker", "version": "1.0"}
        }
    }
    resp = requests.post(BYDATA_URL, json=init_payload, headers=headers, timeout=30)
    resp.raise_for_status()
    _bydata_session_id = resp.headers.get("Mcp-Session-Id")
    if not _bydata_session_id:
        raise Exception("No Mcp-Session-Id in initialize response")
    # Send initialized notification
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    requests.post(BYDATA_URL, json=notif,
                  headers={**headers, "Mcp-Session-Id": _bydata_session_id}, timeout=10)


def query_bydata(sql):
    global _bydata_session_id, _bydata_req_id
    import requests

    if not _bydata_session_id:
        _init_bydata_session()

    _bydata_req_id += 1
    payload = {
        "jsonrpc": "2.0", "id": _bydata_req_id,
        "method": "tools/call",
        "params": {"name": "bydata_sync_query", "arguments": {"sql": sql}}
    }
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": BYDATA_API_KEY,
        "Mcp-Session-Id": _bydata_session_id
    }
    resp = requests.post(BYDATA_URL, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    result = resp.json()
    if "error" in result:
        raise Exception(f"Bydata error: {result['error']}")
    content = result.get("result", {}).get("content", [])
    for item in content:
        if item.get("type") == "resource":
            return parse_csv_text(item.get("resource", {}).get("text", ""))
    return []


def parse_csv_text(text):
    lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if not lines:
        return []
    headers = lines[0].split(',')
    rows = []
    for line in lines[1:]:
        vals = line.split(',')
        row = {}
        for i, h in enumerate(headers):
            v = vals[i] if i < len(vals) else ''
            try:
                v = float(v)
                if v == int(v):
                    v = int(v)
            except (ValueError, OverflowError):
                pass
            row[h] = v
        rows.append(row)
    return rows


def get_week_boundaries(ref_date=None):
    """Calculate prev and curr week boundaries relative to a reference date.
    If today is Mon-Sun of week N, curr = week N-1 (most recent completed week).
    Week numbering: W1 starts first Monday of the year (Jan 5, 2026).
    """
    if ref_date is None:
        ref_date = datetime.now()
    dow = ref_date.weekday()
    this_mon = ref_date - timedelta(days=dow)
    curr_mon = this_mon - timedelta(days=7)
    curr_sun = this_mon - timedelta(days=1)
    prev_mon = curr_mon - timedelta(days=7)
    prev_sun = curr_mon - timedelta(days=1)
    # Week number: first Monday of 2026 = Jan 5. Count weeks from there.
    jan1 = datetime(ref_date.year, 1, 1)
    first_mon = jan1 + timedelta(days=(7 - jan1.weekday()) % 7)
    curr_wk = (curr_mon - first_mon).days // 7 + 1
    prev_wk = (prev_mon - first_mon).days // 7 + 1
    return {
        'curr_start': curr_mon.strftime('%Y-%m-%d'),
        'curr_end': curr_sun.strftime('%Y-%m-%d'),
        'prev_start': prev_mon.strftime('%Y-%m-%d'),
        'prev_end': prev_sun.strftime('%Y-%m-%d'),
        'curr_week': curr_wk,
        'prev_week': prev_wk,
    }


def get_latest_dt():
    rows = query_bydata(
        "SELECT MAX(dt) as latest_dt FROM bridge.ads_user_convert_funnel_extend_df "
        "WHERE dt >= DATE_SUB(CURDATE(), INTERVAL 3 DAY)"
    )
    return rows[0]['latest_dt'] if rows else None


def fetch_country_data(dt, week):
    sql = f"""
    SELECT site_id, regist_platform as platform, user_country as country,
      SUM(CASE WHEN di >= '{week['curr_start']}' AND di <= '{week['curr_end']}' THEN signup ELSE 0 END) as su_curr,
      SUM(CASE WHEN di >= '{week['prev_start']}' AND di <= '{week['prev_end']}' THEN signup ELSE 0 END) as su_prev,
      SUM(CASE WHEN di >= '{week['curr_start']}' AND di <= '{week['curr_end']}' THEN eftd_cnt ELSE 0 END) as ed_curr,
      SUM(CASE WHEN di >= '{week['prev_start']}' AND di <= '{week['prev_end']}' THEN eftd_cnt ELSE 0 END) as ed_prev,
      SUM(CASE WHEN di >= '{week['curr_start']}' AND di <= '{week['curr_end']}' THEN eftt ELSE 0 END) as et_curr,
      SUM(CASE WHEN di >= '{week['prev_start']}' AND di <= '{week['prev_end']}' THEN eftt ELSE 0 END) as et_prev
    FROM bridge.ads_user_convert_funnel_extend_df
    WHERE dt = '{dt}'
      AND di >= '{week['prev_start']}' AND di <= '{week['curr_end']}'
      AND site_id IN ('{"','".join(SITES)}')
      AND regist_platform IN ('ios','android')
      {FILTERS}
    GROUP BY site_id, regist_platform, user_country
    ORDER BY site_id, regist_platform, su_curr DESC
    """
    return query_bydata(sql)


# ============================================================
# ASC (App Store Connect) API
# ============================================================

def generate_asc_token():
    import jwt
    key_path = Path(ASC_KEY_PATH)
    if not key_path.exists():
        print(f"  WARN: ASC key not found at {ASC_KEY_PATH}")
        return None
    private_key = key_path.read_text()
    now = int(time.time())
    payload = {
        "iss": ASC_ISSUER_ID,
        "iat": now,
        "exp": now + 1200,
        "aud": "appstoreconnect-v1"
    }
    token = jwt.encode(payload, private_key, algorithm="ES256", headers={"kid": ASC_KEY_ID})
    return token


def fetch_asc_downloads(week):
    """Fetch per-country download data from ASC Analytics."""
    import requests

    token = generate_asc_token()
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}"}
    base = "https://api.appstoreconnect.apple.com"

    # Request analytics report for downloads by source/country
    # ASC Analytics Reports API: request totalDownloads grouped by source, country
    # Date range covers both prev and curr week
    start_date = week['prev_start']
    end_date = week['curr_end']

    # Step 1: Create report request
    report_req = {
        "data": {
            "type": "analyticsReportRequests",
            "attributes": {
                "accessType": "ONGOING"
            },
            "relationships": {
                "app": {
                    "data": {"type": "apps", "id": ASC_APP_ID}
                }
            }
        }
    }
    resp = requests.post(
        f"{base}/v1/analyticsReportRequests",
        json=report_req, headers={**headers, "Content-Type": "application/json"},
        timeout=60
    )
    if resp.status_code == 409:
        # Report request already exists - list existing
        pass

    # Step 2: List available reports
    resp = requests.get(
        f"{base}/v1/apps/{ASC_APP_ID}/analyticsReportRequests",
        headers=headers, timeout=60
    )
    if resp.status_code != 200:
        print(f"  WARN: ASC reports list failed: {resp.status_code}")
        return None

    reports = resp.json().get("data", [])
    if not reports:
        print("  WARN: No ASC report requests found")
        return None

    # Get the first ongoing report's reports
    report_id = reports[0]["id"]
    resp = requests.get(
        f"{base}/v1/analyticsReportRequests/{report_id}/reports",
        headers=headers, params={"filter[category]": "APP_STORE_ENGAGEMENT"},
        timeout=60
    )
    if resp.status_code != 200:
        print(f"  WARN: ASC report details failed: {resp.status_code}")
        return None

    # Find appDownloads report and get segments
    app_dl_reports = [r for r in resp.json().get("data", []) if r.get("attributes", {}).get("name") == "appDownloads"]
    if not app_dl_reports:
        return None

    dl_report_id = app_dl_reports[0]["id"]
    resp = requests.get(
        f"{base}/v1/analyticsReports/{dl_report_id}/instances",
        headers=headers,
        params={"filter[granularity]": "DAILY", "filter[processingDate]": end_date},
        timeout=60
    )
    if resp.status_code != 200:
        return None

    instances = resp.json().get("data", [])
    if not instances:
        return None

    # Download the segment data
    segments_url = instances[0].get("relationships", {}).get("segments", {}).get("links", {}).get("related")
    if not segments_url:
        return None

    resp = requests.get(segments_url, headers=headers, timeout=60)
    if resp.status_code != 200:
        return None

    segments = resp.json().get("data", [])
    # Parse into country/source breakdown
    downloads = {}
    for seg in segments:
        attrs = seg.get("attributes", {})
        url = attrs.get("url")
        if url:
            data_resp = requests.get(url, timeout=120)
            if data_resp.status_code == 200:
                return parse_asc_tsv(data_resp.text, week)

    return None


def parse_asc_tsv(tsv_text, week):
    """Parse ASC download TSV into per-country download data."""
    lines = tsv_text.strip().split('\n')
    if len(lines) < 2:
        return {}
    headers = lines[0].split('\t')
    date_idx = headers.index('Date') if 'Date' in headers else 0
    country_idx = headers.index('Territory') if 'Territory' in headers else -1
    source_idx = headers.index('Source Type') if 'Source Type' in headers else -1
    dl_idx = headers.index('Total Downloads') if 'Total Downloads' in headers else -1

    if any(i == -1 for i in [country_idx, source_idx, dl_idx]):
        return {}

    prev_start = datetime.strptime(week['prev_start'], '%Y-%m-%d')
    prev_end = datetime.strptime(week['prev_end'], '%Y-%m-%d')
    curr_start = datetime.strptime(week['curr_start'], '%Y-%m-%d')
    curr_end = datetime.strptime(week['curr_end'], '%Y-%m-%d')

    countries = {}
    for line in lines[1:]:
        cols = line.split('\t')
        if len(cols) <= max(date_idx, country_idx, source_idx, dl_idx):
            continue
        dt = datetime.strptime(cols[date_idx], '%Y-%m-%d')
        country = cols[country_idx]
        source = cols[source_idx].lower()
        dls = int(cols[dl_idx]) if cols[dl_idx].isdigit() else 0

        if country not in countries:
            countries[country] = {
                'search_w30': 0, 'search_w31': 0,
                'browse_w30': 0, 'browse_w31': 0,
                'appRef_w30': 0, 'appRef_w31': 0,
                'webRef_w30': 0, 'webRef_w31': 0,
            }

        period = None
        if prev_start <= dt <= prev_end:
            period = 'w30'
        elif curr_start <= dt <= curr_end:
            period = 'w31'
        else:
            continue

        if 'search' in source:
            countries[country][f'search_{period}'] += dls
        elif 'browse' in source or 'feature' in source:
            countries[country][f'browse_{period}'] += dls
        elif 'app referral' in source:
            countries[country][f'appRef_{period}'] += dls
        elif 'web referral' in source:
            countries[country][f'webRef_{period}'] += dls

    # Build final array sorted by total w31 downloads
    result = []
    for country, data in countries.items():
        total_w30 = data['search_w30'] + data['browse_w30'] + data['appRef_w30'] + data['webRef_w30']
        total_w31 = data['search_w31'] + data['browse_w31'] + data['appRef_w31'] + data['webRef_w31']
        if total_w30 + total_w31 > 50:
            result.append({
                'country': country, 'w30': total_w30, 'w31': total_w31, **data
            })
    result.sort(key=lambda x: x['w31'], reverse=True)
    return result


# ============================================================
# Google Play CSV Reader
# ============================================================

def read_gp_csvs():
    """Read Google Play CSV exports from gp_data/ folder.
    Expected file: store_performance_country.csv with columns:
    Country, Store Listing Visitors, Store Listing Acquisitions, etc.
    """
    if not GP_DIR.exists():
        GP_DIR.mkdir(parents=True, exist_ok=True)
        print(f"  Created {GP_DIR} — drop Google Play CSVs here")
        return None

    csv_files = sorted(GP_DIR.glob("*.csv"))
    if not csv_files:
        print(f"  No CSV files in {GP_DIR}")
        return None

    latest = csv_files[-1]
    print(f"  Reading GP data from: {latest.name}")
    rows = []
    with open(latest, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


# ============================================================
# HTML Update
# ============================================================

def fmt_entry(country, prev, curr):
    return f'{{country:"{country}",w30:{int(prev)},w31:{int(curr)}}}'


def fmt_array(rows, limit=40):
    """Format rows as JS array entries. Each row must have .country, .prev, .curr"""
    entries = [fmt_entry(r['country'], r['prev'], r['curr']) for r in rows[:limit]]
    lines = []
    for i in range(0, len(entries), 3):
        lines.append(','.join(entries[i:i+3]))
    return ',\n'.join(lines)


def replace_array_in_html(html, block_marker, end_marker, array_name, new_content):
    """Replace an array within a bounded region of the HTML."""
    block_idx = html.find(block_marker)
    if block_idx == -1:
        print(f"  WARN: Could not find block marker: {block_marker}")
        return html
    end_idx = html.find(end_marker, block_idx + len(block_marker))
    if end_idx == -1:
        end_idx = len(html)

    arr_idx = html.find(f'{array_name}: [', block_idx)
    if arr_idx == -1 or arr_idx > end_idx:
        print(f"  WARN: Could not find {array_name} in region")
        return html

    bracket_start = html.index('[', arr_idx)
    depth, i = 0, bracket_start
    while i < len(html):
        if html[i] == '[':
            depth += 1
        if html[i] == ']':
            depth -= 1
            if depth == 0:
                break
        i += 1

    return html[:arr_idx] + f'{array_name}: [\n{new_content}\n]' + html[i + 1:]


def build_country_rows(data, site, platform, metric_curr, metric_prev, limit=40):
    """Filter and format country data for a specific site/platform/metric."""
    filtered = [r for r in data if r['site_id'] == site and r['platform'] == platform]
    rows = [{'country': r['country'], 'prev': r[metric_prev], 'curr': r[metric_curr]}
            for r in filtered if r[metric_curr] > 0 or r[metric_prev] > 0]
    rows.sort(key=lambda x: x['curr'], reverse=True)
    return rows[:limit]


def update_html_data(wow_data, week, asc_downloads=None):
    """Update the HTML dashboard with new data."""
    html = HTML_PATH.read_text()

    wn = week['curr_week']
    wp = week['prev_week']
    curr_start = datetime.strptime(week['curr_start'], '%Y-%m-%d')
    curr_end = datetime.strptime(week['curr_end'], '%Y-%m-%d')

    # --- IOS_DATA: signups, eftds, eftts (bounded by 'downloads: [') ---
    ios_su = build_country_rows(wow_data, 'BYBIT', 'ios', 'su_curr', 'su_prev')
    ios_ed = build_country_rows(wow_data, 'BYBIT', 'ios', 'ed_curr', 'ed_prev')
    ios_et = build_country_rows(wow_data, 'BYBIT', 'ios', 'et_curr', 'et_prev')

    html = replace_array_in_html(html, 'const IOS_DATA', 'downloads: [', 'signups', fmt_array(ios_su))
    html = replace_array_in_html(html, 'const IOS_DATA', 'downloads: [', 'eftds', fmt_array(ios_ed))
    html = replace_array_in_html(html, 'const IOS_DATA', 'downloads: [', 'eftts', fmt_array(ios_et))

    # --- ANDROID_DATA: signups, eftds, eftts ---
    and_su = build_country_rows(wow_data, 'BYBIT', 'android', 'su_curr', 'su_prev')
    and_ed = build_country_rows(wow_data, 'BYBIT', 'android', 'ed_curr', 'ed_prev')
    and_et = build_country_rows(wow_data, 'BYBIT', 'android', 'et_curr', 'et_prev')

    android_end = 'const SITE_DATA'
    html = replace_array_in_html(html, 'const ANDROID_DATA', android_end, 'signups', fmt_array(and_su))
    html = replace_array_in_html(html, 'const ANDROID_DATA', android_end, 'eftds', fmt_array(and_ed))
    html = replace_array_in_html(html, 'const ANDROID_DATA', android_end, 'eftts', fmt_array(and_et))

    # --- SITE_DATA: Rebuild entire block ---
    def build_site_block(site, wow_data):
        """Build a complete site JS object."""
        lines = []
        for platform in ['ios', 'android']:
            su = build_country_rows(wow_data, site, platform, 'su_curr', 'su_prev')
            ed = build_country_rows(wow_data, site, platform, 'ed_curr', 'ed_prev')
            et = build_country_rows(wow_data, site, platform, 'et_curr', 'et_prev')
            su_str = ','.join(fmt_entry(r['country'], r['prev'], r['curr']) for r in su)
            ed_str = ','.join(fmt_entry(r['country'], r['prev'], r['curr']) for r in ed)
            et_str = ','.join(fmt_entry(r['country'], r['prev'], r['curr']) for r in et)
            lines.append(f'  {platform}: {{ signups: [{su_str}], eftds: [{ed_str}], eftts: [{et_str}] }}')
        return ',\n'.join(lines)

    site_blocks = []
    for site in ['EU', 'IDN', 'GEO', 'KAZ', 'TUR']:
        block = build_site_block(site, wow_data)
        site_blocks.append(f'{site}: {{\n{block}\n}}')

    new_site_data = 'const SITE_DATA = {\n' + ',\n'.join(site_blocks) + '\n};'

    # Replace the entire SITE_DATA block
    site_start = html.find('const SITE_DATA = {')
    if site_start != -1:
        # Find the closing }; — search for "\n};" after the opening
        search_from = site_start + 20
        depth = 1
        i = html.index('{', site_start) + 1
        while i < len(html) and depth > 0:
            if html[i] == '{':
                depth += 1
            elif html[i] == '}':
                depth -= 1
            i += 1
        # i is now just past the closing }, look for the semicolon
        while i < len(html) and html[i] in ' \t\n':
            i += 1
        if i < len(html) and html[i] == ';':
            i += 1
        html = html[:site_start] + new_site_data + html[i:]

    # --- ASC Downloads (if available) ---
    if asc_downloads:
        dl_entries = []
        for row in asc_downloads[:50]:
            entry = (f'  {{\n    country: "{row["country"]}",\n'
                     f'    w30: {row["w30"]},\n    w31: {row["w31"]},\n'
                     f'    search_w30: {row["search_w30"]},\n    search_w31: {row["search_w31"]},\n'
                     f'    browse_w30: {row["browse_w30"]},\n    browse_w31: {row["browse_w31"]},\n'
                     f'    appRef_w30: {row["appRef_w30"]},\n    appRef_w31: {row["appRef_w31"]},\n'
                     f'    webRef_w30: {row["webRef_w30"]},\n    webRef_w31: {row["webRef_w31"]}\n  }}')
            dl_entries.append(entry)
        dl_content = ',\n'.join(dl_entries)
        html = replace_array_in_html(html, 'const IOS_DATA', 'const ANDROID_DATA', 'downloads', dl_content)

    # --- Update week labels ---
    period_str = f'Week {wn} Report ({curr_start.strftime("%b %-d")} – {curr_end.strftime("%b %-d, %Y")})'
    html = re.sub(r"Week \d+ Report \([^)]+\)", period_str, html)
    html = re.sub(r"Week \d+ Dashboard", f"Week {wn} Dashboard", html)

    # Update WoW badge labels (all occurrences including tooltips and chart titles)
    html = re.sub(r"W\d+ vs W\d+", f"W{wp} vs W{wn}", html)
    html = re.sub(r"Top 15 Countries by W\d+ Volume", f"Top 15 Countries by W{wn} Volume", html)

    # Update footnote
    prev_start_dt = datetime.strptime(week['prev_start'], '%Y-%m-%d')
    prev_end_dt = datetime.strptime(week['prev_end'], '%Y-%m-%d')
    new_footnote = (f'Downloads use ASC dates (W{wp}: {prev_start_dt.strftime("%b %-d")}–'
                    f'{prev_end_dt.strftime("%b %-d")}, W{wn}: {curr_start.strftime("%b %-d")}–'
                    f'{curr_end.strftime("%-d")}). ASA installs subtracted for pure organic. '
                    f'OA = Other Africa, OZ = Other Asia-Pacific.')
    html = re.sub(r"Downloads use ASC dates \([^)]+\)\.[^.]+\.[^.]+\.", new_footnote, html)

    # Update KPI label defaults
    html = re.sub(r"const wPrev = wLabels \? wLabels\[0\] : 'W\d+'",
                  f"const wPrev = wLabels ? wLabels[0] : 'W{wp}'", html)
    html = re.sub(r"const wCurr = wLabels \? wLabels\[1\] : 'W\d+'",
                  f"const wCurr = wLabels ? wLabels[1] : 'W{wn}'", html)

    # Update GP Console "not yet available" label
    html = re.sub(r"W\d+ not yet available; showing W\d+ vs W\d+",
                  f"W{wn} not yet available; showing W{wp-2} vs W{wp-1}", html)
    html = re.sub(r"Google Play Console · W\d+ vs W\d+",
                  f"Google Play Console · W{wp} vs W{wn}", html)

    HTML_PATH.write_text(html)
    print(f"  Updated HTML → Week {wn} (W{wp} vs W{wn})")


# ============================================================
# Git Push
# ============================================================

def git_push(message):
    os.chdir(REPO_DIR)
    subprocess.run(['git', 'add', 'index.html'], check=True)
    result = subprocess.run(['git', 'diff', '--cached', '--quiet'])
    if result.returncode == 0:
        print("  No changes to commit")
        return False
    subprocess.run(['git', 'commit', '-m', message], check=True)
    subprocess.run(['git', 'push'], check=True)
    print(f"  Pushed: {message}")
    return True


# ============================================================
# Main
# ============================================================

def main():
    today = datetime.now()
    day_name = today.strftime('%A')
    print(f"[{today.strftime('%Y-%m-%d %H:%M')}] ASO Tracker Update ({day_name})")

    # Get latest snapshot date from bydata
    print("  Checking bydata...")
    dt = get_latest_dt()
    if not dt:
        print("  ERROR: No recent snapshot in bydata")
        sys.exit(1)
    print(f"  Latest snapshot: {dt}")

    # Calculate week boundaries
    week = get_week_boundaries()
    wn = week['curr_week']
    wp = week['prev_week']
    print(f"  Reporting: W{wp} vs W{wn} ({week['prev_start']} to {week['curr_end']})")

    # Fetch country-level WoW data
    print("  Fetching sign-ups/eFTDs/eFTTs...")
    wow_data = fetch_country_data(dt, week)
    print(f"  Got {len(wow_data)} country rows")

    # ASC downloads (Mon/Wed only)
    asc_downloads = None
    if day_name in ('Monday', 'Wednesday'):
        print("  Fetching ASC downloads...")
        try:
            asc_downloads = fetch_asc_downloads(week)
            if asc_downloads:
                print(f"  Got {len(asc_downloads)} countries from ASC")
            else:
                print("  WARN: ASC download fetch returned nothing — keeping existing data")
        except Exception as e:
            print(f"  WARN: ASC fetch failed: {e}")

    # Google Play CSVs (check if new file available)
    gp_data = read_gp_csvs()

    # Update HTML
    print("  Updating HTML...")
    update_html_data(wow_data, week, asc_downloads)

    # Git push
    git_push(f"W{wn} update ({today.strftime('%Y-%m-%d')}): sign-ups/eFTDs/eFTTs"
             + (" + ASC downloads" if asc_downloads else ""))

    print("  Done!")


if __name__ == '__main__':
    main()
