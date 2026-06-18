#!/usr/bin/env python3
"""
Mist WiFi Client Checker — Local POC
Run this on any server with line-of-sight to your WiFi clients.
A client visiting http://<this-server> will see their own live Mist stats.

Configuration: edit config.ini  (env vars override config.ini if both are set)
"""

from flask import Flask, request, render_template_string, jsonify
import requests
import configparser
import os
import re
import time
from datetime import timedelta, datetime
from urllib.parse import quote

app = Flask(__name__)

# ── Configuration — read exclusively from config.ini ─────────────────────────
_cfg = configparser.ConfigParser()
_cfg_path = os.path.join(os.path.dirname(__file__), "config.ini")

if not os.path.exists(_cfg_path):
    raise FileNotFoundError(f"config.ini not found at {_cfg_path}")

_cfg.read(_cfg_path)

try:
    MIST_API_TOKEN = _cfg.get("mist", "api_token")
    ORG_ID         = _cfg.get("mist", "org_id")
    MIST_BASE      = _cfg.get("mist", "base_url")
except configparser.NoSectionError:
    raise ValueError("config.ini is missing the [mist] section.")
except configparser.NoOptionError as e:
    raise ValueError(f"config.ini is missing a required setting: {e}")

# Optional — if set, an Email Support button appears on the dashboard
SUPPORT_EMAIL = _cfg.get("mist", "support_email", fallback="")

# Optional — WiFi info text (displayed on simple view)
WIFI_INFO_TEXT = _cfg.get("mist", "wifi_info_text", fallback=
    "The information shown here is how the WiFi network hears your client. "
    "RSSI (signal strength) and SNR (signal quality) are measured at the access point. "
    "Throughput is the current data rate. Retries indicate packet loss."
)

# Optional — Wired info text (displayed on simple view)
WIRED_INFO_TEXT = _cfg.get("mist", "wired_info_text", fallback=
    "The information shown here is your wired Ethernet connection status. "
    "Link speed is the negotiated port speed. Link status shows if the port is active. "
    "PoE indicates if power is being supplied. Throughput is the current data rate."
)

# Optional — Port number (can also be overridden by PORT environment variable)
APP_PORT = int(_cfg.get("mist", "port", fallback="80"))


# ── Mist API helpers ──────────────────────────────────────────────────────────

def mist_get(path, params=None):
    r = requests.get(
        f"{MIST_BASE}{path}",
        headers={"Authorization": f"Token {MIST_API_TOKEN}"},
        params=params,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def get_client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return xff.split(",")[0].strip() if xff else request.remote_addr


def get_ap_names(site_id):
    """Return {mac_lower: name} for every AP in the site. Never raises."""
    try:
        devices = mist_get(f"/sites/{site_id}/stats/devices", params={"type": "ap"})
        items = devices if isinstance(devices, list) else devices.get("results", [])
        return {
            d["mac"].lower(): d.get("name") or d.get("hostname") or d["mac"].upper()
            for d in items if d.get("mac")
        }
    except Exception:
        return {}


def get_switch_names(site_id):
    """Return {mac_lower: {name, model}} for every switch in the site. Never raises."""
    try:
        result = mist_get(f"/sites/{site_id}/stats/devices", params={"type": "switch"})
        items = result if isinstance(result, list) else result.get("data", result.get("results", []))
        return {
            d["mac"].lower(): {"name": d.get("name") or d["mac"].upper(), "model": d.get("model", "")}
            for d in items if d.get("mac")
        }
    except Exception:
        return {}


def _infer_port_speed(port_id):
    """Infer Mbps from Juniper port name prefix."""
    p = (port_id or "").lower()
    if p.startswith("et-"):          return 100000   # 100 GbE
    if p.startswith("xe-"):          return 10000    # 10 GbE
    if p.startswith("mge-"):         return 10000    # Multi-Gig (up to 10G on EX4300-MP)
    if p.startswith(("ge-", "me-")): return 1000     # GigE
    if p.startswith("fe-"):          return 100      # Fast Ethernet
    return None


def get_port_stats(site_id, switch_mac, port_id):
    """Return live stats for a port. Tries switch_ports/search first (richer data),
    falls back to device stats if_stat. Never raises."""

    # ── Primary: switch_ports/search (has PoE, errors, STP, port_desc, real speed) ──
    for path in [
        f"/sites/{site_id}/stats/switch_ports/search",
        f"/orgs/{ORG_ID}/stats/switch_ports/search",
    ]:
        try:
            r     = mist_get(path, params={"mac": switch_mac})
            ports = r if isinstance(r, list) else r.get("results", r.get("data", []))
            for port in ports:
                if port.get("port_id") == port_id:
                    result = dict(port)
                    # Fill in computed fallbacks if Mist didn't return them
                    if not result.get("speed"):
                        result["speed"] = _infer_port_speed(port_id)
                    if "full_duplex" not in result:
                        spd = result.get("speed")
                        result["full_duplex"] = spd >= 100 if spd else None
                    return result
        except requests.HTTPError:
            pass

    # ── Fallback: device stats if_stat (limited but widely available) ────────
    try:
        sw_id   = f"00000000-0000-0000-1000-{switch_mac.lower()}"
        r       = mist_get(f"/sites/{site_id}/stats/devices/{sw_id}")
        if_stat = r.get("if_stat", {})
        pd = if_stat.get(f"{port_id}.0") or if_stat.get(port_id) or {}
        if not pd:
            port_lower = port_id.lower()
            for key, val in if_stat.items():
                if key.lower().rstrip(".0") == port_lower or key.lower().startswith(port_lower):
                    pd = val
                    break
        if pd:
            speed = _infer_port_speed(port_id)
            result = dict(pd)
            result.update({
                "port_id":     port_id,
                "speed":       speed,
                "full_duplex": speed >= 100 if speed else None,
                "tx_bytes":    pd.get("tx_bytes", 0),
                "rx_bytes":    pd.get("rx_bytes", 0),
                "tx_bps":      pd.get("tx_bps", 0),
                "rx_bps":      pd.get("rx_bps", 0),
            })
            return result
    except Exception:
        pass
    return {}


def search_wired_client(ip, hint_site_id=None):
    """Search for a wired client by IP. Tries org → hinted site → all sites. Never raises."""
    # Org-level (fastest)
    for endpoint in [
        f"/orgs/{ORG_ID}/wired_clients/search",
        f"/orgs/{ORG_ID}/wiredclients/search",   # legacy spelling
    ]:
        try:
            r = mist_get(endpoint, {"ip": ip, "limit": 1})
            if r.get("results"):
                return r["results"][0]
        except requests.HTTPError:
            pass

    # Site-level: hinted site first, then sweep remaining
    tried = set()
    for sid in filter(None, [hint_site_id]):
        tried.add(sid)
        for endpoint in [
            f"/sites/{sid}/wired_clients/search",
            f"/sites/{sid}/wiredclients/search",
        ]:
            try:
                r = mist_get(endpoint, {"ip": ip, "limit": 1})
                if r.get("results"):
                    return r["results"][0]
                break           # endpoint exists, just no results — no need to try legacy spelling
            except requests.HTTPError:
                pass

    try:
        sites = mist_get(f"/orgs/{ORG_ID}/sites")
        for site in (sites if isinstance(sites, list) else []):
            sid = site.get("id")
            if not sid or sid in tried:
                continue
            for endpoint in [
                f"/sites/{sid}/wired_clients/search",
                f"/sites/{sid}/wiredclients/search",
            ]:
                try:
                    r = mist_get(endpoint, {"ip": ip, "limit": 1})
                    if r.get("results"):
                        return r["results"][0]
                    break
                except requests.HTTPError:
                    pass
    except Exception:
        pass

    return None


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_bps(bps):
    if not bps: return "0 bps"
    if bps >= 1_000_000: return f"{bps/1_000_000:.1f} Mbps"
    if bps >= 1_000:     return f"{bps/1_000:.1f} Kbps"
    return f"{int(bps)} bps"


def fmt_bytes(b):
    if not b: return "0 B"
    if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} GB"
    if b >= 1_048_576:     return f"{b/1_048_576:.1f} MB"
    if b >= 1_024:         return f"{b/1_024:.1f} KB"
    return f"{int(b)} B"


def _retry_rows(stats):
    """Return email body rows for TX/RX retry rates (only when non-zero).
    AP-centric: rx_retries/rx_pkts = client TX retries; tx_retries/tx_pkts = client RX retries."""
    rows = []
    def row(label, value): return f"{label:<14}: {value}"
    tx_r = stats.get("rx_retries") or 0   # client TX = AP rx
    rx_r = stats.get("tx_retries") or 0   # client RX = AP tx
    tx_p = stats.get("rx_pkts")    or 0
    rx_p = stats.get("tx_pkts")    or 0
    if tx_r and tx_p:
        rows.append(row("TX Retries", f"{tx_r/tx_p*100:.1f}%  ({tx_r} / {tx_p} pkts)"))
    if rx_r and rx_p:
        rows.append(row("RX Retries", f"{rx_r/rx_p*100:.1f}%  ({rx_r} / {rx_p} pkts)"))
    return rows


def fmt_speed_label(mbps):
    """Return (label, colour) for a port speed in Mbps."""
    if mbps is None: return "—", "#94a3b8"
    if mbps >= 10000: return "10 Gbps",  "#8b5cf6"
    if mbps >= 1000:  return "1 Gbps",   "#22c55e"
    if mbps >= 100:   return "100 Mbps", "#84cc16"
    if mbps >= 10:    return "10 Mbps",  "#f59e0b"
    return f"{mbps} Mbps", "#ef4444"


def fmt_uptime(seconds):
    if seconds is None: return "—"
    td = timedelta(seconds=int(seconds))
    h, rem = divmod(td.seconds, 3600)
    m = rem // 60
    parts = []
    if td.days: parts.append(f"{td.days}d")
    if h:       parts.append(f"{h}h")
    parts.append(f"{m}m")
    return " ".join(parts) or "0m"


