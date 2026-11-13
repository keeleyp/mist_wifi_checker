# Mist WiFi Checker

A self-service Flask web application that enables clients connected to Juniper Mist Access Points (APs) to view their own device's network status in real-time and optionally run Marvis AI diagnostics.

## Features

✨ **Client Self-Service Dashboard**
- View real-time wireless and wired network statistics
- Works for both WiFi and wired (Ethernet) clients
- No manual MAC address or IP entry required — automatically detects client via HTTP source IP

📊 **Real-Time Statistics**
- **Wireless clients**: RSSI, SNR, throughput, retry rate (with color-coded severity), connection duration, roaming history with AP names
- **Wired clients**: Link speed, link status, PoE details, power draw, port errors, throughput, port information, hostname
- Signal quality visualization (TX/RX ratio) and data throughput charts
- Session data summary bar (wireless and wired)

🔧 **Diagnostic Integration**
- One-click Marvis AI troubleshooting (7-day and 1-hour options)
- Pre-filled email support button with full diagnostic data
- Expert mode for advanced users with detailed AP and port information

📱 **Responsive Design**
- Works on desktop, tablet, and mobile browsers
- Unified header design across all screen sizes
- Auto-refresh with intelligent pausing when diagnostics are running

## Quick Start

### Prerequisites
- Python 3.7+
- Juniper Mist API access (API token, Organization ID, base URL)
- Network connectivity to Mist API

### Installation

1. Clone this repository:
```bash
git clone https://github.com/keeleyp/mist_wifi_checker.git
cd mist_wifi_checker
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create `config.ini` with your Mist API credentials:
```ini
[mist]
# Required: Your Mist API token
# Obtain from Mist Dashboard: Org → Settings → API Tokens
api_token = YOUR_API_TOKEN_HERE

# Required: Your Mist Organization ID
# Found in Mist Dashboard: Org Settings
org_id = YOUR_ORG_ID_HERE

# Required: Mist API base URL
# Standard: https://api.gc3.mist.com/api/v1
base_url = https://api.gc3.mist.com/api/v1

# Optional: Port to run the Flask server on (default: 80)
port = 80

# Optional: Email address shown in the support button
# Users can click to send diagnostics to this address
support_email = helpdesk@example.com

# Optional: Enable debug logging (default: false)
# Set to true to see verbose API calls and troubleshooting info
debug_enabled = false

# Optional: Custom explanatory text for WiFi clients (max 1000 characters)
# wifi_info_text = The information shown here is how the WiFi network hears your client...

# Optional: Custom explanatory text for wired clients (max 1000 characters)
# wired_info_text = The information shown here is your wired Ethernet connection status...
```

Or copy the included template:
```bash
cp config.ini.example config.ini
# Then edit config.ini with your credentials
```

4. Run the application:
```bash
python app.py
```

The app will start on `http://localhost:80` (or your configured port).

## Configuration

### Required Settings
- **api_token**: Your Mist API token (obtain from Mist dashboard: Org → Settings → API Tokens)
- **org_id**: Your Mist Organization ID
- **base_url**: Mist API endpoint (typically `https://api.gc3.mist.com/api/v1`)

### Optional Settings
- **port** (default: 80): Port for the Flask server
- **support_email**: Email address shown in the support button
- **debug_enabled** (default: false): Enable debug logging
- **wifi_info_text**: Custom explanatory text for WiFi clients (max 1000 chars)
- **wired_info_text**: Custom explanatory text for wired clients (max 1000 chars)

## Deployment

### Raspberry Pi / Linux Server

Copy the app folder to the Pi, create `config.ini` from `config.ini.example`, then run the setup script from that folder as your normal user (not with `sudo`; it calls `sudo` itself where needed):

```bash
cp config.ini.example config.ini   # then add api_token, org_id, base_url
bash setup_pi.sh
```

