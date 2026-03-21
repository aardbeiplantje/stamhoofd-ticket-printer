# Stamhoofd MX10 Thermal Printer Daemon

Automated order printing daemon that integrates with Stamhoofd webshops to automatically print order tickets on MX10 thermal printers via Bluetooth Low Energy.

## Quick Start

Install dependencies:
```bash
pip install -r requirements.txt
```

Configure environment variables and run:
```bash
export STAMHOOFD_ORG_ID="your-org-id"
export STAMHOOFD_WEBSHOP_ID="your-webshop-id"
export STAMHOOFD_API_KEY="your-api-key"
export MX10_BLE_ADDRESS="1A:11:27:22:D3:91"
./stamhoofd.py
```

## Requirements

- **System**: Linux with BlueZ Bluetooth stack
- **Python**: 3.7 or later
- **Packages**: Pillow, requests, pytz
- **Hardware**: Bluetooth adapter + MX10 thermal printer
- **Network**: Access to Stamhoofd API

## Installation

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Install BlueZ (if not already installed):
```bash
# Ubuntu/Debian
sudo apt-get install bluez bluetooth

# Fedora/RHEL
sudo dnf install bluez
```

3. Start Bluetooth service:
```bash
sudo systemctl start bluetooth
```

4. Find your printer's Bluetooth address:
```bash
bluetoothctl
[bluetooth]# scan on
# Wait for MX10 to appear
[bluetooth]# devices
# Find: Device 1A:11:27:22:D3:91 MX10
```

## stamhoofd.py (Order Printer Daemon)

Automated order printing daemon that monitors a Stamhoofd webshop for new orders and prints tickets to the MX10 printer via native BLE connection.

**Features:**
- Polls Stamhoofd API every 15 seconds for new orders
- Native BLE support (no subprocess call to Perl script)
- Pillow-based text rendering (no ImageMagick dependency)
- Persistent connection with keepalive to keep printer awake when idle
- Automatic deduplication via filesystem tracking
- Configurable fonts and rendering parameters
- INFO-level logging with timestamps and order details
- Graceful error handling with automatic reconnect
- Multi-tenant support (organizes orders by org/webshop ID)

**Requirements:**
```bash
pip install -r requirements.txt
```

Also requires:
- Python 3.7+
- Linux with BlueZ Bluetooth stack
- Network access to Stamhoofd API

**Setup:**

1. Set required environment variables:
```bash
export STAMHOOFD_ORG_ID="your-org-id"
export STAMHOOFD_WEBSHOP_ID="your-webshop-id"
export STAMHOOFD_API_KEY="your-api-key"
export MX10_BLE_ADDRESS="1A:11:27:22:D3:91"
```

2. Optionally configure printer and rendering:
```bash
export MX10_BLE_ADDR_TYPE="public"          # or "random" for some devices
export MX10_FONT_SIZE="24"                   # font size in pixels
export MX10_FONT_PATH="/path/to/font.ttf"   # custom font (optional)
export MX10_KEEPALIVE_SECONDS="12"          # idle keepalive interval
export STAMHOOFD_PRINTED_BASE_DIR="printed_orders"  # where to store order state
```

3. Run the daemon:
```bash
./stamhoofd.py
```

**Usage Examples:**

Run with default settings:
```bash
STAMHOOFD_ORG_ID=abc123 \
STAMHOOFD_WEBSHOP_ID=xyz789 \
STAMHOOFD_API_KEY=sk_live_xxxxx \
MX10_BLE_ADDRESS=1A:11:27:22:D3:91 \
./stamhoofd.py
```

With custom font and size:
```bash
MX10_FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf \
MX10_FONT_SIZE=28 \
STAMHOOFD_ORG_ID=abc123 \
STAMHOOFD_WEBSHOP_ID=xyz789 \
STAMHOOFD_API_KEY=sk_live_xxxxx \
MX10_BLE_ADDRESS=1A:11:27:22:D3:91 \
./stamhoofd.py
```