def fmt_timestamp(ts):
    if not ts: return "—"
    return datetime.fromtimestamp(int(ts)).strftime("%H:%M:%S")


def signal_info(rssi):
    if rssi is None: return "Unknown", "#94a3b8", 0
    pct = min(100, max(0, int((rssi + 90) / 60 * 100)))
    if rssi >= -50: return "Excellent", "#22c55e", pct
    if rssi >= -67: return "Good",      "#84cc16", pct
    if rssi >= -80: return "Fair",      "#f59e0b", pct
    return "Poor", "#ef4444", pct


def snr_info(snr):
    if snr is None: return "Unknown", "#94a3b8", 0
    pct = min(100, max(0, int(snr / 60 * 100)))
    if snr >= 40: return "Excellent", "#22c55e", pct
    if snr >= 25: return "Good",      "#84cc16", pct
    if snr >= 10: return "Fair",      "#f59e0b", pct
    return "Poor", "#ef4444", pct


def build_mailto(ip, stats, client, wired_client, port_stats,
                 ap_names, switch_names, checker_url, timestamp):
    """Build a mailto: URL with full diagnostic detail pre-filled."""
    if not SUPPORT_EMAIL:
        return None

    def row(label, value):
        return f"{label:<14}: {value}"

    lines = []

    if stats:
        hostname   = stats.get("hostname") or "Unknown"
        subject    = f"Network Issue — {hostname} at {ip}"
        rssi_lbl, _, _ = signal_info(stats.get("rssi"))
        snr_lbl,  _, _ = snr_info(stats.get("snr"))
        ap_mac     = stats.get("ap_mac", "")
        ap_name    = ap_names.get(ap_mac.lower(), ap_mac.upper()) if ap_mac else "—"
        random_mac = "Yes (randomised)" if (client and client.get("random_mac")) else "No"

        username = (client.get("last_username") or
                    (client.get("username") or [None])[0]) if client else None
        lines += [
            "=== DEVICE ===",
            row("Hostname",    hostname),
            *([row("Username", username)] if username else []),
            row("MAC Address", stats.get("mac", "—").upper()),
            row("Random MAC",  random_mac),
            row("Manufacturer",stats.get("manufacture") or "—"),
            row("Family/Model",(f"{stats.get('family','')} {stats.get('model','')}").strip() or "—"),
            row("OS",          stats.get("os") or "—"),
            row("IP Address",  ip),
            "",
            "=== WIFI CONNECTION ===",
            row("SSID",        stats.get("ssid") or "—"),
            row("Band",        BAND_LABEL.get(stats.get("band",""), stats.get("band","—"))),
            row("Protocol",    WIFI_GEN.get(stats.get("proto",""), stats.get("proto","—"))),
            row("Channel",     str(stats.get("channel","—"))),
            row("BSSID",       stats.get("bssid","—").upper()),
            row("VLAN",        str(stats.get("vlan_id","—"))),
            row("Access Point",f"{ap_name}  ({ap_mac.upper()})"),
            "",
            "=== SIGNAL QUALITY ===",
            row("RSSI",        f"{stats.get('rssi','—')} dBm  ({rssi_lbl})"),
            row("SNR",         f"{stats.get('snr','—')} dB  ({snr_lbl})"),
            "",
            "=== THROUGHPUT ===",
            row("Upload now",  fmt_bps(stats.get("rx_bps"))),    # AP rx = client upload
            row("Download now",fmt_bps(stats.get("tx_bps"))),    # AP tx = client download
            row("Session TX",  fmt_bytes(stats.get("rx_bytes", 0) or 0)),
            row("Session RX",  fmt_bytes(stats.get("tx_bytes", 0) or 0)),
            *(_retry_rows(stats)),
            "",
            "=== SESSION ===",
            row("Uptime",      fmt_uptime(stats.get("uptime"))),
            row("Idle time",   fmt_uptime(stats.get("idle_time"))),
            row("Last seen",   fmt_timestamp(stats.get("last_seen"))),
        ]

        if client and client.get("ap"):
            lines += ["", "=== ROAMING HISTORY ==="]
            for ap in client["ap"]:
                name    = ap_names.get(ap.lower(), ap.upper())
                current = "  <-- current" if ap == ap_mac else ""
                lines.append(f"  {name}  ({ap.upper()}){current}")

    elif wired_client:
        hostname  = wired_client.get("dhcp_hostname") or "Unknown"
        subject   = f"Network Issue — {hostname} at {ip}"
        sw_mac    = wired_client.get("last_device_mac", "").lower()
        sw_info   = switch_names.get(sw_mac, {})
        sw_name   = sw_info.get("name", sw_mac.upper())
        sw_model  = sw_info.get("model", "—")
        spd_lbl, _ = fmt_speed_label(port_stats.get("speed"))
        duplex    = ("Full" if port_stats.get("full_duplex")
                     else "Half" if port_stats.get("full_duplex") is not None
                     else "—")
        link      = ("Up" if port_stats.get("up")
                     else "Down" if port_stats.get("up") is not None
                     else "—")

        lines += [
            "=== DEVICE ===",
            row("Hostname",    hostname),
            row("MAC Address", wired_client.get("mac","—").upper()),
            row("Manufacturer",wired_client.get("manufacture") or "—"),
            row("IP Address",  ip),
            "",
            "=== WIRED CONNECTION ===",
            row("Switch",      f"{sw_name}  ({sw_model})"),
            row("Port",        wired_client.get("last_port_id","—")),
            row("VLAN",        f"{wired_client.get('last_vlan','—')}  {wired_client.get('last_vlan_name','')}".strip()),
            row("Auth",        wired_client.get("auth_method") or "None"),
            "",
            "=== PORT QUALITY ===",
            row("Speed",       spd_lbl),
            row("Duplex",      duplex),
            row("Link Status", link),
            row("PoE",         (
                f"Active — {port_stats.get('power_draw'):.1f} W"
                + (f" / {port_stats.get('power_allocated'):.1f} W allocated" if port_stats.get("power_allocated") else "")
                + (f"  [{port_stats.get('poe_mode')}  {port_stats.get('poe_priority','').capitalize()} priority]".strip() if port_stats.get("poe_mode") else "")
                if port_stats.get("poe_on") and port_stats.get("power_draw") else
                "Active" if port_stats.get("poe_on") else
                "Disabled" if port_stats.get("poe_disabled") else "Inactive"
            )),
            "",
            "=== THROUGHPUT ===",
            row("Upload now",  fmt_bps(port_stats.get("rx_bps"))),
            row("Download now",fmt_bps(port_stats.get("tx_bps"))),
            *([row("RX Errors", str(port_stats.get("rx_errors")))] if port_stats.get("rx_errors") else []),
            *([row("TX Errors", str(port_stats.get("tx_errors")))] if port_stats.get("tx_errors") else []),
        ]

        history = wired_client.get("device_mac_port", [])
        if len(history) > 1:
            lines += ["", "=== PORT HISTORY ==="]
            for entry in history:
                e_sw   = switch_names.get(entry.get("device_mac","").lower(), {})
                e_name = e_sw.get("name", entry.get("device_mac","—").upper())
                is_cur = (entry.get("port_id")    == wired_client.get("last_port_id") and
                          entry.get("device_mac") == wired_client.get("last_device_mac"))
                cur    = "  <-- current" if is_cur else ""
                lines.append(f"  {e_name}  port {entry.get('port_id','—')}  VLAN {entry.get('vlan','—')}{cur}")
    else:
        return None

    lines += [
        "",
        "=== MARVIS AI DIAGNOSTICS ===",
        "Please run Marvis before sending — click either Marvis button",
        "on the dashboard and paste the results below:",
        "",
        "(paste Marvis output here)",
        "",
        "=== DIAGNOSTIC METADATA ===",
        row("Timestamp",   timestamp),
        row("Checker URL", checker_url),
    ]

    body = "\r\n".join(lines)
    return f"mailto:{SUPPORT_EMAIL}?subject={quote(subject)}&body={quote(body)}"


BAND_LABEL = {"24": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}
WIFI_GEN   = {
    "ax": "Wi-Fi 6/6E (802.11ax)", "ac": "Wi-Fi 5 (802.11ac)",
    "n":  "Wi-Fi 4 (802.11n)",     "g":  "Wi-Fi 3 (802.11g)",
    "a":  "802.11a",               "b":  "802.11b",
}


# ── HTML Template ─────────────────────────────────────────────────────────────