This will:
- Create (or rebuild, if broken) the Python virtual environment in `.venv`
- Create `config.ini` from the example if it is missing
- Install and enable the `mist-wifi-checker` systemd service, so the app starts on boot and restarts if it crashes
- Run the app as your user while allowing it to listen on port 80 (`CAP_NET_BIND_SERVICE`), with no root needed

The app reads all settings, including `port`, from `config.ini` each time it starts. After editing it:

```bash
sudo systemctl restart mist-wifi-checker
```

From a Mac or Linux machine you can instead run `./deploy.sh <pi-ip> <user>`, which copies the files (never your `config.ini`) and runs `setup_pi.sh` for you.

Useful commands:

```bash
sudo systemctl status mist-wifi-checker --no-pager
sudo journalctl -u mist-wifi-checker -f
```

### Docker (Optional)

Create a `Dockerfile` if needed for containerized deployment.

## How It Works

### Client Detection
1. Client connects to Mist AP over WiFi or wired connection
2. Client opens the app URL in a browser (e.g., `http://192.168.1.1/`)
3. App reads the client's source IP from the HTTP request
4. App queries Mist API to find the client by IP address
5. Dashboard displays the client's own network statistics

### API Integration
- Uses Juniper Mist `/clients/search` endpoint for client detection
- Queries device stats for AP performance metrics
- Retrieves switch port stats for wired client details (PoE, errors, speed)
- Integrates with Marvis API for on-demand troubleshooting

## File Structure

```
mist_wifi_checker/
├── app.py                      # Main Flask application (~1700 lines)
│                               # Includes SIMPLE_TEMPLATE and EXPERT_TEMPLATE
├── config.ini.example          # Configuration template (copy to config.ini and fill in credentials)
├── requirements.txt            # Python dependencies (Flask, Requests, Gunicorn)
├── setup_pi.sh                 # Raspberry Pi systemd service setup script
├── deploy.sh                   # Deployment helper script
├── USER_GUIDE.md               # End-user guide with screen examples
├── README.md                   # This documentation
├── CHANGELOG.md                # Version history and feature list
└── LICENSE                     # MIT License
```

## Usage

### For End Users
1. Connect to a Mist AP (WiFi or wired)
2. Open a web browser
3. Navigate to the configured URL (e.g., `http://192.168.1.1/`)
4. View your device's network status
5. Click "Run Marvis" to diagnose issues
6. Click "Email Support" to send diagnostics to your IT team

### For Administrators
- Monitor which clients are accessing the application
- Use debug logs to troubleshoot connection issues
- Customize support email and info text in `config.ini`
- Adjust port and other settings as needed

## Troubleshooting

### Client Not Found
- Ensure client is connected to the Mist AP
- Check that Mist API credentials are correct
- Verify network connectivity to `api.gc3.mist.com`

### Missing Hostname on Wired Clients
- Ensure client has a DHCP lease assigned
- Check that AP/switch has DHCP snooping enabled

### PoE Status Not Showing
- Confirm the port supports PoE
- Verify switch_ports/search endpoint access in Mist API

### Auto-Refresh Not Working
- Check browser console for JavaScript errors
- Verify API token hasn't expired
- Ensure `debug_enabled = false` in production (may interfere with refresh)

## Technologies

- **Backend**: Flask (Python web framework)
- **Frontend**: Vanilla JavaScript, HTML, CSS
- **APIs**: Juniper Mist Cloud API
- **Server**: Gunicorn (production), Flask dev server (development)
- **OS**: Linux/Raspberry Pi recommended

## Dependencies

See `requirements.txt`:
- Flask
- Requests
- Gunicorn

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

## Support

For issues, feature requests, or contributions, please:
1. Check the troubleshooting section above
2. Review the included documentation guides
3. Open an issue on GitHub

## Contributing

Contributions welcome! Please ensure:
- Code follows existing style conventions
- Changes are tested on both WiFi and wired clients
- Documentation is updated if behavior changes

---

**Created for Juniper Mist deployments** | Works with wireless APs and wired switches that report to Mist