Run as systemd service (see [Systemd Setup](#systemd-setup) below).

**How It Works:**

1. **Polling Loop**: Connects to Stamhoofd API every 15 seconds to fetch new orders
2. **Order Deduplication**: Marks orders as printed using JSON files in `STAMHOOFD_PRINTED_BASE_DIR/<org>/<webshop>/<order_id>.json.printed`
3. **BLE Connection**: Maintains persistent BLE connection to MX10 printer
4. **Keepalive**: Sends low-overhead heartbeat command to keep printer from going to sleep when idle
5. **Receipt Generation**: Formats order data as readable ticket with:
   - Table number
   - Order number  
   - Customer email
   - Timestamp
   - Item list with quantities
6. **Pillow Rendering**: Converts text to 384-pixel-wide bitmap rows for thermal printer
7. **Printing**: Sends bitmap rows over BLE to printer
8. **State Tracking**: Renames order file to `.printed` on successful print, or skips printing if order has no items

**Log Output Example:**
```
2026-03-21 19:50:05 - __main__ - INFO - Starting order watcher
2026-03-21 19:50:05 - __main__ - INFO - Polling URL: https://2a68cc9c-23c4-4d77-b720-97b40b0e422f.api.stamhoofd.app/v191/webshop/9cb2150e-6101-47cc-ae11-1f811a432082/orders
2026-03-21 19:50:05 - __main__ - INFO - Printed-order store: /home/user/stamhoofd-printer/printed_orders/2a68cc9c-23c4-4d77-b720-97b40b0e422f/9cb2150e-6101-47cc-ae11-1f811a432082
2026-03-21 19:50:20 - __main__ - INFO - Printing new order #42 (id=abc12345) tafel=5 items=[3x Beer, 2x Wine]
2026-03-21 19:50:25 - __main__ - INFO - Printed order #42 (id=abc12345)
2026-03-21 19:50:30 - __main__ - INFO - Sleeping for 15 seconds
```

**Environment Variables Reference:**

**Required:**
- `STAMHOOFD_ORG_ID` - Stamhoofd organization ID (UUID format)
- `STAMHOOFD_WEBSHOP_ID` - Stamhoofd webshop ID (UUID format)
- `STAMHOOFD_API_KEY` - Stamhoofd API key (Bearer token)
- `MX10_BLE_ADDRESS` - Bluetooth address of MX10 printer (XX:XX:XX:XX:XX:XX)

**Optional:**
- `MX10_BLE_ADDR_TYPE` - BLE address type: `public` (default) or `random`
- `MX10_FONT_SIZE` - Font size in pixels (default: `24`)
- `MX10_FONT_PATH` - Path to TrueType font file (default: system DejaVu Sans)
- `MX10_KEEPALIVE_SECONDS` - Idle keepalive interval in seconds (default: `12`)
- `STAMHOOFD_PRINTED_BASE_DIR` - Base directory for order state files (default: `printed_orders`)

**Troubleshooting:**

**BLE connect fails with "Invalid argument":**
```bash
# Try with address type override
MX10_BLE_ADDR_TYPE=random ./stamhoofd.py
```

**Printer doesn't wake up / keeps going to sleep:**
- Lower `MX10_KEEPALIVE_SECONDS` (e.g., to `6` or `9`)
- Check printer hasn't entered deep sleep mode (press button to wake if needed)

**Orders not being printed:**
- Check `STAMHOOFD_PRINTED_BASE_DIR` exists and is writable
- Verify API key and org/webshop IDs are correct
- Look at logs for API errors (403, 404, etc.)

**Font rendering looks wrong:**
- Set custom font: `MX10_FONT_PATH=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`
- Adjust size: `MX10_FONT_SIZE=20` (smaller = more text per line)

**Systemd service won't start:**
- Check logs: `sudo journalctl -u stamhoofd-printer -f`
- Verify user has permission to access Bluetooth and working directory
- Ensure all required environment variables are set in `/etc/stamhoofd-printer.env`

## Systemd Setup

A service file is provided at `stamhoofd-printer.service` that:
- **Auto-restart**: Restarts every 5 seconds if the service dies
- **Restart limits**: Maximum 3 restarts within 60 seconds to prevent infinite restart loops
- **Bluetooth dependency**: Waits for the Bluetooth subsystem before starting
- **Logging**: All output goes to the systemd journal
- **Security**: Runs with restricted filesystem access
- Reads environment variables from `/etc/stamhoofd-printer.env`

**1. Install host dependencies (preferred for systemd services on Debian/Ubuntu):**
```bash
sudo apt-get update
sudo apt-get install -y bluez bluetooth python3 python3-pil python3-requests python3-pytz
```

If one of these Python packages is unavailable in your distro, use this fallback:
```bash
sudo /usr/bin/python3 -m pip install -r requirements.txt
```

**2. Install the script:**
```bash
sudo cp stamhoofd.py /usr/local/bin/
sudo chmod +x /usr/local/bin/stamhoofd.py
```

**3. Create the working and data directories:**
```bash
sudo mkdir -p /var/lib/stamhoofd-printer/printed_orders
```

**4. Copy the service file:**
```bash
sudo cp stamhoofd-printer.service /etc/systemd/system/
```

**5. Create the environment config file:**
```bash
sudo cp stamhoofd-printer.env.example /etc/stamhoofd-printer.env
sudo nano /etc/stamhoofd-printer.env   # fill in your actual values
```

**6. Set proper permissions on the env file:**
```bash
sudo chmod 600 /etc/stamhoofd-printer.env
```

**7. Reload systemd and enable the service:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now stamhoofd-printer.service
```

**Check service status:**
```bash
sudo systemctl status stamhoofd-printer.service
```

**Follow live logs:**
```bash
sudo journalctl -u stamhoofd-printer.service -f
```

**View recent logs:**
```bash
sudo journalctl -u stamhoofd-printer.service -n 50
```

**Stop / restart / disable:**
```bash
sudo systemctl stop stamhoofd-printer.service
sudo systemctl restart stamhoofd-printer.service
sudo systemctl disable stamhoofd-printer.service
```

**Troubleshooting the service:**

If the service fails to start, check:
1. Environment variables are set correctly in `/etc/stamhoofd-printer.env`
2. Bluetooth is available: `bluetoothctl show`
3. Logs for errors: `sudo journalctl -u stamhoofd-printer.service -n 100`

## Architecture

### Stamhoofd Order Printer Flow
```
Stamhoofd API
    ↓
[Poll every 15 seconds]
    ↓
[New order detected]
    ↓
[Python stamhoofd.py]
    ↓
[Pillow text rendering]
    ↓
[BLE socket(AF_BLUETOOTH)]
    ↓
[L2CAP CID=4 (ATT)]
    ↓
[Bluetooth 2.4GHz RF]
    ↓
[MX10 Printer]
```

### MX10 Command Packet Structure
```
[0x51][0x78][COMMAND][DATA...][0xFF]
  │     │       │         │      └─ End marker
  │     │       │         └───────── Variable data
  │     │       └─────────────────── MX10 command code
  │     └─────────────────────────── Prefix byte
  └───────────────────────────────── Prefix byte
```

## Performance

- **BLE Connection**: ~2-5 seconds
- **Text rendering**: ~100-500 ms per order
- **Printing**: ~1-3 seconds per ticket
- **Paper feeding**: ~200-300 ms
- **API Poll latency**: ~5-15 seconds (15-second poll interval + network)
- **Typical end-to-end**: 10-30 seconds from order placement to print

## Architecture Details

### Persistent Connection with Keepalive

The daemon maintains a persistent BLE connection to avoid reconnection delays when orders arrive frequently. A low-cost keepalive heartbeat (GET_DEVICE_STATE command) is sent every 12 seconds (configurable) while idle to prevent the printer from going to sleep.

### Order Deduplication

Orders are marked as printed using filesystem state files:
```
printed_orders/
├── <org_id>/
│   └── <webshop_id>/
│       ├── <order_id>.json        (cached order)
│       └── <order_id>.json.printed (marks as printed)
```

Order IDs are checked before API results are processed. Once an order file has `.printed` extension, it won't be printed again even if polled multiple times.

### Pillow Text Rendering

Text is rasterized to monochrome 384×n pixel bitmaps using Pillow and system TTF fonts. The bitmap is packed into 48-byte rows (384 pixels ÷ 8 bits) and sent to the printer via the MX10 CAT binary protocol.

## References

For Perl-based low-level BLE debugging and standalone printer scripts, see:
- [stamhoofd-mx10-printer](https://github.com/yourusername/stamhoofd-mx10-printer) - Separate repository with Perl MX10 tools

## Support

Having issues? 

**Check these first:**
- Verify all required environment variables are set
- Confirm Bluetooth address is correct: `bluetoothctl devices`
- Ensure printer is powered on and in range
- Check Bluetooth service is running: `sudo systemctl status bluetooth`

**Common fixes:**
- BLE connection fails: Try `MX10_BLE_ADDR_TYPE=random`
- Printer goes to sleep: Lower `MX10_KEEPALIVE_SECONDS` 
- Font issues: Set `MX10_FONT_PATH` to a known TTF path
- API errors: Verify Stamhoofd API key and org/webshop IDs

**Debug mode:**
- Watch the order state directory: `ls -la printed_orders/*/*/`
- Monitor systemd logs: `sudo journalctl -u stamhoofd-printer -f`
- Run directly with verbose output to see timing and errors

## License

See header comments in stamhoofd.py for license information.

## Contributing

Improvements welcome! Areas for enhancement:
- Custom receipt formatting
- Multi-printer support
- Order history/analytics
- Print queue management

Please test thoroughly with your Stamhoofd setup before submitting changes.

---

**Last Updated**: March 2026
**Python Version**: 3.7+
**Tested With**: MX10 (CAT printer family)
**Linux**: Ubuntu 20.04+, Debian 10+, Fedora 30+

