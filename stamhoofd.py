#!/usr/bin/python3

import os
import sys
import signal
import json
import re
import socket
import struct
import ctypes
sys.tracebacklimit = None
from datetime import datetime, timedelta, timezone
import logging
import pytz
from PIL import Image, ImageDraw, ImageFont

class _ColorFormatter(logging.Formatter):
    _COLORS = {
        logging.DEBUG:    "\033[36m",   # cyan
        logging.INFO:     "\033[32m",   # green
        logging.WARNING:  "\033[33m",   # yellow
        logging.ERROR:    "\033[31m",   # red
        logging.CRITICAL: "\033[35m",   # magenta
    }
    _RESET = "\033[0m"
    _BOLD  = "\033[1m"

    def format(self, record):
        color = self._COLORS.get(record.levelno, "")
        record.levelname = f"{color}{self._BOLD}{record.levelname}{self._RESET}"
        return super().format(record)


def _build_handler():
    handler = logging.StreamHandler()
    use_color = hasattr(handler.stream, "isatty") and handler.stream.isatty()
    fmt = "%(asctime)s [%(process)d] %(name)s %(levelname)s %(message)s"
    formatter = _ColorFormatter(fmt, datefmt="%Y-%m-%d %H:%M:%S") if use_color \
                else logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    return handler


logging.root.setLevel(logging.INFO)
logging.root.handlers = [_build_handler()]
logger = logging.getLogger("stamhoofd-printer")

import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

ORG_ID  = os.environ.get("STAMHOOFD_ORG_ID")
WEBSHOP_ID = os.environ.get("STAMHOOFD_WEBSHOP_ID")
WEBSHOP_IDS_RAW = os.environ.get("STAMHOOFD_WEBSHOP_IDS", "")
MAX_PARALLEL_POLLS = max(1, int(os.environ.get("STAMHOOFD_MAX_PARALLEL_POLLS", "4")))
API_KEY = os.environ.get("STAMHOOFD_API_KEY")
MX10_BLE_ADDRESS = os.environ.get("MX10_BLE_ADDRESS")
MX10_BLE_ADDR_TYPE = os.environ.get("MX10_BLE_ADDR_TYPE", "public").lower()
EVENT_DURATION_HOURS = float(os.environ.get("STAMHOOFD_EVENT_DURATION_HOURS", "6"))
RATE_SAFETY_MARGIN = float(os.environ.get("STAMHOOFD_RATE_SAFETY_MARGIN", "0.9"))
PRINTED_ORDERS_BASE_DIR = os.environ.get("STAMHOOFD_PRINTED_BASE_DIR", "printed_orders")
STATE_DIR = os.environ.get("STAMHOOFD_STATE_DIR", "")
SLEEP_STATE_FILE = os.environ.get(
    "STAMHOOFD_SLEEP_STATE_FILE",
    os.path.join(STATE_DIR, "sleep_state.json"),
)
TABLE_FIELD_NAME = os.environ.get("STAMHOOFD_TABLE_FIELD", "TAFEL")
MX10_FONT_SIZE = int(os.environ.get("MX10_FONT_SIZE", "24"))
MX10_KEEPALIVE_SECONDS = int(os.environ.get("MX10_KEEPALIVE_SECONDS", "12"))
MX10_FONT_PATH = os.environ.get("MX10_FONT_PATH")

AF_BLUETOOTH = getattr(socket, "AF_BLUETOOTH", 31)
BTPROTO_L2CAP = getattr(socket, "BTPROTO_L2CAP", 0)
BDADDR_LE_PUBLIC = getattr(socket, "BDADDR_LE_PUBLIC", 0x01)
BDADDR_LE_RANDOM = getattr(socket, "BDADDR_LE_RANDOM", 0x02)