EXPERT_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Mist Network Status</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: #0f172a; color: #e2e8f0;
      min-height: 100vh; padding: 1rem;
    }
    .container { max-width: 900px; margin: 0 auto; }

    header {
      display: flex; align-items: center; gap: 0.75rem;
      margin-bottom: 0.9rem;
    }
    header .logo {
      width: 36px; height: 36px; border-radius: 10px; flex-shrink: 0;
      background: linear-gradient(135deg, #6366f1, #8b5cf6);
      display: flex; align-items: center; justify-content: center; font-size: 1.1rem;
    }
    header h1 { font-size: 1.15rem; font-weight: 700; color: #f8fafc; }
    header p  { font-size: 0.78rem; color: #94a3b8; margin-top: 1px; }

    .pill {
      display: inline-flex; align-items: center; gap: 0.35rem;
      background: #1e293b; border: 1px solid #334155;
      border-radius: 999px; padding: 0.25rem 0.65rem;
      font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.9rem;
    }
    .pill .dot { width: 6px; height: 6px; border-radius: 50%; background: #22c55e; }

    .error-box {
      background: #450a0a; border: 1px solid #7f1d1d; border-radius: 10px;
      padding: 0.9rem 1.1rem; color: #fca5a5; margin-bottom: 0.9rem;
    }
    .error-box strong { display: block; margin-bottom: 0.2rem; color: #f87171; }

    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0.65rem; }
    @media (max-width: 560px) { .grid { grid-template-columns: 1fr; } }

    .card {
      background: #1e293b; border: 1px solid #334155;
      border-radius: 12px; padding: 0.8rem 1rem;
    }
    .card-title {
      font-size: 0.65rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.08em; color: #64748b; margin-bottom: 0.55rem;
    }

    .stat-row {
      display: flex; justify-content: space-between; align-items: baseline;
      padding: 0.27rem 0; border-bottom: 1px solid #0f172a; gap: 0.5rem;
    }
    .stat-row:last-child { border-bottom: none; }
    .stat-label { font-size: 0.78rem; color: #94a3b8; flex-shrink: 0; }
    .stat-value { font-size: 0.82rem; color: #f1f5f9; font-weight: 500; text-align: right; }

    /* Signal bars */
    .signal-block { margin-bottom: 0.65rem; }
    .signal-block:last-child { margin-bottom: 0; }
    .signal-header {
      display: flex; justify-content: space-between; align-items: baseline;
      margin-bottom: 0.35rem;
    }
    .signal-name  { font-size: 0.76rem; color: #94a3b8; }
    .signal-value { font-size: 0.92rem; font-weight: 700; }
    .signal-label { font-size: 0.7rem; margin-left: 0.3rem; font-weight: 400; }
    .bar-track { background: #0f172a; border-radius: 999px; height: 6px; overflow: hidden; }
    .bar-fill  { height: 100%; border-radius: 999px; transition: width 0.6s ease; }

    /* Throughput */
    .throughput-row {
      display: flex; align-items: center; gap: 0.6rem;
      padding: 0.4rem 0; border-bottom: 1px solid #0f172a;
    }
    .throughput-row:last-child { border-bottom: none; }
    .arrow    { font-size: 0.95rem; }
    .tp-label { font-size: 0.74rem; color: #64748b; width: 65px; }
    .tp-value { font-size: 0.92rem; font-weight: 700; color: #f1f5f9; }

    /* Badges */
    .badge {
      display: inline-block; padding: 0.15rem 0.5rem;
      border-radius: 999px; font-size: 0.68rem; font-weight: 600;
    }
    .badge-green  { background: #14532d; color: #86efac; }
    .badge-orange { background: #431407; color: #fdba74; }

    .full-width { grid-column: 1 / -1; }

    /* AP chip in roaming history */
    .ap-chip {
      display: inline-flex; flex-direction: column;
      background: #0f172a; border: 1px solid #334155; border-radius: 8px;
      padding: 0.3rem 0.65rem; font-size: 0.74rem;
    }
    .ap-chip .ap-name { color: #e2e8f0; font-weight: 500; }
    .ap-chip .ap-mac  { color: #475569; font-family: monospace; font-size: 0.7rem; margin-top: 1px; }
    .ap-chip .ap-cur  { color: #22c55e; font-size: 0.65rem; margin-top: 2px; }

    /* Not-found */
    .not-found {
      text-align: center; padding: 2.5rem 1.5rem;
      background: #1e293b; border: 1px solid #334155; border-radius: 12px;
    }
    .not-found .icon { font-size: 2.5rem; margin-bottom: 0.75rem; }
    .not-found h2 { color: #f1f5f9; margin-bottom: 0.4rem; }
    .not-found p  { color: #64748b; font-size: 0.85rem; }

    /* Buttons */
    .btn-row {
      display: flex; gap: 0.6rem; justify-content: center;
      margin-top: 0.9rem; flex-wrap: wrap;
    }
    .refresh-btn {
      background: #1e293b; color: #94a3b8;
      border: 1px solid #334155; border-radius: 8px;
      padding: 0.5rem 1.1rem; font-size: 0.82rem; font-weight: 600;
      cursor: pointer; text-decoration: none;
    }
    .refresh-btn:hover { background: #334155; }

    .email-btn {
      background: #1e293b; color: #22c55e;
      border: 1px solid #22c55e; border-radius: 8px;
      padding: 0.5rem 1.1rem; font-size: 0.82rem; font-weight: 600;
      cursor: pointer; text-decoration: none;
    }
    .email-btn:hover { background: #14532d; color: #86efac; }

    .marvis-btn {
      display: inline-flex; align-items: center; gap: 0.4rem;
      border: none; border-radius: 8px;
      padding: 0.5rem 1.1rem; font-size: 0.82rem; font-weight: 600;
      cursor: pointer; transition: opacity 0.2s; color: #fff;
    }
    .marvis-btn-7d { background: linear-gradient(135deg, #6366f1, #8b5cf6); }
    .marvis-btn-1h { background: linear-gradient(135deg, #0ea5e9, #6366f1); }
    .marvis-btn:hover    { opacity: 0.85; }
    .marvis-btn:disabled { opacity: 0.45; cursor: not-allowed; }

    /* Marvis results */
    .marvis-section {
      margin-top: 0.65rem;
      background: #1e293b; border: 1px solid #334155;
      border-radius: 12px; padding: 0.8rem 1rem;
    }
    .marvis-section-title {
      font-size: 0.65rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.08em; color: #64748b; margin-bottom: 0.65rem;
    }
    .marvis-card {
      border-left: 3px solid #6366f1; background: #0f172a;
      border-radius: 8px; padding: 0.7rem 0.9rem; margin-bottom: 0.55rem;
    }
    .marvis-card:last-child { margin-bottom: 0; }
    .marvis-header { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem; flex-wrap: wrap; }
    .marvis-category { font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.06em; }
    .marvis-reason   { font-size: 0.82rem; font-weight: 600; color: #f1f5f9; }
    .marvis-text     { font-size: 0.8rem; color: #94a3b8; line-height: 1.5; }
    .marvis-rec      { margin-top: 0.5rem; padding-left: 1.1rem; font-size: 0.77rem; color: #64748b; line-height: 1.55; }
    .marvis-rec li   { margin-bottom: 0.2rem; }
    .marvis-ok       { display: flex; align-items: center; gap: 0.5rem; color: #22c55e; font-size: 0.85rem; font-weight: 600; padding: 0.25rem 0; }
    .marvis-spinner  { display: flex; align-items: center; gap: 0.6rem; color: #64748b; font-size: 0.82rem; padding: 0.25rem 0; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .spinner-ring {
      width: 15px; height: 15px; border: 2px solid #334155;
      border-top-color: #6366f1; border-radius: 50%;
      animation: spin 0.7s linear infinite; flex-shrink: 0;
    }

    footer { text-align: center; margin-top: 1.25rem; font-size: 0.7rem; color: #334155; }

    /* ── Auto-refresh indicator ── */
    .refresh-indicator {
      flex-shrink: 0; text-align: center; cursor: pointer;
      padding: 0.25rem 0.5rem; border-radius: 8px;
      border: 1px solid #1e293b; transition: border-color 0.2s;
      user-select: none;
    }
    .refresh-indicator:hover { border-color: #334155; }
    .refresh-indicator .ri-count {
      font-size: 1rem; font-weight: 700; line-height: 1;
      transition: color 0.3s;
    }
    .refresh-indicator .ri-label {
      font-size: 0.58rem; color: #475569; margin-top: 2px; letter-spacing: 0.04em;
    }
  </style>
</head>
<body>
<div class="container">

  <header>
    <div style="font-size: 1.2rem; font-weight: 700; color: #f8fafc; flex: 1;">
      {% if client and stats %}Wireless Stats{% elif wired_client %}Wired Stats{% else %}Network Status{% endif %}
    </div>
    {% if client and stats %}
      <button style="background: none; border: 1px solid #334155; border-radius: 50%; width: 28px; height: 28px; color: #94a3b8; cursor: pointer; font-size: 0.85rem; padding: 0; display: flex; align-items: center; justify-content: center;" onclick="openWiFiInfoModal()" title="WiFi metrics explained">ⓘ</button>
    {% elif wired_client %}
      <button style="background: none; border: 1px solid #334155; border-radius: 50%; width: 28px; height: 28px; color: #94a3b8; cursor: pointer; font-size: 0.85rem; padding: 0; display: flex; align-items: center; justify-content: center;" onclick="openWiredInfoModal()" title="Wired connection info">ⓘ</button>
    {% endif %}
    <a href="/{% if ip_override %}{{ ip_override }}{% endif %}" style="padding:0.4rem 0.8rem; border-radius:6px; border:1px solid #334155; background:#1e293b; color:#94a3b8; font-size:0.7rem; text-decoration:none;">Simple Mode</a>
    <div class="refresh-indicator" onclick="togglePause()" title="Click to pause / resume auto-refresh">
      <div class="ri-count" id="ri-count">30s</div>
      <div class="ri-label">AUTO-REFRESH</div>
    </div>
  </header>

  <div class="pill">
    {% if wired_client %}
      <span class="dot" style="background:#f59e0b;"></span>
      🔌 Wired client at <strong style="color:#e2e8f0; margin-left:3px;">{{ client_ip }}</strong>
    {% else %}
      <span class="dot"></span>
      📶 WiFi client at <strong style="color:#e2e8f0; margin-left:3px;">{{ client_ip }}</strong>
    {% endif %}
    {% if ip_override %}
      <span style="color:#6366f1; margin-left:6px; font-size:0.7rem;">(manual lookup)</span>
    {% endif %}
  </div>

  {% if error %}
  <div class="error-box">
    <strong>⚠ Could not retrieve client data</strong>{{ error }}
  </div>
  {% endif %}

  {% if not stats and not wired_client and not error %}
  <div class="not-found">
    <div class="icon">🔍</div>
    <h2>Client Not Found</h2>
    <p>No active Mist client found for <strong>{{ client_ip }}</strong>.<br>
       Checked both wireless and wired — make sure this device is connected to a Mist-managed network.</p>
  </div>

  {% elif stats %}

  {% set rssi_label, rssi_colour, rssi_pct = signal_info(stats.get('rssi')) %}
  {% set snr_label,  snr_colour,  snr_pct  = snr_info(stats.get('snr'))   %}

  <div class="grid">

    {# ── Device ── #}
    <div class="card">
      <div class="card-title">Device</div>
      <div class="stat-row">
        <span class="stat-label">Hostname</span>
        <span class="stat-value">{{ stats.get('hostname') or '—' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Manufacturer</span>
        <span class="stat-value">{{ stats.get('manufacture') or '—' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Family / Model</span>
        <span class="stat-value">
          {{ stats.get('family') or '' }}{% if stats.get('model') %} · {{ stats.get('model') }}{% endif %}
          {% if not stats.get('family') and not stats.get('model') %}—{% endif %}
        </span>
      </div>
      <div class="stat-row">
        <span class="stat-label">OS</span>
        <span class="stat-value">{{ stats.get('os') or '—' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">MAC Address</span>
        <span class="stat-value" style="font-family:monospace;font-size:0.76rem;">{{ stats.get('mac','—')|upper }}</span>
      </div>
      {% set username = (client.get('last_username') or (client.get('username') or [None])[0]) if client else none %}
      {% if username %}
      <div class="stat-row">
        <span class="stat-label">Username</span>
        <span class="stat-value">{{ username }}</span>
      </div>
      {% endif %}
      <div class="stat-row">
        <span class="stat-label">Random MAC</span>
        <span class="stat-value">
          {% if client and client.get('random_mac') %}
            <span class="badge badge-orange">Yes</span>
          {% else %}
            <span class="badge badge-green">No</span>
          {% endif %}
        </span>
      </div>
    </div>

    {# ── Signal Quality ── #}
    <div class="card">
      <div class="card-title">Signal Quality</div>

      <div class="signal-block">
        <div class="signal-header">
          <span class="signal-name">RSSI</span>
          <span class="signal-value" style="color:{{ rssi_colour }}">
            {{ stats.get('rssi','—') }} dBm
            <span class="signal-label" style="color:{{ rssi_colour }}">{{ rssi_label }}</span>
          </span>
        </div>
        <div class="bar-track"><div class="bar-fill" style="width:{{ rssi_pct }}%;background:{{ rssi_colour }};"></div></div>
      </div>

      <div class="signal-block">
        <div class="signal-header">
          <span class="signal-name">SNR</span>
          <span class="signal-value" style="color:{{ snr_colour }}">
            {{ stats.get('snr','—') }} dB
            <span class="signal-label" style="color:{{ snr_colour }}">{{ snr_label }}</span>
          </span>
        </div>
        <div class="bar-track"><div class="bar-fill" style="width:{{ snr_pct }}%;background:{{ snr_colour }};"></div></div>
      </div>

      {# ── Retry rates (only shown when non-zero) ── #}
      {# Retries: AP-centric like bytes — rx_retries/rx_pkts = client TX, tx_retries/tx_pkts = client RX #}
      {% set tx_retries = stats.get('rx_retries', 0) or 0 %}
      {% set rx_retries = stats.get('tx_retries', 0) or 0 %}
      {% set tx_pkts    = stats.get('rx_pkts', 0) or 0 %}
      {% set rx_pkts    = stats.get('tx_pkts', 0) or 0 %}
      {% if (tx_retries and tx_pkts) or (rx_retries and rx_pkts) %}
      <div style="margin-top:0.55rem; padding-top:0.55rem; border-top:1px solid #0f172a;">
        {% if tx_retries and tx_pkts %}
        {% set tx_pct = (tx_retries / tx_pkts * 100) %}
        {% set tx_col = '#ef4444' if tx_pct > 20 else '#f59e0b' if tx_pct > 5 else '#84cc16' %}
        <div class="stat-row">
          <span class="stat-label">TX Retries</span>
          <span class="stat-value" style="color:{{ tx_col }}; font-weight:600;">
            {{ "%.1f"|format(tx_pct) }}%
            <span style="color:#475569; font-weight:400; font-size:0.72rem;">({{ tx_retries }} / {{ tx_pkts }} pkts)</span>
          </span>
        </div>
        {% endif %}
        {% if rx_retries and rx_pkts %}
        {% set rx_pct = (rx_retries / rx_pkts * 100) %}
        {% set rx_col = '#ef4444' if rx_pct > 20 else '#f59e0b' if rx_pct > 5 else '#84cc16' %}
        <div class="stat-row">
          <span class="stat-label">RX Retries</span>
          <span class="stat-value" style="color:{{ rx_col }}; font-weight:600;">
            {{ "%.1f"|format(rx_pct) }}%
            <span style="color:#475569; font-weight:400; font-size:0.72rem;">({{ rx_retries }} / {{ rx_pkts }} pkts)</span>
          </span>
        </div>
        {% endif %}
      </div>
      {% endif %}

      {# ── TX / RX split bar ──
         Mist stats are AP-centric:
           tx_bytes = AP transmitted to client  = client DOWNLOAD (▼ RX, green)
           rx_bytes = AP received from client   = client UPLOAD   (▲ TX, indigo) #}
      {% set ul_b    = stats.get('rx_bytes', 0) or 0 %}
      {% set dl_b    = stats.get('tx_bytes', 0) or 0 %}
      {% set total_b = ul_b + dl_b %}
      {% if total_b > 0 %}
      {% set ul_pct = (ul_b / total_b * 100) | int %}
      {% set dl_pct = 100 - ul_pct %}
      <div style="margin-top:0.6rem; padding-top:0.6rem; border-top:1px solid #0f172a;">
        <div class="signal-name" style="margin-bottom:0.35rem;">Session Data</div>
        <div style="display:flex; height:10px; border-radius:5px; overflow:hidden; background:#0f172a; gap:1px;">
          <div style="width:{{ ul_pct }}%; background:#6366f1; border-radius:5px 0 0 5px; min-width:3px;"></div>
          <div style="width:{{ dl_pct }}%; background:#22c55e; border-radius:0 5px 5px 0; min-width:3px;"></div>
        </div>
        <div style="display:flex; justify-content:space-between; margin-top:0.3rem;">
          <span style="font-size:0.74rem; color:#6366f1; font-weight:600;">
            ▲ TX &nbsp;{{ fmt_bytes(ul_b) }}
          </span>
          <span style="font-size:0.72rem; color:#475569;">
            {{ fmt_bytes(total_b) }} total
          </span>
          <span style="font-size:0.74rem; color:#22c55e; font-weight:600;">
            {{ fmt_bytes(dl_b) }}&nbsp; ▼ RX
          </span>
        </div>
      </div>
      {% endif %}

    </div>

    {# ── Connection ── #}
    <div class="card">
      <div class="card-title">Connection</div>
      <div class="stat-row">
        <span class="stat-label">SSID</span>
        <span class="stat-value">{{ stats.get('ssid') or '—' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Band</span>
        <span class="stat-value">{{ BAND_LABEL.get(stats.get('band',''), stats.get('band','—')) }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Protocol</span>
        <span class="stat-value">{{ WIFI_GEN.get(stats.get('proto',''), stats.get('proto','—')) }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Channel</span>
        <span class="stat-value">{{ stats.get('channel','—') }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">BSSID</span>
        <span class="stat-value" style="font-family:monospace;font-size:0.74rem;">{{ stats.get('bssid','—')|upper }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">VLAN</span>
        <span class="stat-value">{{ stats.get('vlan_id','—') }}</span>
      </div>
    </div>

    {# ── Throughput & Session ── #}
    <div class="card">
      <div class="card-title">Throughput &amp; Session</div>
      {# AP-centric: tx_bps = AP→client = download; rx_bps = client→AP = upload #}
      <div class="throughput-row">
        <span class="arrow" style="color:#6366f1;">▲</span>
        <span class="tp-label">Upload</span>
        <span class="tp-value">{{ fmt_bps(stats.get('rx_bps')) }}</span>
      </div>
      <div class="throughput-row">
        <span class="arrow" style="color:#22c55e;">▼</span>
        <span class="tp-label">Download</span>
        <span class="tp-value">{{ fmt_bps(stats.get('tx_bps')) }}</span>
      </div>
      <div style="margin-top:0.5rem;">
        <div class="stat-row">
          <span class="stat-label">Session uptime</span>
          <span class="stat-value">{{ fmt_uptime(stats.get('uptime')) }}</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Idle time</span>
          <span class="stat-value">{{ fmt_uptime(stats.get('idle_time')) }}</span>
        </div>
        <div class="stat-row">
          <span class="stat-label">Last seen</span>
          <span class="stat-value">{{ fmt_timestamp(stats.get('last_seen')) }}</span>
        </div>
      </div>
    </div>

    {# ── Roaming History ── #}
    {% if client and client.get('ap') and client['ap']|length > 0 %}
    <div class="card full-width">
      <div class="card-title">Roaming History (this session)</div>
      <div style="display:flex;flex-wrap:wrap;gap:0.45rem;margin-top:0.2rem;">
        {% for ap_mac in client.get('ap', []) %}
        {% set is_current = (ap_mac == stats.get('ap_mac')) %}
        {% set ap_name = ap_names.get(ap_mac.lower(), '') %}
        <div class="ap-chip" style="border-color:{{ '#22c55e' if is_current else '#334155' }};">
          <span class="ap-name">{{ ap_name if ap_name else ap_mac|upper }}</span>
          {% if ap_name %}
          <span class="ap-mac">{{ ap_mac|upper }}</span>
          {% endif %}
          {% if is_current %}<span class="ap-cur">● current</span>{% endif %}
        </div>
        {% endfor %}
      </div>
    </div>
    {% endif %}

  </div><!-- /grid -->

  {% elif wired_client %}

  {# ════════════════ WIRED CLIENT GRID ════════════════ #}
  {% set spd_label, spd_colour = fmt_speed_label(port_stats.get('speed')) %}
  {% set sw_info = switch_names.get(wired_client.get('last_device_mac','').lower(), {}) %}

  <div class="grid">

    {# ── Device ── #}
    <div class="card">
      <div class="card-title">Device</div>
      <div class="stat-row">
        <span class="stat-label">Hostname</span>
        <span class="stat-value">{{ wired_client.get('dhcp_hostname') or '—' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Manufacturer</span>
        <span class="stat-value">{{ wired_client.get('manufacture') or '—' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">IP Address</span>
        <span class="stat-value">{{ wired_client.get('last_ip') or '—' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">MAC Address</span>
        <span class="stat-value" style="font-family:monospace;font-size:0.76rem;">{{ wired_client.get('mac','—')|upper }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Random MAC</span>
        <span class="stat-value">
          {% if wired_client.get('random_mac') %}
            <span class="badge badge-orange">Yes</span>
          {% else %}
            <span class="badge badge-green">No</span>
          {% endif %}
        </span>
      </div>
    </div>

    {# ── Port Quality ── #}
    <div class="card">
      <div class="card-title">Port Quality</div>
      <div class="signal-block">
        <div class="signal-header">
          <span class="signal-name">Link Speed</span>
          <span class="signal-value" style="color:{{ spd_colour }};">{{ spd_label }}</span>
        </div>
        {% set spd_pct = [[(port_stats.get('speed') or 0), 10000]|min, 0]|max %}
        <div class="bar-track">
          <div class="bar-fill" style="width:{{ ((port_stats.get('speed') or 0) / 10000 * 100)|int }}%;background:{{ spd_colour }};"></div>
        </div>
      </div>
      <div class="stat-row">
        <span class="stat-label">Duplex</span>
        <span class="stat-value">
          {% if port_stats.get('full_duplex') is not none %}
            {% if port_stats.get('full_duplex') %}<span style="color:#22c55e;">Full</span>
            {% else %}<span style="color:#f59e0b;">Half</span>{% endif %}
          {% else %}—{% endif %}
        </span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Link Status</span>
        <span class="stat-value">
          {% if port_stats.get('up') %}<span class="badge badge-green">Up</span>
          {% elif port_stats.get('up') is not none %}<span class="badge" style="background:#450a0a;color:#f87171;">Down</span>
          {% else %}—{% endif %}
        </span>
      </div>
      <div class="stat-row">
        <span class="stat-label">PoE</span>
        <span class="stat-value">
          {% if port_stats.get('poe_on') %}
            <span class="badge badge-green">Active</span>
            {% if port_stats.get('power_draw') %}
              <span style="color:#22c55e; font-size:0.78rem; margin-left:0.4rem;">
                {{ "%.1f"|format(port_stats.get('power_draw')) }} W
                {% if port_stats.get('power_allocated') %}/ {{ "%.1f"|format(port_stats.get('power_allocated')) }} W{% endif %}
              </span>
            {% endif %}
          {% elif port_stats.get('poe_disabled') %}
            <span class="badge" style="background:#431407;color:#fdba74;">Disabled</span>
          {% elif port_stats.get('poe_on') == false %}
            <span class="badge" style="background:#1e293b;color:#475569;">Inactive</span>
          {% else %}
            <span style="color:#475569;">—</span>
          {% endif %}
        </span>
      </div>
      {% if port_stats.get('poe_on') and port_stats.get('poe_mode') %}
      <div class="stat-row">
        <span class="stat-label">PoE Mode</span>
        <span class="stat-value" style="font-size:0.78rem;">
          {{ port_stats.get('poe_mode') }}
          {% if port_stats.get('poe_priority') %}
            · {{ port_stats.get('poe_priority')|capitalize }} priority
          {% endif %}
        </span>
      </div>
      {% endif %}
      {% if port_stats.get('port_desc') %}
      <div class="stat-row">
        <span class="stat-label">Port Desc</span>
        <span class="stat-value" style="font-size:0.78rem; color:#94a3b8;">{{ port_stats.get('port_desc') }}</span>
      </div>
      {% endif %}
      {% if port_stats.get('rx_errors') %}
      <div class="stat-row">
        <span class="stat-label">RX Errors</span>
        <span class="stat-value" style="color:#ef4444; font-weight:600;">{{ port_stats.get('rx_errors') }}</span>
      </div>
      {% endif %}
      {% if port_stats.get('tx_errors') %}
      <div class="stat-row">
        <span class="stat-label">TX Errors</span>
        <span class="stat-value" style="color:#ef4444; font-weight:600;">{{ port_stats.get('tx_errors') }}</span>
      </div>
      {% endif %}
      {# TX/RX split bar #}
      {% set tx_b = port_stats.get('tx_bytes', 0) or 0 %}
      {% set rx_b = port_stats.get('rx_bytes', 0) or 0 %}
      {% set total_b = tx_b + rx_b %}
      {% if total_b > 0 %}
      {% set tx_pct = (tx_b / total_b * 100) | int %}
      {% set rx_pct = 100 - tx_pct %}
      <div style="margin-top:0.6rem; padding-top:0.6rem; border-top:1px solid #0f172a;">
        <div class="signal-name" style="margin-bottom:0.35rem;">Session Data</div>
        <div style="display:flex; height:10px; border-radius:5px; overflow:hidden; background:#0f172a; gap:1px;">
          <div style="width:{{ tx_pct }}%; background:#6366f1; border-radius:5px 0 0 5px; min-width:3px;"></div>
          <div style="width:{{ rx_pct }}%; background:#22c55e; border-radius:0 5px 5px 0; min-width:3px;"></div>
        </div>
        <div style="display:flex; justify-content:space-between; margin-top:0.3rem;">
          <span style="font-size:0.74rem; color:#6366f1; font-weight:600;">▲ TX &nbsp;{{ fmt_bytes(tx_b) }}</span>
          <span style="font-size:0.72rem; color:#475569;">{{ fmt_bytes(total_b) }} total</span>
          <span style="font-size:0.74rem; color:#22c55e; font-weight:600;">{{ fmt_bytes(rx_b) }}&nbsp; ▼ RX</span>
        </div>
      </div>
      {% endif %}
    </div>

    {# ── Connection ── #}
    <div class="card">
      <div class="card-title">Connection</div>
      <div class="stat-row">
        <span class="stat-label">Switch</span>
        <span class="stat-value">{{ sw_info.get('name') or wired_client.get('last_device_mac','—')|upper }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Model</span>
        <span class="stat-value">{{ sw_info.get('model') or '—' }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Port</span>
        <span class="stat-value" style="font-family:monospace;font-size:0.8rem;">{{ wired_client.get('last_port_id','—') }}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">VLAN</span>
        <span class="stat-value">
          {{ wired_client.get('last_vlan','—') }}
          {% if wired_client.get('last_vlan_name') %}
            <span style="color:#64748b; font-size:0.72rem;"> · {{ wired_client.get('last_vlan_name') }}</span>
          {% endif %}
        </span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Auth</span>
        <span class="stat-value">{{ wired_client.get('auth_method') or 'None' }}</span>
      </div>
    </div>

    {# ── Throughput ── #}
    <div class="card">
      <div class="card-title">Throughput</div>
      <div class="throughput-row">
        <span class="arrow" style="color:#6366f1;">▲</span>
        <span class="tp-label">Upload</span>
        <span class="tp-value">{{ fmt_bps(port_stats.get('rx_bps')) }}</span>
      </div>
      <div class="throughput-row">
        <span class="arrow" style="color:#22c55e;">▼</span>
        <span class="tp-label">Download</span>
        <span class="tp-value">{{ fmt_bps(port_stats.get('tx_bps')) }}</span>
      </div>
    </div>

    {# ── Port History ── #}
    {% set port_history = wired_client.get('device_mac_port', []) %}
    {% if port_history | length > 1 %}
    <div class="card full-width">
      <div class="card-title">Port History (this session)</div>
      <div style="display:flex;flex-wrap:wrap;gap:0.45rem;margin-top:0.2rem;">
        {% for entry in port_history %}
        {% set entry_sw = switch_names.get(entry.get('device_mac','').lower(), {}) %}
        {% set is_current = (entry.get('port_id') == wired_client.get('last_port_id') and entry.get('device_mac') == wired_client.get('last_device_mac')) %}
        <div class="ap-chip" style="border-color:{{ '#22c55e' if is_current else '#334155' }};">
          <span class="ap-name">{{ entry_sw.get('name') or entry.get('device_mac','—')|upper }} · {{ entry.get('port_id','—') }}</span>
          <span class="ap-mac">VLAN {{ entry.get('vlan','—') }}{% if entry.get('ip') %} · {{ entry.get('ip') }}{% endif %}</span>
          {% if is_current %}<span class="ap-cur">● current</span>{% endif %}
        </div>
        {% endfor %}
      </div>
    </div>
    {% endif %}

  </div><!-- /wired grid -->

  {% endif %}{# end stats / wired_client #}

  {# ── Marvis Buttons — shared for both client types ── #}
  {% if stats or wired_client %}
  {% set btn_mac  = stats.get('mac','')                            if stats else wired_client.get('mac','') %}
  {% set btn_site = (client.get('site_id','') if client else '')   if stats else wired_client.get('site_id','') %}
  <div class="btn-row">
    <button class="marvis-btn marvis-btn-1h"
            data-mac="{{ btn_mac }}" data-site="{{ btn_site }}"
            data-hours="1" data-target="marvis-1h"
            onclick="runTroubleshoot(this)">
      ⚡ Marvis — Last Hour
    </button>
    <button class="marvis-btn marvis-btn-7d"
            data-mac="{{ btn_mac }}" data-site="{{ btn_site }}"
            data-hours="168" data-target="marvis-7d"
            onclick="runTroubleshoot(this)">
      🔬 Marvis — Last 7 Days
    </button>
  </div>

  <div id="marvis-1h" class="marvis-section" style="display:none;">
    <div class="marvis-section-title">Marvis AI Troubleshoot — Last Hour</div>
    <div class="marvis-results"></div>
  </div>

  <div id="marvis-7d" class="marvis-section" style="display:none;">
    <div class="marvis-section-title">Marvis AI Troubleshoot — Last 7 Days</div>
    <div class="marvis-results"></div>
  </div>
  {% endif %}

  <div class="btn-row">
    <a href="{{ '/' + client_ip if ip_override else '/' }}" class="refresh-btn">↺ Refresh</a>
    {% if mailto_url %}
    <a href="{{ mailto_url }}" class="email-btn">📧 Email Support</a>
    {% endif %}
  </div>

  <footer>Mist WiFi Checker · POC · {{ now }}</footer>

</div>

<script>
  // ── Auto-refresh ─────────────────────────────────────────────────────────────
  const REFRESH_SECS = 30;
  let timeLeft     = REFRESH_SECS;
  let manualPaused = false;
  let marvisPaused = false;   // set true the moment a Marvis panel opens

  function isPaused() { return manualPaused || marvisPaused; }

  function updateRefreshUI() {
    const el = document.getElementById('ri-count');
    if (!el) return;
    if (isPaused()) {
      el.textContent = '⏸';
      el.style.color  = '#f59e0b';
    } else {
      el.textContent = timeLeft + 's';
      el.style.color  = timeLeft <= 5 ? '#ef4444' : '#64748b';
    }
  }

  function togglePause() {
    manualPaused = !manualPaused;
    if (!manualPaused) { timeLeft = REFRESH_SECS; }   // reset countdown on resume
    updateRefreshUI();
  }

  const refreshTimer = setInterval(() => {
    if (isPaused()) return;
    timeLeft--;
    updateRefreshUI();
    if (timeLeft <= 0) {
      clearInterval(refreshTimer);
      window.location.reload();
    }
  }, 1000);

  updateRefreshUI();   // paint initial state immediately

  // ── Marvis ───────────────────────────────────────────────────────────────────
  const CAT_COLOURS = {
    'Connectivity':  '#ef4444',
    'Client':        '#f59e0b',
    'Device Health': '#3b82f6',
  };

  function parseRec(text) {
    const steps = text.split(/\\s*\\d+\\.\\s+/).filter(Boolean);
    if (steps.length <= 1) return `<p class="marvis-text" style="margin-top:0.4rem">${text}</p>`;
    return '<ol class="marvis-rec">' + steps.map(s => `<li>${s.trim()}</li>`).join('') + '</ol>';
  }

  function renderFindings(results) {
    if (!results || results.length === 0)
      return '<div class="marvis-ok">✓ No issues detected by Marvis for this client</div>';
    return results.map(r => {
      const col = CAT_COLOURS[r.category] || '#6366f1';
      const rec = r.recommendation ? parseRec(r.recommendation) : '';
      return `
        <div class="marvis-card" style="border-left-color:${col}">
          <div class="marvis-header">
            <span class="marvis-category" style="color:${col}">${r.category}</span>
            ${r.reason ? `<span class="marvis-reason">${r.reason}</span>` : ''}
          </div>
          <p class="marvis-text">${r.text}</p>${rec}
        </div>`;
    }).join('');
  }

  async function runTroubleshoot(btn) {
    const targetId = btn.dataset.target;
    const section  = document.getElementById(targetId);
    const results  = section.querySelector('.marvis-results');
    const mac      = btn.dataset.mac;
    const siteId   = btn.dataset.site;
    const hours    = btn.dataset.hours;

    // Pause auto-refresh as soon as a Marvis panel is opened
    marvisPaused = true;
    updateRefreshUI();

    btn.disabled = true;
    btn.textContent = '⏳ Querying Marvis…';
    section.style.display = 'block';
    results.innerHTML = '<div class="marvis-spinner"><div class="spinner-ring"></div>Running Marvis AI analysis…</div>';

    try {
      const resp = await fetch(
        `/api/troubleshoot?mac=${encodeURIComponent(mac)}&site_id=${encodeURIComponent(siteId)}&hours=${hours}`
      );
      const data = await resp.json();
      results.innerHTML = resp.ok
        ? renderFindings(data.results)
        : `<div class="marvis-text" style="color:#f87171">⚠ ${data.error || 'Unknown error'}</div>`;
    } catch(e) {
      results.innerHTML = `<div class="marvis-text" style="color:#f87171">⚠ Request failed: ${e.message}</div>`;
    }

    btn.disabled = false;
    btn.textContent = btn.dataset.hours === '1' ? '⚡ Run Again' : '🔬 Run Again';
  }

  function openWiFiInfoModal() {
    const modal = document.getElementById('wifiInfoModal');
    if (modal) modal.style.display = 'flex';
  }

  function closeWiFiInfoModal(event) {
    const modal = document.getElementById('wifiInfoModal');
    if (modal && (!event || event.target.id === 'wifiInfoModal')) {
      modal.style.display = 'none';
    }
  }

  function openWiredInfoModal() {
    const modal = document.getElementById('wiredInfoModal');
    if (modal) modal.style.display = 'flex';
  }

  function closeWiredInfoModal(event) {
    const modal = document.getElementById('wiredInfoModal');
    if (modal && (!event || event.target.id === 'wiredInfoModal')) {
      modal.style.display = 'none';
    }
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      const modal1 = document.getElementById('wifiInfoModal');
      const modal2 = document.getElementById('wiredInfoModal');
      if (modal1) modal1.style.display = 'none';
      if (modal2) modal2.style.display = 'none';
    }
  });
</script>

<div id="wifiInfoModal" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.7); z-index:1000; align-items:center; justify-content:center;" onclick="closeWiFiInfoModal(event)">
  <div style="background:#1e293b; border:1px solid #334155; border-radius:12px; padding:1.5rem; max-width:500px; width:90%; box-shadow:0 10px 40px rgba(0,0,0,0.5);" onclick="event.stopPropagation()">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem;">
      <h2 style="font-size:1.1rem; color:#f1f5f9; margin:0;">WiFi Metrics</h2>
      <button style="background:none; border:none; color:#94a3b8; cursor:pointer; font-size:1.5rem; padding:0; width:28px; height:28px;" onclick="closeWiFiInfoModal()">✕</button>
    </div>
    <div style="color:#cbd5e1; font-size:0.9rem; line-height:1.5;">
      {{ wifi_info_text }}
    </div>
  </div>
</div>

<div id="wiredInfoModal" style="display:none; position:fixed; top:0; left:0; right:0; bottom:0; background:rgba(0,0,0,0.7); z-index:1000; align-items:center; justify-content:center;" onclick="closeWiredInfoModal(event)">
  <div style="background:#1e293b; border:1px solid #334155; border-radius:12px; padding:1.5rem; max-width:500px; width:90%; box-shadow:0 10px 40px rgba(0,0,0,0.5);" onclick="event.stopPropagation()">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:1rem;">
      <h2 style="font-size:1.1rem; color:#f1f5f9; margin:0;">Wired Connection Info</h2>
      <button style="background:none; border:none; color:#94a3b8; cursor:pointer; font-size:1.5rem; padding:0; width:28px; height:28px;" onclick="closeWiredInfoModal()">✕</button>
    </div>
    <div style="color:#cbd5e1; font-size:0.9rem; line-height:1.5;">
      {{ wired_info_text }}
    </div>
  </div>
</div>

</body>
</html>
"""


SIMPLE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Mist Network Status</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 1rem; }
    .container { max-width: 600px; margin: 0 auto; }
    header { display: flex; align-items: center; gap: 1rem; margin-bottom: 0.9rem; }
    header .title { font-size: 1.2rem; font-weight: 700; color: #f8fafc; flex: 1; }
    header-controls { display: flex; align-items: center; gap: 0.6rem; }

    .header-controls { display: flex; gap: 0.5rem; margin-left: auto; }
    .header-controls a { padding: 0.4rem 0.8rem; border-radius: 6px; border: 1px solid #334155; background: #1e293b; color: #94a3b8; font-size: 0.7rem; text-decoration: none; }
    .header-controls a:hover { background: #334155; }

    .pill { display: inline-flex; align-items: center; gap: 0.35rem; background: #1e293b; border: 1px solid #334155; border-radius: 999px; padding: 0.25rem 0.65rem; font-size: 0.75rem; color: #94a3b8; margin-bottom: 0.9rem; }
    .pill .dot { width: 6px; height: 6px; border-radius: 50%; background: #22c55e; }

    .error-box { background: #450a0a; border: 1px solid #7f1d1d; border-radius: 10px; padding: 0.9rem 1.1rem; color: #fca5a5; margin-bottom: 0.9rem; }
    .error-box strong { display: block; margin-bottom: 0.2rem; color: #f87171; }

    .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1rem; margin-bottom: 0.65rem; }
    .card-title { font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: #64748b; margin-bottom: 0.7rem; }

    .stat-row { display: flex; justify-content: space-between; align-items: center; padding: 0.6rem 0; border-bottom: 1px solid #0f172a; }
    .stat-row:last-child { border-bottom: none; }
    .stat-label { font-size: 0.85rem; color: #94a3b8; }
    .stat-value { font-size: 1rem; color: #f1f5f9; font-weight: 600; }

    .signal-indicator { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.85rem; font-weight: 600; }
    .signal-good { background: #14532d; color: #86efac; }
    .signal-fair { background: #451a03; color: #fdba74; }
    .signal-poor { background: #450a0a; color: #f87171; }

    .throughput-row { display: flex; align-items: center; gap: 0.6rem; padding: 0.6rem 0; }
    .throughput-row:first-child { padding-top: 0; }
    .arrow { font-size: 1.2rem; flex-shrink: 0; }
    .tp-value { font-size: 1rem; font-weight: 600; color: #f1f5f9; margin-left: auto; }

    .not-found { text-align: center; padding: 2.5rem 1.5rem; background: #1e293b; border: 1px solid #334155; border-radius: 12px; }
    .not-found .icon { font-size: 2.5rem; margin-bottom: 0.75rem; }
    .not-found h2 { color: #f1f5f9; margin-bottom: 0.4rem; }

    .btn-row { display: flex; gap: 0.6rem; justify-content: center; margin-top: 1.2rem; flex-wrap: wrap; }
    .marvis-btn { display: inline-flex; align-items: center; gap: 0.4rem; border: none; border-radius: 8px; padding: 0.5rem 1.1rem; font-size: 0.82rem; font-weight: 600; cursor: pointer; color: #fff; background: linear-gradient(135deg, #0ea5e9, #6366f1); }
    .marvis-btn:hover { opacity: 0.85; }
    .marvis-btn:disabled { opacity: 0.45; cursor: not-allowed; }

    .refresh-btn { background: #1e293b; color: #94a3b8; border: 1px solid #334155; border-radius: 8px; padding: 0.5rem 1.1rem; font-size: 0.82rem; font-weight: 600; cursor: pointer; text-decoration: none; }
    .refresh-btn:hover { background: #334155; }

    .email-btn { display: inline-flex; align-items: center; gap: 0.4rem; background: #1e293b; color: #22c55e; border: 1px solid #22c55e; border-radius: 8px; padding: 0.5rem 1.1rem; font-size: 0.82rem; font-weight: 600; cursor: pointer; text-decoration: none; }
    .email-btn:hover { background: #14532d; color: #86efac; }

    .marvis-section { margin-top: 0.65rem; padding: 0.8rem; background: #0f172a; border-radius: 8px; border-left: 3px solid #0ea5e9; }

    .info-btn { background: none; border: 1px solid #334155; border-radius: 50%; width: 28px; height: 28px; color: #94a3b8; cursor: pointer; font-size: 0.85rem; padding: 0; display: flex; align-items: center; justify-content: center; }
    .info-btn:hover { border-color: #64748b; color: #cbd5e1; }

    .modal { display: none; position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.7); z-index: 1000; align-items: center; justify-content: center; }
    .modal.active { display: flex; }
    .modal-content { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 1.5rem; max-width: 500px; width: 90%; box-shadow: 0 10px 40px rgba(0,0,0,0.5); }
    .modal-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem; }
    .modal-header h2 { font-size: 1.1rem; color: #f1f5f9; margin: 0; }
    .modal-close { background: none; border: none; color: #94a3b8; cursor: pointer; font-size: 1.5rem; padding: 0; width: 28px; height: 28px; }
    .modal-close:hover { color: #f1f5f9; }
    .modal-body { color: #cbd5e1; font-size: 0.9rem; line-height: 1.5; }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title">{% if client and stats %}Wireless Stats{% elif wired_client %}Wired Stats{% else %}Network Status{% endif %}</div>
      <div class="header-controls">
        {% if client and stats %}
          <button class="info-btn" onclick="openWiFiInfoModal()" title="WiFi metrics explained">ⓘ</button>
        {% elif wired_client %}
          <button class="info-btn" onclick="openWiredInfoModal()" title="Wired connection info">ⓘ</button>
        {% endif %}
        <a href="/expert{% if ip_override %}/{{ ip_override }}{% endif %}">Expert Mode</a>
      </div>
    </header>

    {% if error %}
    <div class="error-box">
      <strong>Connection Error</strong>
      {{ error }}
    </div>
    {% elif not client and not wired_client %}
    <div class="not-found">
      <div class="icon">🔍</div>
      <h2>Client Not Found</h2>
      <p>Device at {{ client_ip }} is not currently connected to a Mist network.</p>
    </div>
    {% else %}

    {# ── WIRELESS CLIENT ── #}
    {% if client and stats %}
    <div class="pill">
      <span class="dot"></span>
      WiFi client at {{ client_ip }}
    </div>

    <div class="card">
      <div class="card-title">Device</div>
      <div class="stat-row">
        <span class="stat-label">Hostname</span>
        <span class="stat-value">{{ (client.get('last_hostname') or (client.get('hostname') or ['-'])[0]) }}</span>
      </div>
      {% set username = (client.get('last_username') or (client.get('username') or [None])[0]) if client else none %}
      {% if username %}
      <div class="stat-row">
        <span class="stat-label">Username</span>
        <span class="stat-value">{{ username }}</span>
      </div>
      {% endif %}
    </div>

    <div class="card">
      <div class="card-title">Connection</div>
      <div class="stat-row">
        <span class="stat-label">Access Point</span>
        <span class="stat-value">{% if stats %}{{ ap_names.get((stats.get('ap_mac') or '').lower()) or (stats.get('ap_mac') or '—').upper()[:12] }}{% else %}—{% endif %}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Signal Strength</span>
        <span>
          {% if stats %}
            {% set rssi = stats.get('rssi') %}
            {% if rssi and rssi >= -50 %}
              <span class="signal-indicator signal-good">{{ rssi }} dBm — Excellent</span>
            {% elif rssi and rssi >= -67 %}
              <span class="signal-indicator signal-good">{{ rssi }} dBm — Good</span>
            {% elif rssi and rssi >= -80 %}
              <span class="signal-indicator signal-fair">{{ rssi }} dBm — Fair</span>
            {% else %}
              <span class="signal-indicator signal-poor">{{ rssi | default('—') }} dBm — Poor</span>
            {% endif %}
          {% else %}
            —
          {% endif %}
        </span>
      </div>
      <div class="stat-row">
        <span class="stat-label">SSID</span>
        <span class="stat-value">{% if stats %}{{ stats.get('ssid', '—') }}{% else %}—{% endif %}</span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Throughput Now</div>
      <div class="throughput-row">
        <span class="arrow">▲</span>
        <span class="stat-label">Upload</span>
        <span class="tp-value">{% if stats %}{{ fmt_bps(stats.get('rx_bps')) }}{% else %}—{% endif %}</span>
      </div>
      <div class="throughput-row">
        <span class="arrow">▼</span>
        <span class="stat-label">Download</span>
        <span class="tp-value">{% if stats %}{{ fmt_bps(stats.get('tx_bps')) }}{% else %}—{% endif %}</span>
      </div>
    </div>

    {% else %}

    {# ── WIRED CLIENT ── #}
    <div class="pill" style="background:#1e293b; border-color:#334155;">
      <span class="dot" style="background:#f97316;"></span>
      Wired client at {{ client_ip }}
    </div>

    <div class="card">
      <div class="card-title">Device</div>
      <div class="stat-row">
        <span class="stat-label">Hostname</span>
        <span class="stat-value">{% if wired_client %}{{ wired_client.get('dhcp_hostname') or '—' }}{% else %}—{% endif %}</span>
      </div>
      {% set username = (wired_client.get('last_username') or (wired_client.get('username') or [None])[0]) if wired_client else none %}
      {% if username %}
      <div class="stat-row">
        <span class="stat-label">Username</span>
        <span class="stat-value">{{ username }}</span>
      </div>
      {% endif %}
    </div>

    <div class="card">
      <div class="card-title">Connection</div>
      <div class="stat-row">
        <span class="stat-label">Switch</span>
        <span class="stat-value">{% if wired_client %}{{ switch_names.get((wired_client.get('last_device_mac') or '').lower(), {}).get('name') or '—' }}{% else %}—{% endif %}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Port</span>
        <span class="stat-value">{% if wired_client %}{{ wired_client.get('last_port_id', '—') }}{% else %}—{% endif %}</span>
      </div>
      <div class="stat-row">
        <span class="stat-label">Link Status</span>
        <span>
          {% if port_stats and port_stats.get('up') %}
            <span class="signal-indicator signal-good">Up</span>
          {% else %}
            <span class="signal-indicator signal-poor">Down</span>
          {% endif %}
        </span>
      </div>
    </div>

    <div class="card">
      <div class="card-title">Throughput Now</div>
      <div class="throughput-row">
        <span class="arrow">▲</span>
        <span class="stat-label">Upload</span>
        <span class="tp-value">{% if port_stats %}{{ fmt_bps(port_stats.get('rx_bps')) }}{% else %}—{% endif %}</span>
      </div>
      <div class="throughput-row">
        <span class="arrow">▼</span>
        <span class="stat-label">Download</span>
        <span class="tp-value">{% if port_stats %}{{ fmt_bps(port_stats.get('tx_bps')) }}{% else %}—{% endif %}</span>
      </div>
    </div>

    {% endif %}

    <div class="btn-row">
      <button id="marvis-btn" class="marvis-btn" onclick="runMarvis(1)">
        ⚡ Marvis — Last Hour
      </button>
      <button class="refresh-btn" onclick="location.href='{{ '/' + client_ip if ip_override else '/' }}'">
        🔄 Refresh
      </button>
      {% if mailto_url %}
      <a href="{{ mailto_url }}" class="email-btn">📧 Email Support</a>
      {% endif %}
    </div>

    <div id="marvis-results"></div>

    {% endif %}

    <div id="wifiInfoModal" class="modal" onclick="closeWiFiInfoModal(event)">
      <div class="modal-content" onclick="event.stopPropagation()">
        <div class="modal-header">
          <h2>WiFi Metrics</h2>
          <button class="modal-close" onclick="closeWiFiInfoModal()">✕</button>
        </div>
        <div class="modal-body">
          {{ wifi_info_text }}
        </div>
      </div>
    </div>

    <div id="wiredInfoModal" class="modal" onclick="closeWiredInfoModal(event)">
      <div class="modal-content" onclick="event.stopPropagation()">
        <div class="modal-header">
          <h2>Wired Connection Info</h2>
          <button class="modal-close" onclick="closeWiredInfoModal()">✕</button>
        </div>
        <div class="modal-body">
          {{ wired_info_text }}
        </div>
      </div>
    </div>
  </div>

  <script>
    let refreshTimer = null;

    function openWiFiInfoModal() {
      document.getElementById('wifiInfoModal').classList.add('active');
    }

    function closeWiFiInfoModal(event) {
      if (!event || event.target.id === 'wifiInfoModal') {
        document.getElementById('wifiInfoModal').classList.remove('active');
      }
    }

    function openWiredInfoModal() {
      document.getElementById('wiredInfoModal').classList.add('active');
    }

    function closeWiredInfoModal(event) {
      if (!event || event.target.id === 'wiredInfoModal') {
        document.getElementById('wiredInfoModal').classList.remove('active');
      }
    }

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        closeWiFiInfoModal();
        closeWiredInfoModal();
      }
    });

    function runMarvis(hours) {
      const btn = document.getElementById('marvis-btn');
      btn.disabled = true;
      btn.textContent = '⏳ Loading...';

      const mac = '{{ (stats.get("mac") if stats else wired_client.get("mac") if wired_client else "") }}';
      const siteId = '{{ (stats.get("site_id") if stats else wired_client.get("site_id") if wired_client else "") }}';

      fetch(`/api/troubleshoot?mac=${mac}&site_id=${siteId}&hours=${hours}`)
        .then(r => r.json())
        .then(data => {
          let html = '<div class="marvis-section"><strong>Marvis AI Results:</strong><br>';
          if (data.error) {
            html += `<span style="color:#f87171;">${data.error}</span>`;
          } else if (data.findings && data.findings.length > 0) {
            html += '<ul style="margin-left:1.2rem; margin-top:0.4rem;">';
            data.findings.forEach(f => {
              const col = f.category === 'Connectivity' ? '#ef4444' : f.category === 'Client' ? '#f59e0b' : '#3b82f6';
              html += `<li style="color:${col}; margin-bottom:0.5rem;"><strong>${f.reason}</strong><br><span style="color:#cbd5e1; font-size:0.85rem;">${f.description}</span></li>`;
            });
            html += '</ul>';
          } else {
            html += '<span style="color:#22c55e;">✓ No issues detected</span>';
          }
          html += '</div>';
          document.getElementById('marvis-results').innerHTML = html;
          btn.textContent = 'Run Again';
          btn.disabled = false;
        })
        .catch(err => {
          document.getElementById('marvis-results').innerHTML = `<div class="marvis-section" style="border-left-color:#ef4444;"><span style="color:#f87171;">Error: ${err.message}</span></div>`;
          btn.textContent = '⚡ Marvis — Last Hour';
          btn.disabled = false;
        });
    }
  </script>
</body>
</html>
"""


# ── Routes ────────────────────────────────────────────────────────────────────

def _render_dashboard(template, ip_override=None):
    """Shared logic for both simple and expert dashboard views."""
    ip     = ip_override.strip() if ip_override else get_client_ip()
    error  = None
    # Wireless
    client   = None
    stats    = None
    ap_names = {}
    # Wired
    wired_client  = None
    port_stats    = {}
    switch_names  = {}

    try:
        # ── Step 1: wireless search (may return stale records) ────────────────
        search          = mist_get(f"/orgs/{ORG_ID}/clients/search", {"ip": ip, "limit": 1})
        wireless_hits   = search.get("results", [])
        hint_site_id    = wireless_hits[0].get("site_id") if wireless_hits else None
        had_stale_wifi  = False

        if wireless_hits:
            candidate = wireless_hits[0]
            site_id   = candidate.get("site_id")
            mac       = candidate.get("mac")
            if site_id and mac:
                try:
                    stats = mist_get(f"/sites/{site_id}/stats/clients/{mac}")
                    stats.setdefault("mac", mac)
                    client   = candidate          # confirmed currently connected
                    ap_names = get_ap_names(site_id)
                except requests.HTTPError as e:
                    if e.response.status_code != 404:
                        raise
                    had_stale_wifi = True         # known but not currently on WiFi

        # ── Step 2: wired lookup (runs if no live wireless stats found) ───────
        if not stats:
            wc = search_wired_client(ip, hint_site_id=hint_site_id)
            if wc:
                wired_client = wc
                wired_site   = wired_client.get("site_id")
                if wired_site:
                    switch_mac = wired_client.get("last_device_mac", "")
                    port_id    = wired_client.get("last_port_id", "")
                    switch_names = get_switch_names(wired_site)
                    if switch_mac and port_id:
                        port_stats = get_port_stats(wired_site, switch_mac, port_id)
            elif had_stale_wifi:
                error = (
                    f"Device at {ip} was recently on WiFi but is not connected right now. "
                    "If it has switched to a wired connection, Wired Assurance must be "
                    "enabled on the Mist organisation for wired client lookup."
                )

    except requests.HTTPError as e:
        error = f"Mist API error {e.response.status_code}: {e.response.text[:300]}"
    except requests.ConnectionError:
        error = "Cannot reach the Mist API — check network connectivity."
    except Exception as exc:
        error = f"Unexpected error: {exc}"

    ts         = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mailto_url = build_mailto(
        ip, stats, client, wired_client, port_stats,
        ap_names, switch_names, request.url, ts,
    )

    return render_template_string(
        template,
        client_ip=ip, ip_override=ip_override,
        error=error,
        # wireless
        client=client, stats=stats, ap_names=ap_names,
        # wired
        wired_client=wired_client, port_stats=port_stats, switch_names=switch_names,
        # email
        mailto_url=mailto_url,
        # config
        wifi_info_text=WIFI_INFO_TEXT,
        wired_info_text=WIRED_INFO_TEXT,
        # helpers
        signal_info=signal_info, snr_info=snr_info, fmt_speed_label=fmt_speed_label,
        fmt_bps=fmt_bps, fmt_bytes=fmt_bytes,
        fmt_uptime=fmt_uptime, fmt_timestamp=fmt_timestamp,
        BAND_LABEL=BAND_LABEL, WIFI_GEN=WIFI_GEN,
        now=ts,
    )


@app.route("/")
@app.route("/<ip_override>")
def index(ip_override=None):
    return _render_dashboard(SIMPLE_TEMPLATE, ip_override)


@app.route("/expert")
@app.route("/expert/<ip_override>")
def expert(ip_override=None):
    return _render_dashboard(EXPERT_TEMPLATE, ip_override)


@app.route("/api/troubleshoot")
def api_troubleshoot():
    mac     = request.args.get("mac", "").strip()
    site_id = request.args.get("site_id", "").strip()
    hours   = min(int(request.args.get("hours", 168)), 168)   # cap at 7 days

    if not mac or not site_id:
        return jsonify({"error": "mac and site_id are required"}), 400

    try:
        start = int(time.time()) - (hours * 3600)
        data  = mist_get(f"/orgs/{ORG_ID}/troubleshoot",
                         params={"type": "client", "mac": mac, "start": start})
        results = data.get("results", [])
        for r in results:
            if r.get("recommendation"):
                steps = re.split(r"\s*\d+\.\s+", r["recommendation"])
                r["rec_steps"] = [s.strip() for s in steps if s.strip()]
        return jsonify({"results": results, "count": len(results)})

    except requests.HTTPError as e:
        code = e.response.status_code
        msg  = e.response.text[:300]
        if code == 402:
            msg = "Marvis troubleshoot requires a Marvis AI subscription on this org."
        return jsonify({"error": msg}), code
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/api/debug/<path:ip>")
def api_debug(ip):
    """Diagnostic endpoint — exposes raw network data. DISABLE IN PRODUCTION.
       Set debug_enabled = false in config.ini [mist] section to turn off."""
    if not _cfg.getboolean("mist", "debug_enabled", fallback=False):
        return jsonify({"error": "Debug endpoint is disabled. Set debug_enabled = true in config.ini to enable."}), 403
    out = {"ip": ip, "wireless": None, "wired": None, "port_stats": None,
           "switch_names": None, "if_stat_keys": None}
    try:
        ws = mist_get(f"/orgs/{ORG_ID}/clients/search", {"ip": ip, "limit": 1})
        out["wireless"] = ws.get("results", [])
    except Exception as e:
        out["wireless_error"] = str(e)

    try:
        wc = search_wired_client(ip)
        out["wired"] = wc
        if wc:
            site_id   = wc.get("site_id", "")
            sw_mac    = wc.get("last_device_mac", "")
            port_id   = wc.get("last_port_id", "")
            sw_id     = f"00000000-0000-0000-1000-{sw_mac.lower()}"
            out["switch_device_id"] = sw_id
            try:
                r       = mist_get(f"/sites/{site_id}/stats/devices/{sw_id}")
                if_stat = r.get("if_stat", {})
                out["if_stat_keys"]      = sorted(if_stat.keys())
                out["if_stat_port_raw"]  = (if_stat.get(f"{port_id}.0")
                                             or if_stat.get(port_id)
                                             or "not found")
                # Also try the richer switch_ports/search endpoint
                for spath in [f"/sites/{site_id}/stats/switch_ports/search",
                               f"/orgs/{ORG_ID}/stats/switch_ports/search"]:
                    try:
                        sr    = mist_get(spath, params={"mac": sw_mac})
                        ports = sr if isinstance(sr, list) else sr.get("results", sr.get("data", []))
                        for p in ports:
                            if p.get("port_id") == port_id:
                                out["switch_port_search_raw"] = p
                                break
                        if out.get("switch_port_search_raw"):
                            break
                    except Exception as se:
                        out[f"switch_port_search_error_{spath.split('/')[-3]}"] = str(se)
                out["port_stats"]        = get_port_stats(site_id, sw_mac, port_id)
                out["switch_names"]      = get_switch_names(site_id)
            except Exception as e:
                out["device_stats_error"] = str(e)
    except Exception as e:
        out["wired_error"] = str(e)

    return jsonify(out)


@app.route("/health")
def health():
    return {"status": "ok"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", APP_PORT))
    print(f"\n  Mist WiFi Checker  →  http://0.0.0.0:{port}")
    print(f"  API  : {MIST_BASE}")
    print(f"  Org  : {ORG_ID or '(not set — edit config.ini)'}\n")
    app.run(host="0.0.0.0", port=port, debug=False)
