# Changelog

All notable changes to the Mist WiFi Checker project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-06-18

### Added
- Initial release of Mist WiFi Checker
- Auto-detection of WiFi clients via HTTP source IP
- Auto-detection of wired clients via HTTP source IP
- Real-time wireless statistics display (RSSI, SNR, throughput, retry rate, roaming history)
- Real-time wired statistics display (link speed, link status, PoE details, port errors)
- Simple and Expert mode UI templates
- Signal quality visualization with throughput charts
- Session data summary bar
- Marvis AI integration with 7-day and 1-hour diagnostic buttons
- Email support button with pre-filled diagnostic data
- Responsive design for desktop, tablet, and mobile
- Auto-refresh with intelligent pausing when Marvis panels open
- Support for Virtual Chassis and Multi-Gigabit Ethernet (MGE) ports
- Configuration via config.ini file
- Debug logging capability
- Systemd service setup for Raspberry Pi
- Docker deployment support via setup scripts

### Technical Features
- IP-based client identification using HTTP request headers
- Juniper Mist API integration (clients/search, device stats, switch_ports/search, Marvis)
- AP-centric statistics interpretation
- Retry rate calculation as percentage with color-coded severity thresholds
- Port speed inference for Ethernet ports (et-, xe-, mge-, ge-, me-, fe- prefixes)
- PoE data retrieval from switch_ports/search endpoint
- Hostname fallback logic (dhcp_hostname for wired, last_hostname for WiFi)
- Unified header design across all screen sizes

### Fixed
- TX/RX direction correction for WiFi clients (now correctly shows download/upload)
- MCS index removed from display (not available in live API data)
- Auto-refresh countdown fix (proper cleanup of setInterval)
- Manual URL IP preservation on refresh
- Wired client MGE port stats population
- PoE status display for wired clients
- Mobile header layout and responsive design
- Wired hostname display on simple screen

---

## Development Notes

### Known Limitations
- Requires active Mist API token with org-level access
- Client IP must be visible to the Flask application (behind transparent proxy)
- PoE data only available for managed switches reporting to Mist

### Future Enhancements
- Batch client search for bulk diagnostics
- Historical data trending and analytics
- Configurable refresh intervals
- LDAP/SSO integration for support email routing
- Dark mode toggle
- Multi-language support
