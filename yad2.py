import subprocess
from scipy import stats
from selenium.webdriver import DesiredCapabilities
import undetected_chromedriver as uc
import pandas as pd
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    WebDriverException,
)
import time, csv, os, logging, random, json, re
from selenium.webdriver.common.action_chains import ActionChains
from collections import defaultdict
from urllib.parse import urlparse
from db import (
    init_db,
    get_listing,
    init_subscriptions,
    touch_listing,
    upsert_listing,
    update_price_and_touch,
    cleanup_deleted_listings_by_age,
    bulk_upsert_listings

)
from dataclasses import dataclass
from typing import Optional, List
from selenium.webdriver.support.ui import WebDriverWait
import re, string, time as _t
import re
import tempfile, shutil, pathlib, sys
import requests  # pip install requests (if you don't already have it)
import urllib.request
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


_banned_ports = set()

# # ===== Speed / timeout tuning =====
# PAGELOAD_TIMEOUT = 20  # hard cap (seconds) for driver.get before we give up waiting
# COOLDOWN_NORMAL = (10.0, 20.0)     # rotate() sleep for normal cycle
# COOLDOWN_BOTWALL = (20.0, 45.0)
# DELAY_BETWEEN_LISTINGS = (1.5, 3.0)
# DELAY_BETWEEN_PAGES    = (3.0, 6.0)
# SCROLL_MAX_SECONDS     = 5
# HUMAN_IDLE_ROUNDS      = 3
# GET_AND_GUARD_RETRIES = 2  # was 3


# PROFILE = "aggressive"
PAGELOAD_TIMEOUT = 15
COOLDOWN_NORMAL  = (4.0, 8.0)
COOLDOWN_BOTWALL = (10.0, 20.0)
DELAY_BETWEEN_LISTINGS = (0.4, 1.0)
DELAY_BETWEEN_PAGES    = (3.0, 5.0)
SCROLL_MAX_SECONDS     = 2
HUMAN_IDLE_ROUNDS      = 2
GET_AND_GUARD_RETRIES  = 1
# optional: rotate a bit more often to vary fingerprints
ROTATE_MIN, ROTATE_MAX = 8, 12

ROTATE_MIN, ROTATE_MAX = 10, 13
_next_rotate_at = random.randint(ROTATE_MIN, ROTATE_MAX)



# ===== Stable browser fingerprint config =====
_base_versions = [
    "141.0.0.0",
    "141.0.0.60",
    "141.0.1.87",
]

def _pick_stable_ua():
    ver = random.choice(_base_versions)
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{ver} Safari/537.36"
    )

STABLE_UA = _pick_stable_ua()

CHROME_PROFILE_DIR = os.path.abspath("./chrome_profile_yad2")




# near other sleep helpers in yad2.py
# CONFIG at top of file (add near other constants)
SLEEP_CFG = {
    "page_short": (0.2, 0.6),     # after list load, quick settle
    "page_long":  (1.0, 2.0),     # optional slower mode
    "scroll":     (0.4, 0.9),
    "mouse":      (0.3, 1.0),
    "element_backoff": (0.4, 1.0),
    "rotate_normal": (1.5, 3.5),
    "rotate_bot_wall": (8.0, 14.0),
}

def random_sleep(min_s=None, max_s=None):
    if min_s is None:
        min_s, max_s = SLEEP_CFG["page_short"]
    time.sleep(random.uniform(min_s, max_s))

# *********************************************************************


SCRAPE_LOG = logging.getLogger("yad2_scrape")
SCRAPE_LOG.setLevel(logging.INFO)
SCRAPE_LOG.propagate = False  # don't bubble into root

_scrape_handler = logging.StreamHandler()
_scrape_handler.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
# Ensure all default print() goes through timestamped logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(message)s",
    handlers=[logging.StreamHandler()]
)
def print(*args, **kwargs):
    logging.info(" ".join(str(a) for a in args))

SCRAPE_LOG.addHandler(_scrape_handler)

# ********************************************************************
# ====== Exit-IP tracking and rotation helpers ======
_last_exit_ip: str | None = None
_recent_bad_ips: dict = {}

def _record_exit_ip(ip: str, reason: str = "new_session"):
    """
    Append timestamped exit-IP + reason + current LPM port to exit_ip_history.txt
    and cache the value globally.
    """
    global _last_exit_ip
    try:
        _last_exit_ip = ip
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # include port for tracing which proxy produced which IP
        try:
            current_port = _current_port()
        except Exception:
            current_port = "unknown"
        line = f"{ts} port={current_port} {ip} reason={reason}\n"
        with open("exit_ip_history.txt", "a", encoding="utf-8") as f:
            f.write(line)
        PROXY_LOG.info(f"📓 Recorded exit IP: {ip} (port={current_port}, reason={reason})")
    except Exception as e:
        PROXY_LOG.warning(f"Could not record exit IP: {e}")

def _mark_ip_toxic(ip: str):
    """Mark an IP as toxic (bot-wall hit) with current epoch seconds."""
    try:
        now = time.time()
        _recent_bad_ips[ip] = now
        PROXY_LOG.info(f"☠️ Marked IP as toxic: {ip} (t={int(now)})")
    except Exception as e:
        PROXY_LOG.warning(f"Could not mark ip toxic: {e}")

def _is_ip_toxic_recent(ip: str, window_seconds: int = 900) -> bool:
    """Return True if ip was marked toxic within the last window_seconds (default 15 min)."""
    t = _recent_bad_ips.get(ip)
    if not t:
        return False
    return (time.time() - t) < window_seconds

def _probe_exit_ip_for_port(port: str, timeout_sec: float = 8.0) -> str | None:
    """
    Try to ask 'api.ipify.org' through that local LPM port to guess exit IP.
    Allow self-signed TLS (verify=False) because Bright Data sometimes MITMs the CONNECT.
    Return None quietly if it fails.
    """
    proxy_url = f"http://127.0.0.1:{port}"
    try:
        r = requests.get(
            "https://api.ipify.org",
            proxies={"http": proxy_url, "https": proxy_url},
            timeout=timeout_sec,
            verify=False,  # 👈 ignore Bright Data’s fake cert
        )

        if r.status_code == 200:
            txt = r.text.strip()
            # reject HTML/filtered pages; accept only IPv4
            if "<html" in txt.lower() or "safepage" in txt.lower() or not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", txt):
                PROXY_LOG.info(f"🚫 Port {port} returned non-IP ({txt[:60]}…), banning port.")
                _banned_ports.add(port)
                return None
            return txt
        
    except Exception as e:
        PROXY_LOG.debug(f"_probe_exit_ip_for_port({port}) skipped: {e}")
    return None



