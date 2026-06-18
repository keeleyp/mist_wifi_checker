# Changelog

All notable changes to the Mist WiFi Checker project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-18

### Features
- **Auto-Detection**: Clients automatically identified via HTTP source IP—no manual MAC/IP entry required
- **Dual Mode Support**: Works seamlessly for both WiFi and wired (Ethernet) clients
- **Real-Time Wireless Stats**: RSSI, SNR, throughput, retry rate, connection duration, roaming history with AP names and MACs
- **Real-Time Wired Stats**: Link speed, link status, PoE mode/priority/status, power draw, port errors, throughput, hostname
- **Signal Quality Visualization**: TX/RX ratio chart showing download vs. upload ratio
- **Session Data Summary**: Bar showing connection duration and total data transferred
- **Simple & Expert Modes**: Clean dashboard for end-users, detailed view for troubleshooting
- **Marvis AI Integration**: One-click diagnostic buttons (7-day and 1-hour options)
- **Email Support**: Pre-filled diagnostic data with client stats and Marvis output
- **Responsive Design**: Works on desktop, tablet, and mobile browsers
- **Smart Auto-Refresh**: Pauses when Marvis diagnostics are running, resumes automatically
- **Configuration File**: ConfigParser-based config.ini with optional customization
- **Production Ready**: Systemd service setup for Raspberry Pi/Linux servers with Gunicorn

### Technical Features
- IP-based client detection using HTTP request headers
- Juniper Mist API integration:
  - `/clients/search` for WiFi and wired client discovery
  - `/devices/{id}/stats` for AP performance metrics
  - `/switch_ports/search` for rich wired port data (PoE, errors, real speed, STP)
  - Marvis `/troubleshoot` endpoints for diagnostics
- AP-centric statistics interpretation (tx_bytes = download, rx_bytes = upload)
- Retry rate calculation as percentage with color-coded severity thresholds (green < 3%, yellow 3-10%, red > 10%)
- Port speed inference for Ethernet ports (et-=100G, xe-=10G, mge-=10G, ge-/me-=1G, fe-=100M)
- PoE data retrieval from switch_ports/search endpoint (includes poe_on, power_draw, poe_mode, poe_priority, poe_status_detail)
- Hostname field selection (dhcp_hostname for wired clients, last_hostname for WiFi clients)
- Virtual Chassis support (02-prefixed MAC address handling)
- Flexible port stat lookup with multiple format support (port_id.0, port_id, case-insensitive scan)
- Unified header design across all screen sizes

### Fixes & Refinements
- **TX/RX Direction**: Corrected display order to show download (TX from AP) and upload (RX to AP) properly
- **MCS Index**: Removed from display—not available in Mist's live data endpoints
- **Auto-Refresh Countdown**: Fixed negative countdown by properly clearing setInterval
- **URL IP Override**: Manual IP in URL persists across page refreshes
- **Wired Client MGE Ports**: Added mge- prefix support in port speed inference; improved if_stat key lookup with fallback logic
- **PoE Status**: Switched from if_stat (no PoE data) to switch_ports/search endpoint (rich data)
- **Email Button Styling**: Added CSS for proper button appearance with hover state
- **Mobile Header Layout**: Unified single-row header for all screen sizes (removed responsive stacking)
- **Wired Hostname on Simple View**: Fixed field selection (use dhcp_hostname instead of last_hostname)

---

## Development Notes

### Known Limitations
- Requires active Mist API token with org-level access
- Client IP must be visible to the Flask application (works with direct AP connection; may need transparent proxy for captive portal setups)
- PoE data only available for managed switches reporting to Mist
- Hostname resolution depends on DHCP snooping or active DHCP assignment

### Future Enhancements
- Batch client search for network-wide diagnostics
- Historical data trending and performance analytics
- Configurable auto-refresh intervals
- LDAP/SSO integration for support email routing
- Dark mode toggle
- Multi-language support
- Containerized deployment (Docker image)