ATT_ERROR_RSP = 0x01
ATT_FIND_BY_TYPE_REQ = 0x06
ATT_FIND_BY_TYPE_RSP = 0x07
ATT_READ_BY_TYPE_REQ = 0x08
ATT_READ_BY_TYPE_RSP = 0x09
ATT_WRITE_CMD = 0x52
ATT_WRITE_REQ = 0x12
ATT_WRITE_RSP = 0x13

GATT_PRIMARY_SERVICE_UUID = 0x2800
GATT_CHARACTERISTIC_UUID = 0x2803
MX10_SERVICE_UUID = 0xAE30
MX10_DATA_CHAR_UUID = 0xAE01
MX10_NOTIFY_CHAR_UUID = 0xAE02

CMD_PAPER_FEED = 0xA1
CMD_BITMAP_ROW = 0xA2
CMD_GET_DEVICE_STATE = 0xA3
CMD_SET_QUALITY = 0xA4
CMD_LATTICE = 0xA6
CMD_UPDATE_DEVICE = 0xA9
CMD_SET_ENERGY = 0xAF
CMD_SET_SPEED = 0xBD
CMD_DRAWING_MODE = 0xBE

if API_KEY is None or len(API_KEY) == 0:
    logger.error("Need STAMHOOFD_API_KEY")
    sys.exit(1)
if MX10_BLE_ADDRESS is None or len(MX10_BLE_ADDRESS) == 0:
    logger.error("Need MX10_BLE_ADDRESS")
    sys.exit(1)

if ORG_ID is None or len(ORG_ID) == 0:
    logger.error("Need STAMHOOFD_ORG_ID")
    sys.exit(1)

if WEBSHOP_IDS_RAW.strip():
    WEBSHOP_IDS = [wid.strip() for wid in WEBSHOP_IDS_RAW.split(",") if wid.strip()]
elif WEBSHOP_ID is not None and len(WEBSHOP_ID) > 0:
    WEBSHOP_IDS = [WEBSHOP_ID]
else:
    WEBSHOP_IDS = []

if not WEBSHOP_IDS:
    logger.error("Need STAMHOOFD_WEBSHOP_IDS (comma-separated) or STAMHOOFD_WEBSHOP_ID")
    sys.exit(1)