def _advance_port_raw(reason: str = "cycle"):
    """
    The original low-level 'move to next port in list' behavior.
    """
    global _BRD_PORT_LIST, _current_proxy_idx, _current_session
    if not _BRD_PORT_LIST:
        _init_ports_from_env()

    _current_proxy_idx = (_current_proxy_idx + 1) % len(_BRD_PORT_LIST)
    new_port = _BRD_PORT_LIST[_current_proxy_idx]
    _current_session = _new_session_id()
    PROXY_LOG.info(f"🔁 Switched to local LPM port ({reason}): {new_port} (session={_current_session})")

def _advance_port_smart(reason: str = "cycle", toxic_window_sec: int = 3600):
    """
    Rotate to a new local LPM port, BUT try to avoid ports whose exit IP
    was toxic (bot-wall hit) within the last toxic_window_sec seconds.
    """
    global _BRD_PORT_LIST, _current_proxy_idx

    if not _BRD_PORT_LIST:
        _init_ports_from_env()

    attempts = len(_BRD_PORT_LIST) if _BRD_PORT_LIST else 1
    chosen_port = None
    chosen_ip = None

    for _ in range(attempts):
        # advance to next port in the raw wheel
        _advance_port_raw(reason=reason)

        # ← Add a tiny jitter here so we don't probe ports in a tight loop
        time.sleep(random.uniform(0.05, 0.15))

        port = _current_port()

        # skip immediately banned ports (if any)
        if port in _banned_ports:
            PROXY_LOG.info(f"⛔ Port {port} is banned. Trying another…")
            continue

        # probe to learn exit IP for this port
        ip_guess = _probe_exit_ip_for_port(port)

        # if probing failed, skip it (it may be transient or banned by probe)
        if not ip_guess:
            PROXY_LOG.debug(f"Could not probe port {port} (no ip_guess).")
            continue

        # if this exit IP was recently toxic, skip it
        if _is_ip_toxic_recent(ip_guess, window_seconds=toxic_window_sec):
            PROXY_LOG.info(f"🚫 Skipping port {port} (exit IP {ip_guess} marked toxic recently).")
            continue

        # found a good candidate
        chosen_port = port
        chosen_ip = ip_guess
        break

    # if we didn't find anything acceptable, keep whatever _current_port() points to
    if chosen_port is None:
        chosen_port = _current_port()
        chosen_ip = None

    PROXY_LOG.info(f"🔁 Selected port {chosen_port} (exit IP={chosen_ip}) for reason={reason}")
    return chosen_port, chosen_ip


# ====== End of helpers ======


###################################################################################################################################################################################################
# ============== Bright Data proxy config & helpers ==============
###################################################################################################################################################################################################


PROXY_LOG = logging.getLogger("proxy")
PROXY_LOG.setLevel(logging.INFO)

if not any(isinstance(h, logging.StreamHandler) for h in PROXY_LOG.handlers):
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    PROXY_LOG.addHandler(_h)

PROXY_LOG.propagate = False  # 🔥 stop double-printing


def _brd_cfg():
    host = os.getenv("BRD_HOST", "").strip()
    port = os.getenv("BRD_PORT", "").strip()
    user = os.getenv("BRD_USER", "").strip()
    pwd  = os.getenv("BRD_PASS", "").strip()
    if not (host and port and user and pwd):
        raise RuntimeError("Missing BRD_* env vars. Need BRD_HOST, BRD_PORT, BRD_USER, BRD_PASS.")
    return host, port, user, pwd

def _new_session_id() -> str:
    # short, process-aware session id
    return f"py{os.getpid()}_{int(time.time()) % 100000}"

def _masked(s: str) -> str:
    return s[:4] + "…" if s else s

def _proxy_dict(user: str, pwd: str, host: str, port: str) -> dict:
    auth = f"{user}:{pwd}@{host}:{port}"
    return {
        "http":  f"http://{auth}",
        "https": f"http://{auth}",
    }

def _check_exit_ip(user_with_sess: str, pwd: str, host: str, port: str) -> str:
    try:
        r = requests.get("https://api.ipify.org", timeout=10,
                         proxies=_proxy_dict(user_with_sess, pwd, host, port))
        if r.ok:
            return r.text.strip()
    except Exception as e:
        PROXY_LOG.warning(f"Exit IP check failed: {e}")
    return "unknown"