def unique_preserve_order(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


WEBSHOP_IDS = unique_preserve_order(WEBSHOP_IDS)


def api_url_for(webshop_id):
    return f"https://{ORG_ID}.api.stamhoofd.app/v191/webshop/{webshop_id}/orders"


HEADERS = {"Authorization": f"Bearer {API_KEY}"}


class MultiWindowRateLimiter:
    def __init__(self, limits):
        # limits: list of (window_seconds, max_requests_in_window)
        self.limits = sorted(limits, key=lambda x: x[0])
        self.events = {window: deque() for window, _ in self.limits}

    def _prune(self, now):
        for window, _ in self.limits:
            q = self.events[window]
            cutoff = now - window
            while q and q[0] <= cutoff:
                q.popleft()

    def _required_wait(self, now):
        wait = 0.0
        for window, max_requests in self.limits:
            q = self.events[window]
            if len(q) >= max_requests:
                # Wait until the oldest request in this window expires.
                candidate = (q[0] + window) - now
                if candidate > wait:
                    wait = candidate
        return max(wait, 0.0)

    def wait_for_slot(self):
        while True:
            now = time.monotonic()
            self._prune(now)
            wait = self._required_wait(now)
            if wait <= 0:
                break
            time.sleep(wait)

        stamp = time.monotonic()
        self._prune(stamp)
        for window, _ in self.limits:
            self.events[window].append(stamp)


API_RATE_LIMITER = MultiWindowRateLimiter([
    (5, 25),        # 5 req/s maintained for 5s
    (150, 150),     # 1 req/s maintained for 150s
    (3600, 1000),   # 1000 req/hour
    (86400, 2000),  # 2000 req/day
])


def compute_poll_interval_seconds():
    # Quota rates converted to req/s.
    limits_rps = [
        5.0,
        1.0,
        1000.0 / 3600.0,
        2000.0 / max(1.0, EVENT_DURATION_HOURS * 3600.0),
    ]

    safety = RATE_SAFETY_MARGIN
    if safety <= 0 or safety > 1:
        safety = 0.9

    allowed_rps = min(limits_rps) * safety
    interval = max(1.0, 1.0 / allowed_rps)
    return interval


SLEEP_TIME = float(os.environ.get("STAMHOOFD_POLL_SECONDS", str(compute_poll_interval_seconds())))


def crc8(data):
    cs = 0
    for b in data:
        cs ^= (b & 0xFF)
        for _ in range(8):
            if cs & 0x80:
                cs = ((cs << 1) ^ 0x07) & 0xFF
            else:
                cs = (cs << 1) & 0xFF
    return cs


def cat_packet(cmd, payload, cmd_type=0x00):
    payload = bytes(payload)
    length = len(payload)
    cs = crc8(payload)
    return bytes([
        0x51,
        0x78,
        cmd & 0xFF,
        cmd_type & 0xFF,
        length & 0xFF,
        (length >> 8) & 0xFF,
    ]) + payload + bytes([cs, 0xFF])


class MX10BlePrinter:
    def __init__(self, device, addr_type="public", energy=12000, quality=0x32):
        self.device = device
        self.addr_type = addr_type
        self.energy = energy
        self.quality = quality
        self.sock = None
        self.data_handle = 0
        self.notify_cccd_handle = 0
        self.last_keepalive = 0.0

    def _address_type(self):
        return BDADDR_LE_RANDOM if self.addr_type == "random" else BDADDR_LE_PUBLIC

    def _pack_sockaddr_l2(self, address, cid=4, psm=0, addr_type=None):
        if addr_type is None:
            addr_type = self._address_type()
        octets = bytes(int(x, 16) for x in address.split(":"))
        # BlueZ sockaddr_l2 expects little-endian BDADDR bytes in memory.
        bdaddr = octets[::-1]
        return struct.pack("<HH6sHH", AF_BLUETOOTH, psm, bdaddr, cid, addr_type)

    def _raw_bind_connect(self, sock):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.bind.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        libc.bind.restype = ctypes.c_int
        libc.connect.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint]
        libc.connect.restype = ctypes.c_int
        fd = sock.fileno()

        local = self._pack_sockaddr_l2("00:00:00:00:00:00", cid=4, psm=0, addr_type=BDADDR_LE_PUBLIC)
        remote = self._pack_sockaddr_l2(self.device, cid=4, psm=0, addr_type=self._address_type())
        local_buf = ctypes.create_string_buffer(local)
        remote_buf = ctypes.create_string_buffer(remote)

        # bind() may fail on some stacks; connect() can still succeed.
        rc = libc.bind(fd, ctypes.byref(local_buf), len(local))
        if rc != 0:
            err = ctypes.get_errno()
            if err not in (22, 98):  # EINVAL / EADDRINUSE tolerated here
                raise OSError(err, os.strerror(err))

        rc = libc.connect(fd, ctypes.byref(remote_buf), len(remote))
        if rc != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))

    def _close_socket(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.data_handle = 0
        self.notify_cccd_handle = 0

    def disconnect(self):
        self._close_socket()

    def connect(self):
        self._close_socket()

        sock = socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, BTPROTO_L2CAP)
        # Raw libc connect works reliably only with a blocking socket here.
        sock.settimeout(None)

        try:
            self._raw_bind_connect(sock)
        except OSError as e:
            sock.close()
            raise RuntimeError(f"BLE connect failed: {e}")

        # Keep a sane default timeout for later recv paths.
        sock.settimeout(5.0)

        self.sock = sock

        if not self.discover_data_handle():
            self._close_socket()
            raise RuntimeError("Failed to discover MX10 data handle")

        self.subscribe_notify()
        self.last_keepalive = time.time()

    def att_request(self, req, timeout=2.0):
        if self.sock is None:
            raise RuntimeError("Printer socket is not connected")
        old = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            self.sock.send(req)
            return self.sock.recv(512)
        finally:
            self.sock.settimeout(old)

    def discover_data_handle(self):
        req = struct.pack(
            "<BHHHH",
            ATT_FIND_BY_TYPE_REQ,
            0x0001,
            0xFFFF,
            GATT_PRIMARY_SERVICE_UUID,
            MX10_SERVICE_UUID,
        )
        rsp = self.att_request(req)
        if len(rsp) < 5 or rsp[0] != ATT_FIND_BY_TYPE_RSP:
            return False

        svc_start, svc_end = struct.unpack_from("<HH", rsp, 1)
        start = svc_start

        while start <= svc_end:
            req = struct.pack(
                "<BHHH",
                ATT_READ_BY_TYPE_REQ,
                start,
                svc_end,
                GATT_CHARACTERISTIC_UUID,
            )
            rsp = self.att_request(req)
            if len(rsp) < 2:
                break
            op = rsp[0]
            if op in (ATT_ERROR_RSP,):
                break
            if op != ATT_READ_BY_TYPE_RSP:
                break

            entry_len = rsp[1]
            if entry_len < 7:
                break

            pos = 2
            last_decl = start
            while pos + entry_len <= len(rsp):
                entry = rsp[pos:pos + entry_len]
                decl_handle = struct.unpack_from("<H", entry, 0)[0]
                value_handle = struct.unpack_from("<H", entry, 3)[0]
                uuid16 = struct.unpack_from("<H", entry, 5)[0]

                if uuid16 == MX10_DATA_CHAR_UUID:
                    self.data_handle = value_handle
                if uuid16 == MX10_NOTIFY_CHAR_UUID:
                    self.notify_cccd_handle = value_handle + 1

                last_decl = decl_handle
                pos += entry_len

            start = last_decl + 1

        return self.data_handle != 0

    def subscribe_notify(self):
        if not self.notify_cccd_handle:
            return
        req = struct.pack("<BHH", ATT_WRITE_REQ, self.notify_cccd_handle, 0x0001)
        try:
            rsp = self.att_request(req)
            if not rsp or rsp[0] != ATT_WRITE_RSP:
                logger.warning("CCCD subscribe failed on handle 0x%04X", self.notify_cccd_handle)
        except OSError:
            logger.warning("CCCD subscribe failed due to socket error")

    def _write_cat_bytes(self, data):
        if self.sock is None or not self.data_handle:
            raise RuntimeError("Printer is not connected")

        max_chunk = 20
        off = 0
        while off < len(data):
            chunk = data[off:off + max_chunk]
            att = struct.pack("<BH", ATT_WRITE_CMD, self.data_handle)
            self.sock.send(att + chunk)
            off += len(chunk)
            if off < len(data):
                time.sleep(0.02)
        time.sleep(0.02)

    def send_cat_command_d8(self, cmd, data):
        pkt = cat_packet(cmd, bytes([data & 0xFF]))
        self._write_cat_bytes(pkt)

    def send_cat_command_d16(self, cmd, data):
        lo = data & 0xFF
        hi = (data >> 8) & 0xFF
        pkt = cat_packet(cmd, bytes([lo, hi]))
        self._write_cat_bytes(pkt)

    def start_lattice(self):
        payload = bytes([0xAA, 0x55, 0x17, 0x38, 0x44, 0x5F, 0x5F, 0x5F, 0x44, 0x38, 0x2C])
        self._write_cat_bytes(cat_packet(CMD_LATTICE, payload))
        time.sleep(0.02)

    def end_lattice(self):
        payload = bytes([0xAA, 0x55, 0x17, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x17])
        self._write_cat_bytes(cat_packet(CMD_LATTICE, payload))
        time.sleep(0.02)

    def init_printer(self):
        self.send_cat_command_d8(CMD_GET_DEVICE_STATE, 0x00)
        time.sleep(0.05)

        # prepare_camera raw packet from working Perl flow.
        self._write_cat_bytes(bytes([0x51, 0x78, 0xBC, 0x00, 0x01, 0x02, 0x01, 0x2D, 0xFF]))
        time.sleep(0.02)

        self.send_cat_command_d8(CMD_SET_QUALITY, self.quality)
        time.sleep(0.02)
        self.send_cat_command_d8(CMD_SET_SPEED, 35)
        time.sleep(0.02)
        self.send_cat_command_d16(CMD_SET_ENERGY, self.energy)
        time.sleep(0.02)
        self.send_cat_command_d8(CMD_DRAWING_MODE, 1)
        time.sleep(0.02)
        self.send_cat_command_d8(CMD_UPDATE_DEVICE, 0x00)
        time.sleep(0.02)
        self.start_lattice()

        # Resume command to clear paused state.
        self._write_cat_bytes(bytes([0x51, 0x78, 0xA3, 0x01, 0x01, 0x00, 0x00, 0x00, 0xFF]))
        time.sleep(0.3)

    def _bit_reverse_byte(self, b):
        b = ((b & 0xF0) >> 4) | ((b & 0x0F) << 4)
        b = ((b & 0xCC) >> 2) | ((b & 0x33) << 2)
        b = ((b & 0xAA) >> 1) | ((b & 0x55) << 1)
        return b & 0xFF

    def _render_text_rows(self, text):
        width_px = 384
        margin = 4
        line_spacing = 6

        font = None
        font_candidates = []
        if MX10_FONT_PATH:
            font_candidates.append(MX10_FONT_PATH)
        font_candidates.extend([
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ])
        for fp in font_candidates:
            try:
                font = ImageFont.truetype(fp, MX10_FONT_SIZE)
                break
            except OSError:
                continue
        if font is None:
            font = ImageFont.load_default()

        dummy = Image.new("L", (width_px, 64), color=255)
        draw = ImageDraw.Draw(dummy)
        lines = text.split("\n")

        def line_height_for(s):
            bb = draw.textbbox((0, 0), s if s else " ", font=font)
            return max(1, bb[3] - bb[1])

        heights = [line_height_for(s) for s in lines]
        total_height = sum(heights) + max(0, len(lines) - 1) * line_spacing + margin * 2
        total_height = max(total_height, 64)

        image = Image.new("L", (width_px, total_height), color=255)
        draw = ImageDraw.Draw(image)
        y = margin
        for idx, line in enumerate(lines):
            draw.text((margin, y), line, font=font, fill=0)
            y += heights[idx] + line_spacing

        # Convert to crisp 1-bit for thermal output.
        mono = image.point(lambda p: 0 if p < 160 else 255, mode="1")

        rows = []
        row_bytes_len = width_px // 8
        for y in range(mono.height):
            packed = bytearray()
            for x_byte in range(row_bytes_len):
                val = 0
                for bit in range(8):
                    x = x_byte * 8 + bit
                    px = mono.getpixel((x, y))
                    # In mode "1", 0 is black and 255 is white.
                    if px == 0:
                        val |= (1 << (7 - bit))
                packed.append(self._bit_reverse_byte(val))
            rows.append(bytes(packed))

        return rows

    def send_bitmap_row(self, row):
        pkt = cat_packet(CMD_BITMAP_ROW, row)
        self._write_cat_bytes(pkt)

    def print_text(self, text, feed_steps=40):
        rows = self._render_text_rows(text)
        if not rows:
            raise RuntimeError("No printable bitmap rows generated")

        self.init_printer()
        for row in rows:
            self.send_bitmap_row(row)

        self.end_lattice()
        self.send_cat_command_d8(CMD_SET_SPEED, 8)
        self.send_cat_command_d16(CMD_PAPER_FEED, feed_steps)
        self.send_cat_command_d8(CMD_GET_DEVICE_STATE, 0x00)
        self.last_keepalive = time.time()

    def keep_alive(self):
        if self.sock is None:
            return
        now = time.time()
        if now - self.last_keepalive < MX10_KEEPALIVE_SECONDS:
            return
        # Low-cost keepalive command to keep BLE session active.
        self.send_cat_command_d8(CMD_GET_DEVICE_STATE, 0x00)
        self.last_keepalive = now

    def ensure_connected(self):
        if self.sock is not None:
            return
        self.connect()