def _write_brd_auth_extension(user_with_sess: str, pwd: str) -> str:
    """
    Creates a minimal Chrome extension (Manifest v2) that auto-fills proxy auth credentials.
    Returns the extension folder path.
    """
    ext_dir = pathlib.Path(tempfile.mkdtemp(prefix="brd_auth_ext_"))
    manifest = {
        "version": "1.0",
        "manifest_version": 2,
        "name": "BRD Proxy Auth (MV2)",
        "permissions": [
            "webRequest",
            "webRequestBlocking",
            "<all_urls>"
        ],
        "background": {
            "scripts": ["background.js"]
        }
    }
    (ext_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    bg = f"""
    chrome.webRequest.onAuthRequired.addListener(
      function handler(details) {{
        return {{authCredentials: {{username: "{user_with_sess}", password: "{pwd}"}}}};
      }},
      {{urls: ["<all_urls>"]}},
      ['blocking']
    );
    """
    (ext_dir / "background.js").write_text(bg, encoding="utf-8")
    return str(ext_dir)

_current_ext_dir = None
_current_session = None
_current_proxy = None  # (host,port,user_without_session)



# =========================
# Canonical URL handling
# =========================
ITEM_ID_RE = re.compile(r"/(?:vehicles/)?item/([A-Za-z0-9]+)")

def canonical_url(href: str) -> str:
    if not href:
        return href
    m = ITEM_ID_RE.search(href)
    if not m:
        return href.split("?")[0]
    item_id = m.group(1)
    return f"https://www.yad2.co.il/vehicles/item/{item_id}"

# ===== Brand/model selection (kept compatible) =====
# at top of yad2.py (near other imports)
INTERACTIVE = sys.stdin.isatty()

# Replace your existing select_brands() with this:
def select_brands():
    if INTERACTIVE:
        print("\nAvailable brands:")
        for i, brand in enumerate(brands_data):
            print(f"{i+1}. {brand['brand']}")
        print("\nEnter brand numbers (comma separated): ", end="", flush=True)

    # tele_bot writes the exact same line(s) into stdin; read whatever is provided
    brand_indexes = input().strip()
    brand_indexes = [int(idx.strip())-1 for idx in brand_indexes.split(",") if idx.strip().isdigit()]
    return [brands_data[i] for i in brand_indexes]

# Replace your existing select_models(brand) similarly:
def select_models(brand):
    if INTERACTIVE:
        print(f"\nModels for {brand['brand']}:")
        for i, model in enumerate(brand['models']):
            label = model.get("model_name") or str(model.get("model_value"))
            print(f"{i+1}. {label}")
        print(f"\nEnter model numbers for {brand['brand']} (comma separated): ", end="", flush=True)

    model_indexes = input().strip()
    model_indexes = [int(idx.strip())-1 for idx in model_indexes.split(",") if idx.strip().isdigit()]
    return [brand['models'][i] for i in model_indexes]


def build_combined_url(selected_brands_models):
    manufacturer_vals = []
    model_vals = []
    for brand, models in selected_brands_models:
        manufacturer_vals.append(brand['value'])
        model_vals.extend([m['model_value'] for m in models])
    return f"{BASE_URL}?manufacturer={','.join(manufacturer_vals)}&model={','.join(model_vals)}"

def build_single_url(brand: dict, model: dict | None):
    """
    Build a URL for exactly one brand and (optionally) one model.
    """
    base = f"{BASE_URL}?manufacturer={brand['value']}"
    if model:
        return f"{base}&model={model['model_value']}"
    return base
from urllib.parse import urlparse  # (you already import this; keep once)

AUCTION_PATTERNS = ("konesy", "/live/")

def is_auction_or_external(url: str) -> bool:
    u = urlparse(url)
    host = (u.netloc or "").lower()
    path = (u.path or "").lower()
    # Anything not on yad2, or “live”/“konesy” patterns → skip
    if host and not host.endswith("yad2.co.il"):
        return True
    if any(p in host or p in path for p in AUCTION_PATTERNS):
        return True
    return False



####################################################################################################################################################################################################
# ===== Setup undetected Chrome browser (VPN-aware) =====
#####################################################################################################################################################################################################
_BRD_PORT_LIST = []
_current_proxy_idx = -1

def _init_ports_from_env():
    global _BRD_PORT_LIST, _current_proxy_idx
    ports_env = os.getenv("BRD_PORTS", "")
    _BRD_PORT_LIST = [p.strip() for p in ports_env.split(",") if p.strip()]
    if not _BRD_PORT_LIST:
        raise RuntimeError("BRD_PORTS env is empty. You must provide local LPM ports like 24000,24001,...")
    random.shuffle(_BRD_PORT_LIST)
    _current_proxy_idx = 0

def _current_port():
    # return current port in the shuffled list
    global _BRD_PORT_LIST, _current_proxy_idx
    if not _BRD_PORT_LIST:
        _init_ports_from_env()
    return _BRD_PORT_LIST[_current_proxy_idx]

def _advance_port(reason="cycle"):
    global _BRD_PORT_LIST, _current_proxy_idx, _current_ext_dir, _current_session
    if not _BRD_PORT_LIST:
        _init_ports_from_env()
    _current_proxy_idx = (_current_proxy_idx + 1) % len(_BRD_PORT_LIST)
    new_port = _BRD_PORT_LIST[_current_proxy_idx]
    _current_session = _new_session_id()
    PROXY_LOG.info(f"🔁 Switched to local LPM port ({reason}): {new_port} (session={_current_session})")



def _rand_window() -> str:
    """return realistic desktop viewport like '1366,768' or '1440,824'"""
    w = random.randint(1280, 1536)
    h = random.randint(720, 900)
    return f"{w},{h}"

def _rand_lang() -> str:
    """randomize Accept-Language header order/weights per session"""
    candidates = [
        "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
        "he-IL,he;q=0.9,en-GB;q=0.8,en;q=0.7",
        "en-US,en;q=0.9,he-IL;q=0.6",
    ]
    return random.choice(candidates)


def create_driver(retry_attempts: int = 3) -> uc.Chrome | None:
    global driver
    
    # Get the current port from your rotation logic
    port = _current_port()
    proxy_arg = f"http://127.0.0.1:{port}"
    
    # 1. Define the profile directory path
    # Using an absolute path ensures no confusion between relative Desktop paths
    profile_dir = os.path.join(os.path.expanduser("~"), "chrome_profiles", f"port_{port}")
    os.makedirs(profile_dir, exist_ok=True)
    
    SCRAPE_LOG.info(f"🚀 Initializing Chrome on Port {port}")
    SCRAPE_LOG.info(f"📂 Profile Path: {profile_dir}")

    for attempt in range(retry_attempts):
        try:
            options = uc.ChromeOptions()
            
            # 2. CRITICAL: Proxy and User Data must be inside the loop
            options.add_argument(f'--proxy-server={proxy_arg}')
            options.add_argument(f"--user-data-dir={profile_dir}")
            
            # 3. SSL Bypasses for Bright Data's self-signed cert
            options.add_argument('--ignore-certificate-errors')
            options.add_argument('--ignore-ssl-errors')
            options.add_argument('--allow-insecure-localhost')
            
            # 4. Ubuntu-specific stability flags for your ThinkPad
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            options.add_argument(f'--user-agent={STABLE_UA}')
            options.add_argument('--lang=he-IL') # Important for Yad2 Hebrew context

            # 5. Performance logging to catch blocks early
            caps = DesiredCapabilities.CHROME.copy()
            caps['goog:loggingPrefs'] = {'performance': 'ALL'}

            # Use the version_main you confirmed earlier
            driver_local = uc.Chrome(
                options=options,
                desired_capabilities=caps,
                version_main=144 
            )
            
            # Set timeout so a hanging proxy doesn't freeze the whole script
            driver_local.set_page_load_timeout(30)
            
            return driver_local

        except Exception as e:
            SCRAPE_LOG.error(f"⚠️ Attempt {attempt + 1} failed: {e}")
            if attempt < retry_attempts - 1:
                time.sleep(2)
            else:
                return None


# def create_driver(retry_attempts: int = 2, retry_backoff: float = 1.0):
#     """
#     Faster startup browser:
#     - proxy per-port profile (keeps cookies local to that IP)
#     - block images/video = less data
#     - pageLoadStrategy 'eager' so we don't wait forever
#     - set_page_load_timeout(PAGELOAD_TIMEOUT)
#     """

#     global _current_proxy_idx, _last_exit_ip

#     if _current_proxy_idx == -1:
#         _init_ports_from_env()

#     port = _current_port()
#     proxy_arg = f"http://127.0.0.1:{port}"

#     # === Chrome options ===
#     options = uc.ChromeOptions()
#     options.add_argument("--no-sandbox")
#     options.add_argument("--disable-dev-shm-usage")
#     options.add_argument("--disable-blink-features=AutomationControlled")
#     options.add_argument("--disable-infobars")
#     options.add_argument("--ignore-certificate-errors")
    
#     options.add_argument(f"--proxy-server={proxy_arg}")

#     options.add_argument(f"--window-size={_rand_window()}")
#     # options.add_experimental_option("excludeSwitches", ["enable-automation"])
#     # options.add_experimental_option('useAutomationExtension', False)

#     # per-port persistent profile dir
#     profile_base = CHROME_PROFILE_DIR
#     profile_dir = os.path.abspath(os.path.join(profile_base, f"port_{port}"))
#     os.makedirs(profile_dir, exist_ok=True)
#     options.add_argument(f"--user-data-dir={profile_dir}")

#     # stable UA
#     options.add_argument(f"--user-agent={STABLE_UA}")

#     # speed hint: don't wait for every last subresource
#     caps = options.to_capabilities()
#     caps["pageLoadStrategy"] = "eager"

#     PROXY_LOG.info("🚀 Launching Chrome (fast mode)")
#     PROXY_LOG.info(f"🌍 Using local proxy: {proxy_arg}")
#     PROXY_LOG.info(f"🕸 UA: {STABLE_UA}")
#     PROXY_LOG.info(f"🔐 Upstream auth handled by LPM (no prompt)")
#     PROXY_LOG.info(f"💾 Profile dir: {profile_dir}")

#     driver_local = None
#     last_err = None
#     last_err = None
#     for attempt in range(retry_attempts):
#         driver_local = None
#         try:
#             # 1. ALWAYS initialize options inside the loop to avoid "reuse" error
#             options = uc.ChromeOptions()
#             options.add_argument(f'--proxy-server={proxy_arg}')
#             # options.add_argument(f'--user-agent={_rand_ua()}')
#             options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36')
#             options.add_argument('--no-sandbox')
#             options.add_argument('--disable-dev-shm-usage')
#             # Add headless if you are running on a server without a monitor
#             # options.add_argument('--headless') 

#             # 2. Reset capabilities every attempt
#             caps = DesiredCapabilities.CHROME.copy()
#             caps['goog:loggingPrefs'] = {'performance': 'ALL'}

#             # 3. Start the driver
#             # The 'version_main' helps UC find the right driver if it's confused
#             driver_local = uc.Chrome(
#                 options=options, 
#                 desired_capabilities=caps,
#                 version_main=144  # Matches your current browser version
#             )

#             # Hard cap page load wait
#             driver_local.set_page_load_timeout(PAGELOAD_TIMEOUT)

#             # # --- CDP Resource Blocking ---
#             # try:
#             #     driver_local.execute_cdp_cmd("Network.enable", {})
#             #     driver_local.execute_cdp_cmd("Network.setBlockedURLs", {
#             #         "urls": ["*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.svg", "*.woff", "*.woff2"]
#             #     })
#             # except Exception as e:
#             #     PROXY_LOG.debug(f"CDP block skipped: {e}")

#             # --- Updated Stealth CDP Resource Blocking ---
#             try:
#                 driver_local.execute_cdp_cmd("Network.enable", {})
#                 driver_local.execute_cdp_cmd("Network.setBlockedURLs", {
#                     "urls": [
#                         "*.mp4", "*.avi", "*.mov", "*.webm", # Block heavy video
#                         "*.woff", "*.woff2", "*.ttf",        # Block fonts
#                         "*google-analytics.com*", "*doubleclick.net*" # Block trackers
#                     ]
#                 })
#             except Exception as e:
#                 PROXY_LOG.debug(f"CDP block skipped: {e}")


#             try:
#                 driver_local.execute_cdp_cmd(
#                     "Network.setExtraHTTPHeaders",
#                     {"headers": {"Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7"}}
#                 )
#             except Exception as e:
#                 PROXY_LOG.debug(f"Headers patch skipped: {e}")
#             # --- IP Verification ---
#             try:
#                 r = requests.get("http://api.ipify.org", 
#                                 proxies={"http": proxy_arg, "https": proxy_arg}, 
#                                 timeout=10)
#                 if r.status_code == 200:
#                     PROXY_LOG.info(f"🌍 Exit IP (port {port}): {r.text.strip()}")
#             except:
#                 PROXY_LOG.warning("⚠️ Could not fetch exit IP")

#             return driver_local

#         except Exception as e:
#             last_err = e
#             PROXY_LOG.warning(f"Driver launch failed (attempt {attempt+1}/{retry_attempts}): {e}")
            
#             # Clean up the failed attempt immediately
#             if driver_local:
#                 try:
#                     driver_local.quit()
#                 except:
#                     pass
            
#             time.sleep(retry_backoff * (attempt + 1))

#     # If we get here, all attempts failed
#     raise RuntimeError(f"Failed to create Chrome driver after {retry_attempts} tries: {last_err}")



    # for attempt in range(retry_attempts):
    #     try:
    #         # start driver
    #         driver_local = uc.Chrome(options=options, desired_capabilities=caps)

    #         # hard cap page load wait
    #         driver_local.set_page_load_timeout(PAGELOAD_TIMEOUT)

    #         # block heavy assets (images/video/etc) using CDP for speed
    #         try:
    #             driver_local.execute_cdp_cmd("Network.enable", {})
    #             driver_local.execute_cdp_cmd(
    #                 "Network.setExtraHTTPHeaders",
    #                 {"headers": {"Accept-Language": _rand_lang()}}
    #             )
    #             driver_local.execute_cdp_cmd(
    #                 "Network.setBlockedURLs",
    #                 {
    #                     "urls": [
    #                         "*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp",
    #                         "*.svg", "*.mp4", "*.avi", "*.mov", "*.webm",
    #                         "*.woff", "*.woff2", "*.ttf",
    #                     ]
    #                 },
    #             )
    #         except Exception as e:
    #             PROXY_LOG.debug(f"CDP block resources skipped: {e}")

    #         wait_local = WebDriverWait(driver_local, 10)
    #         PROXY_LOG.info("✅ Chrome driver started successfully.")

    #         # figure out the exit IP for THIS port
    #         try:
    #             r = requests.get(
    #                 "http://api.ipify.org",
    #                 proxies={"http": proxy_arg, "https": proxy_arg},
    #                 timeout=10,
    #             )
    #             if r.status_code == 200:
    #                 exit_ip = r.text.strip()
    #                 _last_exit_ip = exit_ip
    #                 PROXY_LOG.info(f"🌍 Current exit IP (port {port}): {exit_ip}")
    #                 _record_exit_ip(exit_ip, reason="new_session")
    #             else:
    #                 PROXY_LOG.warning(f"⚠️ exit IP status {r.status_code}")
    #         except Exception:
    #             _last_exit_ip = None
    #             PROXY_LOG.warning("⚠️ Could not fetch exit IP (will continue without it)")

    #         # stealth patch navigator.webdriver
    #         try:
    #             driver_local.execute_script(
    #                 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
    #             )
    #         except Exception as e:
    #             PROXY_LOG.debug(f"navigator.webdriver patch skipped: {e}")

    #         return driver_local

    #     except Exception as e:
    #         last_err = e
    #         PROXY_LOG.warning(
    #             f"Driver launch failed (attempt {attempt+1}/{retry_attempts}): {e}"
    #         )
    #         time.sleep(retry_backoff * (attempt + 1))

    # raise RuntimeError(f"Failed to create Chrome driver: {last_err}")


    

###################################################################################################################################################################################################
#                                         ====== CONSOLE-UI ======
###################################################################################################################################################################################################

class ConsoleUI:
    def __init__(self, logger):
        self.logger = logger
        self._skip_run: Optional[tuple[int,int,int]] = None  # (page, start_idx, end_idx)
        self._page_printed = set()

    def page_header(self, page: int) -> None:
        if page not in self._page_printed:
            self.logger.info("")
            self.logger.info(f"─────── Page {page} ───────")
            self._page_printed.add(page)

    def _flush_skip_run(self) -> None:
        if not self._skip_run:
            return
        page, start_i, end_i = self._skip_run
        if start_i == end_i:
            self.logger.info(f"➡️  [Page {page}] Skipped listing {start_i}")
        else:
            self.logger.info(f"➡️  [Page {page}] Skipped listings: {start_i}–{end_i}")
        self._skip_run = None

    def mark_skipped(self, page: int, idx: int) -> None:
        if not self._skip_run:
            self._skip_run = (page, idx, idx)
            return
        p, s, e = self._skip_run
        if p == page and idx == e + 1:
            self._skip_run = (p, s, idx)
        else:
            self._flush_skip_run()
            self._skip_run = (page, idx, idx)

    def mark_scrape_start(self, page: int, idx: int, total: int, brand: str, model: str) -> None:
        self._flush_skip_run()
        self.logger.info("")  # spacing the user asked for
        self.logger.info(f"➡️  [Page {page}] Listing {idx}/{total}")
        self.logger.info(f"🚗 Scraping: {brand} {model}")

    def detail_found(self, title: str, price: str, url: str) -> None:
        self.logger.info(f"🔎 Found: {title or '(no title)'} | {price or '(no price)'} | {url}")

    def saved(self, title: str, price: str) -> None:
        self.logger.info(f"✅ Saved: {title or '(no title)'} | {price or '(no price)'}")
    
    def batch_saved(self, n: int) -> None:
        self.logger.info("")
        self.logger.info(f"✅ Saved batch ({n} listings)")


    def updated(self, title: str, old_price: str, new_price: str) -> None:
        self.logger.info(f"🔄 Price updated: {title} | {old_price} → {new_price}")

    def removed(self, count: int) -> None:
        if count > 0:
            self.logger.info(f"🗑️  Removed {count} deleted/expired listings")

    def end_page(self) -> None:
        self._flush_skip_run()

@dataclass
class CrawlStats:
    new_saved: int = 0
    price_updates: int = 0
    removed: int = 0
    pages: int = 0
    total_seen: int = 0

    def summarize(self, logger, context: str):
        logger.info("")
        logger.info(f"📊 Crawl complete: {context}")
        logger.info(f"   ✔️  New: {self.new_saved}")
        logger.info(f"   🔄 Updated prices: {self.price_updates}")
        logger.info(f"   🗑️  Removed: {self.removed}")
        logger.info(f"   📄 Pages: {self.pages} | 👀 Seen listings: {self.total_seen}")
        logger.info("")



# ===== Human-like actions =====
def human_like_scroll(container_selector=None,
                      max_seconds=SCROLL_MAX_SECONDS,
                      max_idle_rounds=HUMAN_IDLE_ROUNDS):
    """
    Scroll page (or container) just enough to look human and let lazy content load.
    Faster version: ~2s instead of ~6s.
    """
    if container_selector:
        scroll_el = driver.execute_script("return document.querySelector(arguments[0]);", container_selector)
    else:
        scroll_el = None

    get_h_js = (
        "return arguments[0].scrollHeight"
        if scroll_el
        else "return document.body.scrollHeight"
    )
    do_scroll_js = (
        "arguments[0].scrollTop = arguments[0].scrollHeight"
        if scroll_el
        else "window.scrollTo(0, document.body.scrollHeight)"
    )

    last_h = 0
    idle = 0
    t0 = time.time()

    while (time.time() - t0) < max_seconds and idle < max_idle_rounds:
        if scroll_el:
            driver.execute_script(do_scroll_js, scroll_el)
        else:
            driver.execute_script(do_scroll_js)

        time.sleep(random.uniform(*SLEEP_CFG["scroll"]))

        new_h = (
            driver.execute_script(get_h_js, scroll_el)
            if scroll_el
            else driver.execute_script(get_h_js)
        )
        if new_h <= last_h:
            idle += 1
        else:
            idle = 0
            last_h = new_h

    # tiny settle
    time.sleep(0.3)


def human_like_mouse_move(element, moves: int = 3):
    """
    Small random mouse moves over an element to look less robotic.
    Non-fatal: any exception is swallowed.
    """
    try:
        actions = ActionChains(driver)
        for _ in range(moves):
            # random tiny offsets so it's not identical every time
            offset_x = random.randint(-5, 5)
            offset_y = random.randint(-3, 6)
            actions.move_to_element_with_offset(element, offset_x, offset_y).perform()
            time.sleep(random.uniform(*SLEEP_CFG["mouse"]))
    except Exception:
        # don't crash scraping if this fails (stale element, etc.)
        pass

# ===== Helpers =====
def safe_find_elements(by, selector, retries=3, timeout=10, settle=0.8):
    last = -1; start = time.time(); stable_since = time.time()
    while time.time() - start < timeout:
        els = driver.find_elements(by, selector)
        if len(els) == last:
            if time.time() - stable_since >= settle:
                return els
        else:
            last = len(els); stable_since = time.time()
        time.sleep(0.2)
    return driver.find_elements(by, selector)

def safe_find_element(by, selector, retries=3):
    for attempt in range(retries):
        try:
            element = wait.until(EC.presence_of_element_located((by, selector)))
            return element
        except TimeoutException:
            logger.warning(f"Timeout finding element {selector}, attempt {attempt+1}/{retries}")
            time.sleep(random.uniform(1, 3))
    logger.error(f"Failed to find element {selector} after {retries} attempts")
    return None

def _normalize_price(p: str) -> str:
    return "".join(ch for ch in (p or "") if ch.isdigit())

def rotate(reason: str = "scheduled"):
    """
    Quit driver, smart-pick next local LPM port (avoid toxic IPs),
    short cooldown, recreate driver.
    """
    global driver, wait

    try:
        driver.quit()
        PROXY_LOG.info("✅ Closed current Chrome session.")
    except Exception as e:
        logging.getLogger("proxy").warning(f"Driver quit error (ignored): {e}")

    # hop to next port, prefer non-toxic exit IP
    _advance_port_smart(reason=reason, toxic_window_sec=3600)

    if reason == "bot_wall":
        sleep_for = random.uniform(*COOLDOWN_BOTWALL)
    else:
        sleep_for = random.uniform(*COOLDOWN_NORMAL)

    PROXY_LOG.info(f"⏸️ Cooldown {sleep_for:.1f}s (reason={reason}) before new session…")
    time.sleep(sleep_for)

    PROXY_LOG.info("🚗 Creating fresh Chrome session via chosen local LPM port…")
    driver = create_driver()
    wait = WebDriverWait(driver, 10)
    PROXY_LOG.info("✅ New driver ready.")




def _looks_soft_blocked(html: str) -> bool:
    """
    Heuristic for 'I'm not *officially* blocked, but the page is fake/useless'.
    We'll treat this like a bot wall.
    """
    if not html or len(html) < 2000:
        # too tiny / almost blank page for a car listings page
        return True

    # Yad2 normally returns a ton of React chunks, car cards, etc.
    # If it's missing all of that but still HTTP 200, it's often a throttle / shadow block.
    bad_markers = [
        "request unsuccessful",   # generic anti-bot pages
        "temporarily blocked",    # rate limit style message
        "access denied",          # generic WAF text
        "verification required",  # JS challenge pages
    ]
    lower = html.lower()
    for needle in bad_markers:
        if needle in lower:
            return True

    return False


def _get_and_guard(url: str, retries: int = GET_AND_GUARD_RETRIES) -> bool:
    """
    Load URL in the browser fast and decide if we're blocked.

    - Hard/soft block => rotate(reason="bot_wall") and retry (short cooldown now).
    - Success => quick human-like scroll (~2s max) and return True.
    """
    BLOCK_SUBSTRS = [
        "are you for real",
        "shieldsquare captcha",
        "אבטחת אתר",
        "y2_captcha_error_page",
        "hcaptcha",
        "validate.perfdrive.com",
        "carta.radware.com/bouncer",
    ]

    _punct_tbl = str.maketrans({ch: " " for ch in string.punctuation})

    def _norm(txt: str) -> str:
        txt = (txt or "").lower().translate(_punct_tbl)
        txt = re.sub(r"\s+", " ", txt).strip()
        return txt

    def _hard_blocked(src: str) -> bool:
        norm = _norm(src)
        if not norm:
            return False
        for needle in BLOCK_SUBSTRS:
            if needle in norm:
                return True
        return False

    for attempt in range(retries):
        PROXY_LOG.info(f"🌐 GET {url} (attempt {attempt+1}/{retries})")

        try:
            driver.get(url)
        except TimeoutException:
            # page load exceeded PAGELOAD_TIMEOUT, but we still might have partial DOM
            PROXY_LOG.warning(f"⏱️ Page load timeout after {PAGELOAD_TIMEOUT}s, continuing with partial DOM")

        # allow WAF/JS challenge to render but don't sit forever
        t0 = _t.time()
        html = ""
        while _t.time() - t0 < 3.0:
            html = driver.page_source or ""
            if _hard_blocked(html) or _looks_soft_blocked(html):
                logging.warning("🤖 Bot wall.")
                if _last_exit_ip:
                    _mark_ip_toxic(_last_exit_ip)
                rotate(reason="bot_wall")

                # 🔴 CHANGE: instead of "break" (which causes retry loop),
                # just return False immediately.
                return False

            _t.sleep(0.25)

        else:
            # if we didn't break -> looks OK
            PROXY_LOG.info("✅ Page passed bot-wall check.")
            try:
                human_like_scroll(
                    max_seconds=SCROLL_MAX_SECONDS,
                    max_idle_rounds=HUMAN_IDLE_ROUNDS,
                )
            except Exception as e:
                PROXY_LOG.debug(f"human_like_scroll skipped: {e}")
            return True

        # if we got here, we rotated and will retry with new driver/session
        continue

    logging.error("❌ Still blocked after multiple rotations; skipping URL.")
    return False




################################
# ===== Scraping functions =====
################################
def get_all_listing_links(page_url, selected_brands_models):
    """
    Return list of dicts: {'url', 'brand', 'model', 'visit': bool}
    Existing URLs: update price from feed (if changed) and return with visit=False.
    New URLs: return with visit=True for detail scrape.
    """
    SCRAPE_LOG.info(f"🌐 Loading page: {page_url}")
    try:
        if not _get_and_guard(page_url):   # ⬅️ NEW
            return []
        try:
            human_like_scroll()
        except Exception as e:
            logger.warning(f"human_like_scroll failed ({e}); using simple fallback")
            for _ in range(6):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(0.5)
        # random_sleep()
        random_sleep(*SLEEP_CFG["page_short"])
        random_sleep(2.0, 4.0)  # chill before next page

    except WebDriverException as e:
        logger.error(f"Error loading page {page_url}: {e}")
        return []

    cards = safe_find_elements(By.CSS_SELECTOR, "a[class*='agency'], a[class*='private']")
    logger.info(f"Found {len(cards)} listing anchors on page")


    # Prepare brand lists for inline parsing
    selected_brand_names = [b['brand'] for (b, _ms) in selected_brands_models]
    selected_brand_names.sort(key=len, reverse=True)
    all_brand_names = [b['brand'] for b in brands_data]
    all_brand_names.sort(key=len, reverse=True)

    result = []
    seen_urls = set()

    for a in cards:
        raw_href = a.get_attribute('href')
        if not raw_href:
            continue
        href = canonical_url(raw_href)
        if not href:
            continue
        if href in seen_urls:
            continue
        if is_auction_or_external(href):
            logger.info(f"⏭️  Skipping non-yad2/auction link: {href}")
            continue
        seen_urls.add(href)
         # Detect seller type from card classes (fallback later to "private")
        cls = (a.get_attribute("class") or "").lower()
        seller_type = "agency" if "agency" in cls else "private"

        # Parse brand/model from card heading text
        raw_title = ""
        try:
            title_el = a.find_element(By.CSS_SELECTOR, '[class*="feed-item-info-section_heading__"]')
            raw_title = (title_el.text or title_el.get_attribute("innerText") or "").strip()
        except Exception:
            try:
                title_el = a.find_element(By.XPATH, './/*[contains(@class,"feed-item-info-section_heading__")]')
                raw_title = (title_el.text or title_el.get_attribute("innerText") or "").strip()
            except Exception:
                raw_title = ""

        brand_name, model_name = "Unknown", ""
        txt = raw_title.strip()
        for bname in selected_brand_names:
            if txt.startswith(bname + " ") or txt == bname:
                brand_name = bname
                model_name = txt[len(bname):].strip()
                break
        if brand_name == "Unknown":
            for bname in all_brand_names:
                if txt.startswith(bname + " ") or txt == bname:
                    brand_name = bname
                    model_name = txt[len(bname):].strip()
                    break
        row = get_listing(href)

        if row:
            # Try to read price from the feed card and update only if changed
            try:
                price_el = a.find_element(By.CSS_SELECTOR, "span[data-testid='price']")
                new_price = (price_el.text or price_el.get_attribute("innerText") or "").strip()
                if new_price:
                    update_price_and_touch(href, new_price, brand_name, model_name)
                else:
                    touch_listing(href, brand=brand_name, model=model_name)
            except Exception:
                touch_listing(href, brand=brand_name, model=model_name)

            # result.append({'url': href, 'brand': brand_name, 'model': model_name, 'visit': False})
            result.append({'url': href, 'brand': brand_name, 'model': model_name, 'visit': False, 'seller_type': seller_type})
            continue


        # New URL → visit later
        # result.append({'url': href, 'brand': brand_name, 'model': model_name, 'visit': True})
        result.append({'url': href, 'brand': brand_name, 'model': model_name, 'visit': True, 'seller_type': seller_type})

    logger.info(f"🔗 To visit after filtering: {len(result)}")
    return result

def scrape_car_detail(url, brand_name, model_name, stats: CrawlStats, ui: ConsoleUI, pre_seller_type: str | None = None, collector: list | None = None):
    if is_auction_or_external(url):
       SCRAPE_LOG.info(f"⏭️  Skipping non-yad2/auction detail: {url}")
       return
    url = canonical_url(url)
    logger.info(f" ⚡ Scraping detail page: {url}")
    try:
        # guarded load (detect bot wall / rotate if needed)
        if not _get_and_guard(url):        # already scrolls lightly
            return

        # tiny settle only
        random_sleep(*SLEEP_CFG["page_short"])

        # 💀 quick broken-page detection
        try:
            page_title = (driver.title or "").strip()
            html = driver.page_source or ""
        except Exception:
            page_title = ""
            html = ""

        broken_signatures = [
            "this page isn’t working",
            "this page isn't working",
            "שגיאה בשרת",
            "502",
            "503",
            "404",
            "bad gateway",
            "request blocked",
            "access denied",
            "server error",
        ]

        joined = (page_title + " " + html[:2000]).lower()
        if any(sig in joined for sig in broken_signatures):
            logger.warning(f"💀 Broken or empty page for {url} — skipping.")
            ui.detail_found(page_title or "Broken page", "(no price)", url)
            return


    except WebDriverException as e:
        logger.error(f"Error loading detail page {url}: {e}")
        return


    # Title
    title_el = safe_find_element(By.CSS_SELECTOR, 'h1')
    title = title_el.text.strip() if title_el else ""
    if not title:
        logger.warning(f"No title found for {url}")

    # Price
    price_el = safe_find_element(By.CSS_SELECTOR, 'span[data-testid="price"]')
    if price_el:
        price = (price_el.text or price_el.get_attribute('innerText') or "").strip()
    else:
        price = ""
        logger.warning(f"No price found for {url}")

    ui.detail_found(title, price, url)


    # Vehicle details
    vehicle_details = driver.find_elements(
        By.CSS_SELECTOR,
        'div.vehicle-details_VehicleDetailsBox__UTKTs div.details-item_detailsItemBox__blPEY'
    )
    car_year = car_hands = car_km = ""
    for item in vehicle_details:
        spans = item.find_elements(By.TAG_NAME, "span")
        texts = [s.text.strip() for s in spans if s.text.strip()]
        if not texts:
            continue
        if len(texts) == 1:
            car_year = texts[0]
        elif "יד" in texts:
            car_hands = " ".join(texts)
        elif any("ק״מ" in t or "ק\"מ" in t for t in texts):
            car_km = " ".join(texts)

    if title_el: human_like_mouse_move(title_el)
    if price_el: human_like_mouse_move(price_el)
    loc_el = safe_find_element(By.CSS_SELECTOR, 'span[data-testid="location"]')
    location = (loc_el.text or loc_el.get_attribute("innerText") or "").strip() if loc_el else ""

    # Ad created date: e.g. "פורסם ב 13/09/25"
    created_el = safe_find_element(By.CSS_SELECTOR, "span.report-ad_createdAt__MhAb0")
    ad_created_at = ""
    if created_el:
        # raw_created = (created_el.text or created_el.get_attribute("innerText") or "").strip()
        # # Extract dd/mm/yy
        # m = re.search(r"\b(\d{2}/\d{2}/\d{2})\b", raw_created)
        # ad_created_at = m.group(1) if m else raw_created
        raw_created = (created_el.text or created_el.get_attribute("innerText") or "").strip()

        m = re.search(r"\b(\d{2}/\d{2}/\d{2})\b", raw_created)
        if m:
            ad_created_at = m.group(1)
        else:
            # Handle Hebrew words like "היום" / "אתמול"
            tz = ZoneInfo("Asia/Jerusalem")
            today = datetime.now(tz).date()
            txt = raw_created.replace("\u200f", "").strip()  # strip RTL marks if present

            if "היום" in txt:
                ad_created_at = today.strftime("%d/%m/%y")
            elif "אתמול" in txt:
                ad_created_at = (today - timedelta(days=1)).strftime("%d/%m/%y")
            else:
                # Fallback: keep the raw text if unknown (your banner code will treat it as not a date)
                ad_created_at = raw_created

    # Seller type: prefer pre_seller_type from list page; fallback to heuristic
    seller_type = (pre_seller_type or "").strip().lower()
    if not seller_type:
        # Heuristic: look for hints on detail page (very defensive)
        body_cls = (driver.find_element(By.TAG_NAME, "body").get_attribute("class") or "").lower()
        seller_type = "agency" if "agency" in body_cls else "private"

    # Detail fields
    fields_dict = {}
    labels = driver.find_elements(By.CSS_SELECTOR, 'dd.item-detail_label__FnhAu')
    values = driver.find_elements(By.CSS_SELECTOR, 'dt.item-detail_value__QHPml[data-testid="item-detail"]')
    for label, value in zip(labels, values):
        key = (label.text or "").strip()
        val = (value.text or "").strip()
        if key:
            fields_dict[key] = val

    description_el = safe_find_element(By.CSS_SELECTOR, 'p')
    description = (description_el.text or "").strip() if description_el else ""

    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    car_data = {
        'brand': brand_name,
        'model': model_name,
        'title': title,
        'price': price,
        'year': car_year,
        'hands': car_hands,
        'km': car_km,
        'fields': json.dumps(fields_dict, ensure_ascii=False),
        'description': description,
        'url': url,
        'last_seen_at': now_iso,
        'location': location,
        'ad_created_at': ad_created_at,
        'seller_type': seller_type,
    }

    existing = get_listing(url)
    if existing:
        old_price = (existing.get("price") or "").strip()
        old_d = "".join(ch for ch in old_price if ch.isdigit())
        new_d = "".join(ch for ch in price if ch.isdigit())
        if price and new_d != old_d:
            ui.updated(title or url, old_price, price)
            stats.price_updates += 1

        # ✅ Preserve first_seen_at exactly as stored (never overwrite or "or now")
        car_data['first_seen_at'] = existing.get('first_seen_at')

    else:
        # ✅ Only when it's truly new do we set first_seen_at
        car_data['first_seen_at'] = now_iso
        stats.new_saved += 1


    # Defer DB write if a collector is provided; otherwise do immediate write.
    if collector is not None:
        collector.append(car_data)   # queued for the page batch
    else:
        upsert_listing(car_data)     # legacy direct-write path
        ui.saved(title, price)       # only log per-listing when we truly wrote now
        return car_data


# ===== Setup logging =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)
ui = ConsoleUI(logger)

# ===== Load brand/model data =====
BASE_URL = "https://www.yad2.co.il/vehicles/cars"
with open("value4urlBuild.json", "r", encoding="utf-8") as f:
    brands_data = json.load(f)

init_db()
init_subscriptions()

# subprocess.run(["nordvpn", "connect"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
driver = create_driver()
wait = WebDriverWait(driver, 10)

# =========================
# MAIN
# =========================
try:
    if INTERACTIVE:
        logger.info("🔍 Please choose brands and models...")
    selected_brands = select_brands()
    selected_brands_models = []
    for brand in selected_brands:
        selected_models = select_models(brand)
        if selected_models:
            selected_brands_models.append((brand, selected_models))

    if not selected_brands_models:
        logger.error("No brands/models selected. Exiting.")
    else:
        # Flatten to (brand, model|None) pairs so we scrape 1×1
        pairs: list[tuple[dict, dict|None]] = []
        for (b, models) in selected_brands_models:
            if models:
                for m in models:
                    pairs.append((b, m))
            else:
                pairs.append((b, None))  # brand-only 

        seen_by_pair = defaultdict(set)
        stats = CrawlStats()
        for (brand, model) in pairs:
            if isinstance(model, dict):
                model_label = model.get("model_name") or model.get("model_value") or ""
            else:
                model_label = ""
        
            pair_label = f"{brand['brand']}" + (f" / {model_label}" if model_label else " (all models)")
            single_url = build_single_url(brand, model if isinstance(model, dict) else None)
            SCRAPE_LOG.info(f"🌐 Scraping (single pair): {pair_label}")


            # if you want the rotation counter to reset every page, keep scraped here inside the page loop
            # if you want it to persist across pages for this brand/model, move 'scraped = 0' above the page loop
            for page in range(1, 2):  # extend if you want more pages
                SCRAPE_LOG.info(f"📄 Crawling page {page} for {pair_label}")
                paged_url = single_url if page == 1 else f"{single_url}&page={page}"

                # We pass only this pair (as list of one) so brand/model parsing stays accurate
                items = get_all_listing_links(paged_url, [(brand, [model] if model else [])])

                unique = {}
                pending_rows: list[dict] = []   
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    u = it.get('url')
                    if not u:
                        continue
                    unique[u] = it
                items = list(unique.values())

                # mark seen per cohort
                for it in items:
                    seen_by_pair[(it.get('brand'), it.get('model'))].add(it.get('url'))

                total_items = len(items)
                ui.page_header(page)
                stats.pages += 1
                stats.total_seen += total_items

                scraped = 0  # reset per page; move outside the page loop if you prefer cross-page counting

                for i, it in enumerate(items, 1):
                    if not it.get("visit"):
                        continue
                    
                    ui.mark_scrape_start(
                        page,
                        i,
                        total_items,
                        it.get("brand", ""),
                        it.get("model", ""),
                    )

                    scrape_car_detail(
                        it.get("url", ""),
                        it.get("brand", ""),
                        it.get("model", ""),
                        stats,
                        ui,
                        pre_seller_type=it.get("seller_type"),
                        collector=pending_rows, 
                    )
                    time.sleep(random.uniform(3.0, 6.0))

                
                
                    scraped += 1
                    if scraped >= _next_rotate_at:
                        rotate()
                        _next_rotate_at = random.randint(ROTATE_MIN, ROTATE_MAX)
                        scraped = 0

                    # Save every 10 listings
                    if len(pending_rows) >= 10:
                        bulk_upsert_listings(pending_rows)
                        SCRAPE_LOG.info(f"💾 Saved {len(pending_rows)} listings (batch)")
                        pending_rows.clear()


            
                affected = bulk_upsert_listings(pending_rows)
                pending_rows.clear()
                ui.batch_saved(affected)                
                time.sleep(random.uniform(5, 10))



        # Cleanup across everything we saw
        # removed = db_cleanup_deleted_listings(seen_by_pair) or 0
        removed = cleanup_deleted_listings_by_age(10)
        ui.removed(removed)
        stats.removed = removed

        ctx = ", ".join(
            (f"{b['brand']}" + (f" ({len(ms)} models)" if ms else "")) for (b, ms) in selected_brands_models
        ) or "Selection"
        stats.summarize(logger, ctx)


        


except Exception as e:
    logger.error(f"Unexpected error: {e}")
finally:
    try:
        if driver:
            driver.quit()
    except Exception as e:
        SCRAPE_LOG.warning(f"driver.quit() failed (ignored): {e}")

    SCRAPE_LOG.info("✅ All done. Data saved to SQLite (yad2_cars.db)")