def safe_path_component(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def find_record_answer(order, field_name):
    for answer in order.get("data", {}).get("recordAnswers", []):
        if answer.get("settings", {}).get("name") == field_name:
            return answer.get("value")
    return None


PRINTED_ORDERS_ORG_DIR = os.path.join(
    PRINTED_ORDERS_BASE_DIR,
    safe_path_component(ORG_ID),
)


def concise_order_summary(order):
    order_number = order.get("number", "?")
    order_id = order["id"]
    name  = order["data"]["customer"]["email"]
    table = find_record_answer(order, TABLE_FIELD_NAME)
    if table is None:
        raise ValueError(f"NO '{TABLE_FIELD_NAME}'")
    items = order["data"]["cart"]["items"]
    item_parts = []
    for item in items:
        name = item["product"]["name"]
        qty = item["amount"]
        if qty is None or qty < 1:
            qty = 1
        item_parts.append(f"{qty}x {name}")
    items_text = ", ".join(item_parts) if item_parts else "geen items"
    return f"#{order_number} (id={order_id}) tafel={table} items=[{items_text}]"


def printed_orders_dir_for(webshop_id):
    return os.path.join(PRINTED_ORDERS_ORG_DIR, safe_path_component(webshop_id))


def ensure_printed_orders_dir(webshop_id):
    os.makedirs(printed_orders_dir_for(webshop_id), exist_ok=True)


def safe_order_filename(order_id):
    # Keep filenames portable and safe
    return re.sub(r"[^A-Za-z0-9_.-]", "_", order_id) + ".json"


def order_file_path(order_id, webshop_id):
    return os.path.join(printed_orders_dir_for(webshop_id), safe_order_filename(order_id))


def is_order_already_printed(order_id, webshop_id):
    return os.path.exists(order_file_path(order_id, webshop_id) + ".printed")


def save_printed_order(order, webshop_id):
    order_id = order.get("id")
    if not order_id:
        logger.error("Order has no id; cannot persist print state")
        sys.exit(1)

    ensure_printed_orders_dir(webshop_id)
    final_path = order_file_path(order_id, webshop_id)
    tmp_path = final_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(order, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, final_path)
    return os.path.abspath(final_path)

def generate_receipt(order):
    order_number = order.get("number", "?")
    customer = order["data"]["customer"]["email"]
    table = find_record_answer(order, TABLE_FIELD_NAME)
    if table is None:
        raise ValueError(f"NO '{TABLE_FIELD_NAME}'")
    items = order["data"]["cart"]["items"]
    if len(items) < 1:
        raise ValueError("NO ITEMS")

    total_items = 0

    # let's add the order number, table and a timestamp
    tz = pytz.timezone("Europe/Brussels")
    now = datetime.now(tz).strftime("%d/%m/%Y %H:%M")
    order_text = f"-- START --\nTAFEL {table} (#{order_number})\n{customer}\n{now}\n\n"

    # all items in the order
    for item in items:
        name = item["product"]["name"]
        qty = item["amount"]
        if qty is None or qty < 1:
            qty = 1
        total_items += qty
        order_text += f"{qty}x {name}\n"

    order_text += f"\nTOTAL ITEMS: {total_items}\n"

    order_text += "\nThanks!\n"
    return order_text


def print_startup_message(printer):
    tz = pytz.timezone("Europe/Brussels")
    now = datetime.now(tz).strftime("%d/%m/%Y %H:%M")
    boot_text = (
        "-- STARTUP --\n"
        "Ticket printer service started\n"
        f"{now}\n"
    )

    printer.ensure_connected()
    printer.print_text(boot_text, feed_steps=20)


def persist_sleep_state(seconds, reason):
    directory = os.path.dirname(SLEEP_STATE_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)

    payload = {
        "sleep_seconds": float(seconds),
        "reason": reason,
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    tmp_path = SLEEP_STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp_path, SLEEP_STATE_FILE)


def fetch_webshop_response(webshop_id):
    api_url = api_url_for(webshop_id)
    try:
        response = requests.get(api_url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        return {
            "webshop_id": webshop_id,
            "url": api_url,
            "status_code": None,
            "error": str(e),
            "orders": [],
            "retry_after": None,
        }

    orders = []
    if response.status_code == 200:
        try:
            orders = response.json().get("results", [])
        except ValueError:
            return {
                "webshop_id": webshop_id,
                "url": api_url,
                "status_code": None,
                "error": "Invalid JSON response",
                "orders": [],
                "retry_after": None,
            }

    return {
        "webshop_id": webshop_id,
        "url": api_url,
        "status_code": response.status_code,
        "error": None,
        "orders": orders,
        "retry_after": response.headers.get("Retry-After"),
    }


def fetch_all_webshop_responses(webshop_ids):
    if len(webshop_ids) == 1:
        API_RATE_LIMITER.wait_for_slot()
        return [fetch_webshop_response(webshop_ids[0])]

    max_workers = min(MAX_PARALLEL_POLLS, len(webshop_ids))
    responses = []
    futures = {}
    positions = {wid: idx for idx, wid in enumerate(webshop_ids)}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for webshop_id in webshop_ids:
            # Reserve a slot in the global limiter before dispatching each request.
            API_RATE_LIMITER.wait_for_slot()
            future = executor.submit(fetch_webshop_response, webshop_id)
            futures[future] = webshop_id

        for future in as_completed(futures):
            responses.append(future.result())

    responses.sort(key=lambda item: positions.get(item.get("webshop_id"), 0))
    return responses


def handle_webshop_orders(webshop_id, orders, printer):
    neworders = {}
    failorders = {}
    printed_any = False

    for order in orders:
        order_id = order.get("id")
        if not order_id:
            logger.warning("Skipping order without id")
            continue
        if not is_order_already_printed(order_id, webshop_id):
            order_number = order.get("number", "?")
            try:
                neworders[order_id] = order
                saved_path = save_printed_order(order, webshop_id)
                logger.info(
                    f"Saved order #{order_number} (id={order_id}, webshop={webshop_id}) to {saved_path}"
                )
                items = order.get("data", {}).get("cart", {}).get("items", [])
                if not items:
                    logger.info(
                        f"Order has no items; skipping print #{order_number} (id={order_id}, webshop={webshop_id})"
                    )
                    os.replace(saved_path, saved_path + ".printed")
                    continue

                logger.info("Printing new order %s (webshop=%s)", concise_order_summary(order), webshop_id)
                order_text = generate_receipt(order)
                try:
                    printer.ensure_connected()
                    printer.print_text(order_text)
                    printed_any = True
                    logger.info(f"Printed order #{order_number} (id={order_id}, webshop={webshop_id})")
                    os.replace(saved_path, saved_path + ".printed")
                except Exception as e:
                    logger.error(
                        f"Problem printing order #{order_number} (id={order_id}, webshop={webshop_id}), error: {e}"
                    )
                    printer.disconnect()
                    failorders[order_id] = order
            except Exception as e:
                logger.error(
                    f"Problem handling order #{order_number} (id={order_id}, webshop={webshop_id}), error: {e}"
                )

    if len(neworders):
        if len(failorders):
            logger.error(
                f"Fetched/processed {len(neworders)} orders for webshop={webshop_id}, printed {len(neworders) - len(failorders)}"
            )
        else:
            logger.info(f"Fetched/processed {len(neworders)} orders for webshop={webshop_id}")

    return printed_any

def main():
    logger.info("Starting order watcher")
    logger.info("Polling webshops: %s", ", ".join(WEBSHOP_IDS))
    logger.info(
        "Poll interval: %.2fs (event=%.2fh, safety=%.2f)",
        SLEEP_TIME,
        EVENT_DURATION_HOURS,
        RATE_SAFETY_MARGIN,
    )
    logger.info("Max parallel webshop polls: %d", MAX_PARALLEL_POLLS)
    logger.info("Printed-order store root: %s", os.path.abspath(PRINTED_ORDERS_ORG_DIR))
    printer = MX10BlePrinter(MX10_BLE_ADDRESS, addr_type=MX10_BLE_ADDR_TYPE)

    try:
        print_startup_message(printer)
        logger.info("Printed startup message")
    except Exception as e:
        logger.warning(f"Failed to print startup message: {e}")
        printer.disconnect()

    try:
        consecutive_429 = 0
        while True:
            idle_print = True
            sleep_seconds = SLEEP_TIME
            hit_rate_limit = False
            responses = fetch_all_webshop_responses(WEBSHOP_IDS)

            rate_limited_responses = [r for r in responses if r.get("status_code") == 429]
            if rate_limited_responses:
                consecutive_429 += 1
                hit_rate_limit = True

                retry_after_seconds = []
                for response_data in rate_limited_responses:
                    retry_after = response_data.get("retry_after")
                    if retry_after and retry_after.isdigit():
                        retry_after_seconds.append(int(retry_after))

                if retry_after_seconds:
                    sleep_seconds = max(max(retry_after_seconds), SLEEP_TIME)
                else:
                    # If Retry-After is missing, increase backoff aggressively.
                    # This avoids hammering the API when daily quota is exhausted.
                    sleep_seconds = min(max(180 * (2 ** (consecutive_429 - 1)), 180), 21600)

                rate_limited_webshops = ", ".join(r["webshop_id"] for r in rate_limited_responses)
                logger.warning(
                    f"Order poll rate-limited (429, webshops={rate_limited_webshops}, streak={consecutive_429}). Backing off for {sleep_seconds} seconds"
                )
            else:
                consecutive_429 = 0

            for response_data in responses:
                webshop_id = response_data["webshop_id"]
                status_code = response_data.get("status_code")

                if status_code == 200:
                    printed_any = handle_webshop_orders(webshop_id, response_data.get("orders", []), printer)
                    if printed_any:
                        idle_print = False
                elif status_code == 429:
                    continue
                elif status_code is None:
                    logger.warning(
                        f"Order poll failed for webshop={webshop_id}: {response_data.get('error')}"
                    )
                else:
                    logger.warning(
                        f"Order poll failed: status={status_code} url={response_data.get('url')}"
                    )

            # Keep BLE session alive when idle so MX10 stays awake.
            if idle_print:
                try:
                    printer.ensure_connected()
                    printer.keep_alive()
                except Exception as e:
                    logger.warning(f"Keepalive failed, will reconnect later: {e}")
                    printer.disconnect()

            sleep_reason = "poll"
            if hit_rate_limit:
                sleep_reason = "rate_limit_backoff"
            try:
                persist_sleep_state(sleep_seconds, sleep_reason)
            except Exception as e:
                logger.warning(f"Failed to persist sleep state to {SLEEP_STATE_FILE}: {e}")

            logger.debug(f"Sleeping for {sleep_seconds} seconds")
            time.sleep(sleep_seconds) # Poll every X seconds, or back off on 429
    except (KeyboardInterrupt, SystemExit) as e:
        sig = "SIGTERM" if isinstance(e, SystemExit) else "SIGINT"
        logger.info(f"Received {sig} — shutting down gracefully")
        printer.disconnect()
        logger.info("Shutdown complete")
    except OSError:
        logger.error(f"Fatal OS error: {sys.exc_info()[1]}")
        printer.disconnect()
        sys.exit(1)


def _handle_sigterm(signum, frame):
    logger.info("Received SIGTERM")
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _handle_sigterm)
    main()
