import os
import json
import uuid
import time
import logging
import asyncio
import sqlite3
from urllib.parse import urlparse, quote
import requests
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# ၁။ Logging & Strict Environment Setup (Security Hardened)
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Fallback Credentials များကို ဖယ်ရှားထားပါသည်။ Railway Variables တွင် မထည့်ပါက ချက်ချင်း Crash ဖြစ်ပါမည်။
try:
    TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    ADMIN_TELEGRAM_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
except (KeyError, ValueError) as e:
    logger.critical("❌ CRITICAL ERROR: TELEGRAM_BOT_TOKEN သို့မဟုတ် ADMIN_TELEGRAM_ID ကို Railway Variables တွင် မှန်ကန်စွာ မထည့်သွင်းထားပါ။")
    raise SystemExit("Environment variables missing or invalid.")

# Conversation States
(
    ADD_SRV_NAME, ADD_SRV_URL, ADD_SRV_USER, ADD_SRV_PASS, ADD_SRV_INBOUND,
    ADD_NAME, ADD_EXPIRY, ADD_DATA_LIMIT, ADD_FLOW,
    EDIT_EXPIRY_INPUT, EDIT_NAME_INPUT
) = range(11)

# ---------------------------------------------------------------------------
# ၂။ SQLite Database Setup (Multi-Server Storage)
# ---------------------------------------------------------------------------
DB_FILE = "servers.db"

def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS servers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                url TEXT NOT NULL,
                username TEXT NOT NULL,
                password TEXT NOT NULL,
                inbound_id INTEGER NOT NULL
            )
        """)
        conn.commit()

def get_all_servers() -> list[dict]:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM servers ORDER BY id DESC").fetchall()
        return [dict(row) for row in rows]

def get_server_by_id(server_id: int) -> dict | None:
    with sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()
        return dict(row) if row else None

def add_server_to_db(name: str, url: str, username: str, password: str, inbound_id: int) -> tuple[bool, str]:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            conn.execute(
                "INSERT INTO servers (name, url, username, password, inbound_id) VALUES (?, ?, ?, ?, ?)",
                (name, url.rstrip("/"), username, password, inbound_id)
            )
            conn.commit()
        return True, "ဆာဗာ ထည့်သွင်းမှု အောင်မြင်ပါသည်။"
    except sqlite3.IntegrityError:
        return False, "ဤဆာဗာအမည်ဖြင့် သိမ်းဆည်းထားပြီး ဖြစ်နေပါသည်။ အမည်ပြောင်း၍ ပြန်ကြိုးစားပါ။"
    except Exception as e:
        logger.error(f"Database Error: {e}")
        return False, "Database အမှားအယွင်း ဖြစ်ပေါ်နေပါသည်။"

def delete_server_from_db(server_id: int) -> bool:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
        conn.commit()
        return cursor.rowcount > 0

# ---------------------------------------------------------------------------
# ၃။ 3x-ui API Client (Smart Session & Auto Re-login Enabled)
# ---------------------------------------------------------------------------
class XUIClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.is_logged_in = False

    def login(self) -> bool:
        url = f"{self.base_url}/login"
        payload = {"username": self.username, "password": self.password}
        try:
            res = self.session.post(url, data=payload, timeout=10)
            try:
                data = res.json()
                if data.get("success", False):
                    self.is_logged_in = True
                    logger.info(f"✅ Panel ({self.base_url}) သို့ Login ဝင်ရောက်ပါပြီ။")
                    return True
            except Exception:
                logger.error(f"❌ Login Response is not JSON: {res.text[:100]}")
        except Exception as e:
            logger.error(f"❌ Login Connection Error: {e}")
        self.is_logged_in = False
        return False

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        if not self.is_logged_in and not self.login():
            return {"success": False, "msg": "Authentication failed."}
        url = f"{self.base_url}{endpoint}"
        try:
            res = self.session.request(method, url, timeout=10, **kwargs)
            try:
                return res.json()
            except Exception:
                logger.warning("🔄 Session သက်တမ်းကုန်သွားပါသဖြင့် Re-login ပြုလုပ်နေသည်...")
                if self.login():
                    res = self.session.request(method, url, timeout=10, **kwargs)
                    try:
                        return res.json()
                    except Exception:
                        return {"success": False, "msg": "Server Response Error"}
                else:
                    return {"success": False, "msg": "Re-authentication failed."}
        except Exception as e:
            logger.error(f"API Request Error: {e}")
            return {"success": False, "msg": "Network Error Occurred."}

    def get_inbound(self, inbound_id: int) -> dict:
        data = self._request("GET", f"/panel/api/inbounds/get/{inbound_id}")
        return data.get("obj", {}) if data.get("success") else {}

    def get_all_clients(self, inbound_id: int) -> list:
        inbound = self.get_inbound(inbound_id)
        if not inbound:
            return []
        settings = inbound.get("settings", {})
        if isinstance(settings, str):
            settings = json.loads(settings)
        return settings.get("clients", [])

    def get_client_by_uuid(self, inbound_id: int, client_uuid: str) -> dict:
        clients = self.get_all_clients(inbound_id)
        for c in clients:
            if c.get("id") == client_uuid:
                return c
        return {}
    
    def get_client_traffic(self, email: str) -> dict:
        data = self._request("GET", f"/panel/api/inbounds/getClientTraffics/{email}")
        if data.get("success"):
            return data.get("obj", {})
        return {}

    def add_client(self, inbound_id: int, email: str, expiry_days: int, total_gb: float, flow: str) -> tuple[bool, str, str]:
        client_uuid = str(uuid.uuid4())
        expiry_time = 0 if expiry_days <= 0 else int((time.time() + (expiry_days * 86400)) * 1000)
        total_bytes = 0 if total_gb <= 0 else int(total_gb * 1024 * 1024 * 1024)

        client_data = {
            "id": client_uuid, "flow": flow, "email": email,
            "limitIp": 0, "totalGB": total_bytes, "expiryTime": expiry_time,
            "enable": True, "tgId": "", "subId": ""
        }
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client_data]})}
        res = self._request("POST", "/panel/api/inbounds/addClient", json=payload)
        
        if not res.get("success"):
            return False, f"Key မဆောက်နိုင်ပါ: {res.get('msg', 'Unknown Server Error')}", ""
        return True, client_uuid, self.build_vless_link(inbound_id, client_uuid, email, flow)

    def update_client(self, inbound_id: int, client_uuid: str, updated_data: dict) -> tuple[bool, str]:
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [updated_data]})}
        res = self._request("POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json=payload)
        if res.get("success"):
            return True, "✅ ပြင်ဆင်မှု အောင်မြင်ပါသည်။"
        return False, f"❌ ပြင်ဆင်မှု မအောင်မြင်ပါ: {res.get('msg', 'Unknown')}"

    def delete_client(self, inbound_id: int, client_uuid: str) -> tuple[bool, str]:
        res = self._request("POST", f"/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}")
        if res.get("success"):
            return True, "✅ Key ကို အပြီးတိုင် ဖျက်သိမ်းပြီးပါပြီ။"
        return False, f"❌ ဖျက်သိမ်းမှု မအောင်မြင်ပါ: {res.get('msg', 'Unknown Server Error')}"

    def build_vless_link(self, inbound_id: int, client_uuid: str, email: str, flow: str) -> str:
        parsed = urlparse(self.base_url)
        vps_ip = parsed.hostname or "127.0.0.1"
        inbound = self.get_inbound(inbound_id)
        if not inbound:
            return f"vless://{client_uuid}@{vps_ip}:443?security=reality#{quote(email)}"
        try:
            port = inbound.get("port", 443)
            stream = json.loads(inbound.get("streamSettings", "{}")) if isinstance(inbound.get("streamSettings"), str) else inbound.get("streamSettings", {})
            reality = stream.get("realitySettings", {})
            settings = reality.get("settings", {})
            
            pbk = settings.get("publicKey") or reality.get("publicKey", "")
            fp = settings.get("fingerprint") or reality.get("fingerprint", "chrome")
            snis = settings.get("serverNames") or reality.get("serverNames") or ["www.amazon.com"]
            sni = snis[0] if isinstance(snis, list) and snis else str(snis).split(",")[0]
            sids = settings.get("shortIds") or reality.get("shortIds") or [""]
            sid = sids[0] if isinstance(sids, list) and sids else str(sids).split(",")[0]

            params = f"type=tcp&security=reality&pbk={pbk}&fp={fp}&sni={sni}"
            if sid: params += f"&sid={sid}"
            if flow: params += f"&flow={flow}"
            return f"vless://{client_uuid}@{vps_ip}:{port}?{params}#{quote(email)}"
        except Exception:
            return f"vless://{client_uuid}@{vps_ip}:443?security=reality#{quote(email)}"

# Cached Client Instances
active_xui_clients: dict[int, XUIClient] = {}

def get_client(server_data: dict) -> XUIClient:
    s_id = server_data["id"]
    if s_id not in active_xui_clients:
        active_xui_clients[s_id] = XUIClient(server_data["url"], server_data["username"], server_data["password"])
    return active_xui_clients[s_id]

# ---------------------------------------------------------------------------
# ၄။ UI Keyboards
# ---------------------------------------------------------------------------
def get_server_list_keyboard(servers: list[dict]):
    buttons = []
    for srv in servers:
        buttons.append([InlineKeyboardButton(f"🖥️ {srv['name']}", callback_data=f"srv_sel:{srv['id']}")])
    buttons.append([InlineKeyboardButton("➕ ဆာဗာအသစ် ထပ်ထည့်ရန်", callback_data="srv_add")])
    if servers:
        buttons.append([InlineKeyboardButton("🗑️ ဆာဗာများ ဖျက်သိမ်းရန်", callback_data="srv_del_menu")])
    return InlineKeyboardMarkup(buttons)

def get_dashboard_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ VLESS Key အသစ်ဆောက်ရန်", callback_data="menu:add_key")],
        [InlineKeyboardButton("📋 Key စာရင်းများ စီမံရန် (Enable/Disable/Edit)", callback_data="menu:list_keys")],
        [InlineKeyboardButton("🔄 Panel အခြေအနေ ပြန်လည်စစ်ဆေးရန်", callback_data="menu:refresh")],
        [InlineKeyboardButton("🔙 ဆာဗာရွေးချယ်သည့် စာမျက်နှာသို့", callback_data="menu:servers")]
    ])

def get_back_to_dashboard_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 ဆာဗာ Dashboard သို့ ပြန်သွားမည်", callback_data="menu:dashboard")]
    ])

# ---------------------------------------------------------------------------
# ၅။ Startup & Server Selection Flow (Loop Bug Fixed)
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    
    servers = await asyncio.to_thread(get_all_servers)
    
    if not servers:
        msg = (
            "🚀 <b>3x-ui Multi-Server Manager</b>\n\n"
            "⚠️ <b>လက်ရှိတွင် ဆာဗာတစ်ခုမှ မရှိသေးပါ။</b>\n\n"
            "ကျေးဇူးပြု၍ ပထမဆုံး ဆာဗာအတွက် <b>ဆာဗာအမည် (Server Name)</b> ရိုက်ထည့်ပေးပါ 👇\n"
            "<i>(ဥပမာ - SG-Server-1, US-Premium)</i>"
        )
        if update.message:
            await update.message.reply_text(msg, parse_mode="HTML")
        elif update.callback_query:
            await update.callback_query.message.edit_text(msg, parse_mode="HTML")
        return ADD_SRV_NAME

    msg = "🚀 <b>3x-ui Multi-Server Manager</b>\n\nစီမံလိုသော ဆာဗာကို အောက်ပါစာရင်းမှ ရွေးချယ်ပါ 👇"
    if update.message:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=get_server_list_keyboard(servers))
    elif update.callback_query:
        await update.callback_query.message.edit_text(msg, parse_mode="HTML", reply_markup=get_server_list_keyboard(servers))
    return ConversationHandler.END

async def show_server_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, srv: dict):
    msg = (
        f"📌 <b>Active Server Dashboard</b>\n\n"
        f"🖥️ <b>ဆာဗာ:</b> <code>{srv['name']}</code>\n"
        f"🔗 <b>Link:</b> <code>{srv['url']}</code>\n"
        f"🎯 <b>Inbound ID:</b> <code>{srv['inbound_id']}</code>\n\n"
        f"အောက်ပါ လုပ်ဆောင်ချက်များမှ တစ်ခု ရွေးချယ်ပါ 👇"
    )
    if update.callback_query:
        await update.callback_query.message.edit_text(msg, parse_mode="HTML", reply_markup=get_dashboard_keyboard())
    elif update.message:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=get_dashboard_keyboard())

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_TELEGRAM_ID:
        return

    data = query.data
    if data == "menu:servers":
        await start_command(update, context)
        return ConversationHandler.END

    elif data == "menu:dashboard" or data == "menu:refresh":
        srv = context.user_data.get("active_server")
        if not srv:
            await start_command(update, context)
            return ConversationHandler.END
        await show_server_dashboard(update, context, srv)

    elif data.startswith("srv_sel:"):
        srv_id = int(data.split(":")[1])
        srv = await asyncio.to_thread(get_server_by_id, srv_id)
        if not srv:
            await query.message.edit_text("❌ ဆာဗာ ရှာမတွေ့ပါ။", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ပြန်သွားမည်", callback_data="menu:servers")]]))
            return
        
        context.user_data["active_server"] = srv
        await show_server_dashboard(update, context, srv)

    elif data == "srv_del_menu":
        servers = await asyncio.to_thread(get_all_servers)
        buttons = [[InlineKeyboardButton(f"🗑️ ဖျက်မည် - {s['name']}", callback_data=f"srv_del_act:{s['id']}")] for s in servers]
        buttons.append([InlineKeyboardButton("🔙 နောက်သို့", callback_data="menu:servers")])
        await query.message.edit_text("🗑️ <b>ဖျက်သိမ်းလိုသော ဆာဗာကို ရွေးချယ်ပါ</b> 👇", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("srv_del_act:"):
        srv_id = int(data.split(":")[1])
        await asyncio.to_thread(delete_server_from_db, srv_id)
        if srv_id in active_xui_clients:
            del active_xui_clients[srv_id]
        await query.answer("✅ ဆာဗာ ဖျက်သိမ်းပြီးပါပြီ။")
        await start_command(update, context)

    elif data == "menu:list_keys":
        await show_client_list(update, context)

    elif data.startswith("client_detail:"):
        await show_client_detail(update, context, data.split(":")[1])

    elif data.startswith("toggle:"):
        await toggle_client_status(update, context, data.split(":")[1])

    elif data.startswith("edit_menu:"):
        await show_edit_menu(update, context, data.split(":")[1])

    elif data.startswith("del_client:"):
        await delete_client_action(update, context, data.split(":")[1])

# ---------------------------------------------------------------------------
# ၆။ Add Server Wizard (With Strict Admin Check)
# ---------------------------------------------------------------------------
async def srv_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    
    await query.message.edit_text(
        "➕ <b>ဆာဗာအသစ် ထည့်သွင်းခြင်း (၁/၅)</b>\n\n"
        "ကျေးဇူးပြု၍ ဆာဗာအတွက် <b>အမည် (Server Name)</b> ရိုက်ထည့်ပေးပါ 👇\n<i>(ဥပမာ - SG-Server-1)</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ ဖျက်သိမ်းမည်", callback_data="menu:servers")]])
    )
    return ADD_SRV_NAME

async def srv_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    context.user_data["srv_name"] = update.message.text.strip()
    await update.message.reply_text(
        f"✅ ဆာဗာအမည် <b>{context.user_data['srv_name']}</b> မှတ်သားပြီးပါပြီ။\n\n"
        "🔗 <b>အဆင့် (၂/၅): 3x-ui Panel Link ရိုက်ထည့်ပါ</b> 👇\n"
        "<i>(ဥပမာ - http://167.172.73.82:53073/KycDj1Uzisw3vpu)</i>",
        parse_mode="HTML"
    )
    return ADD_SRV_URL

async def srv_receive_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    url = update.message.text.strip().rstrip("/")
    if not url.startswith("http://") and not url.startswith("https://"):
        await update.message.reply_text("⚠️ Link URL သည် http:// သို့မဟုတ် https:// ဖြင့် စတင်ရပါမည်။ ပြန်လည်ရိုက်ထည့်ပါ 👇")
        return ADD_SRV_URL
    
    context.user_data["srv_url"] = url
    await update.message.reply_text("👤 <b>အဆင့် (၃/၅): Panel Username ရိုက်ထည့်ပါ</b> 👇", parse_mode="HTML")
    return ADD_SRV_USER

async def srv_receive_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    context.user_data["srv_user"] = update.message.text.strip()
    await update.message.reply_text("🔑 <b>အဆင့် (၄/၅): Panel Password ရိုက်ထည့်ပါ</b> 👇", parse_mode="HTML")
    return ADD_SRV_PASS

async def srv_receive_pass(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    context.user_data["srv_pass"] = update.message.text.strip()
    await update.message.reply_text(
        "🎯 <b>အဆင့် (၅/၅): Inbound ID (ဂဏန်း) ရိုက်ထည့်ပါ</b> 👇\n<i>(များသောအားဖြင့် Reality Inbound ID သည် 1 ဖြစ်သည်)</i>",
        parse_mode="HTML"
    )
    return ADD_SRV_INBOUND

async def srv_receive_inbound(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    try:
        inbound_id = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("⚠️ ကျေးဇူးပြု၍ Inbound ID ကို ဂဏန်းကိန်းဂဏန်းသီးသန့်သာ ရိုက်ထည့်ပါ။ 👇")
        return ADD_SRV_INBOUND
    
    u = context.user_data
    success, msg = await asyncio.to_thread(
        add_server_to_db, u["srv_name"], u["srv_url"], u["srv_user"], u["srv_pass"], inbound_id
    )
    
    if success:
        await update.message.reply_text(f"✅ {msg}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🖥️ ဆာဗာစာရင်းသို့", callback_data="menu:servers")]]))
    else:
        await update.message.reply_text(f"❌ {msg}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 ပြန်လည်ကြိုးစားမည်", callback_data="menu:servers")]]))
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# ၇။ Key စာရင်းနှင့် စီမံခန့်ခွဲမှု (With Admin Check, Delete Key & Safe Errors)
# ---------------------------------------------------------------------------
async def show_client_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return
    query = update.callback_query
    srv = context.user_data.get("active_server")
    if not srv:
        await query.message.edit_text("⚠️ Active Server ရွေးချယ်ထားခြင်း မရှိပါ။", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🖥️ ဆာဗာရွေးရန်", callback_data="menu:servers")]]))
        return

    xui = get_client(srv)
    clients = await asyncio.to_thread(xui.get_all_clients, srv["inbound_id"])
    
    if not clients:
        await query.message.edit_text(f"⚠️ <b>{srv['name']} တွင်း၌ Key တစ်ခုမှ မရှိသေးပါ။</b>", parse_mode="HTML", reply_markup=get_back_to_dashboard_button())
        return

    buttons = []
    for c in clients:
        status_emoji = "🟢" if c.get("enable", False) else "🔴"
        expiry_time = c.get("expiryTime", 0)
        if expiry_time > 0 and expiry_time < int(time.time() * 1000):
            status_emoji = "⚠️(ရက်လွန်)"
        
        btn_text = f"{status_emoji} {c.get('email', 'Unnamed')} | Flow: {c.get('flow', 'None') or 'Default'}"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"client_detail:{c.get('id')}")])

    buttons.append([InlineKeyboardButton("🔙 ဆာဗာ Dashboard သို့ ပြန်သွားမည်", callback_data="menu:dashboard")])
    await query.message.edit_text(
        f"📋 <b>{srv['name']} - Key စာရင်းများ -</b>\n\n<i>(🟢 ဖွင့်ထားသည် | 🔴 ပိတ်ထားသည် | ⚠️ ရက်လွန်)</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )

async def show_client_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, client_uuid: str):
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return
    query = update.callback_query
    srv = context.user_data.get("active_server")
    xui = get_client(srv)
    
    try:
        client = await asyncio.to_thread(xui.get_client_by_uuid, srv["inbound_id"], client_uuid)
        if not client:
            await query.message.edit_text("❌ Key အချက်အလက် ရှာမတွေ့ပါ။", reply_markup=get_back_to_dashboard_button())
            return

        email = client.get("email", "Unnamed")
        traffic_data = await asyncio.to_thread(xui.get_client_traffic, email)
        used_bytes = int(traffic_data.get("up", 0)) + int(traffic_data.get("down", 0))
        used_gb = round(used_bytes / (1024**3), 2)

        flow = client.get("flow", "") or "Default"
        enable = client.get("enable", False)
        expiry_time = int(client.get("expiryTime") or 0)
        total_gb_bytes = int(client.get("totalGB") or 0)

        if expiry_time <= 0:
            exp_str = "အကန့်အသတ်မရှိ (Unlimited) ♾️"
        else:
            days_left = int((expiry_time - int(time.time() * 1000)) / (1000 * 86400))
            if days_left >= 0:
                exp_str = f"{time.strftime('%Y-%m-%d', time.localtime(expiry_time/1000))} ({days_left} ရက် လိုသေးသည်)"
            else:
                exp_str = f"⚠️ ရက်လွန်သွားပါပြီ ({abs(days_left)} ရက်လွန်)"

        total_gb_str = f"{round(total_gb_bytes / (1024**3), 2)} GB" if total_gb_bytes > 0 else "Unlimited ♾️"
        status_str = "🟢 အသုံးပြုခွင့် ဖွင့်ထားသည် (Enabled)" if enable else "🔴 ပိတ်ထားသည် (Disabled)"
        vless_link = await asyncio.to_thread(xui.build_vless_link, srv["inbound_id"], client_uuid, email, flow)
        toggle_btn_text = "🔴 ယာယီ ပိတ်မည် (Disable)" if enable else "🟢 ပြန်လည် ဖွင့်မည် (Enable)"
        
        buttons = [
            [InlineKeyboardButton(toggle_btn_text, callback_data=f"toggle:{client_uuid}")],
            [InlineKeyboardButton("✏️ အမည်/သက်တမ်း ပြင်ဆင်မည် (Edit)", callback_data=f"edit_menu:{client_uuid}")],
            [InlineKeyboardButton("🗑️ Key ကို အပြီးတိုင် ဖျက်သိမ်းမည် (Delete)", callback_data=f"del_client:{client_uuid}")],
            [InlineKeyboardButton("📋 Key စာရင်းသို့", callback_data="menu:list_keys")],
            [InlineKeyboardButton("🔙 ဆာဗာ Dashboard သို့", callback_data="menu:dashboard")]
        ]

        msg = (
            f"👤 <b>Client အချက်အလက် [{srv['name']}]</b>\n\n"
            f"🔹 <b>အမည်:</b> <code>{email}</code>\n"
            f"🔹 <b>အခြေအနေ:</b> {status_str}\n"
            f"⏳ <b>သက်တမ်း:</b> <code>{exp_str}</code>\n"
            f"📊 <b>Data Limit:</b> <code>{total_gb_str}</code>\n"
            f"📈 <b>အသုံးပြုထားသော Data:</b> <code>{used_gb} GB</code>\n"
            f"⚡ <b>Flow:</b> <code>{flow}</code>\n\n"
            f"📋 <b>VLESS Link:</b>\n<code>{vless_link}</code>"
        )
        await query.message.edit_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        logger.error(f"Error in show_client_detail: {e}", exc_info=True)
        await query.message.edit_text("⚠️ <b>ဆာဗာချိတ်ဆက်မှု အမှားအယွင်းဖြစ်ပေါ်နေပါသည်။</b>\n\n ကျေးဇူးပြု၍ ခဏစောင့်ပြီး ပြန်လည်ကြိုးစားပါ။", parse_mode="HTML", reply_markup=get_back_to_dashboard_button())

async def toggle_client_status(update: Update, context: ContextTypes.DEFAULT_TYPE, client_uuid: str):
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return
    query = update.callback_query
    srv = context.user_data.get("active_server")
    xui = get_client(srv)
    
    client = await asyncio.to_thread(xui.get_client_by_uuid, srv["inbound_id"], client_uuid)
    if not client: return
    
    client["enable"] = not client.get("enable", False)
    success, msg = await asyncio.to_thread(xui.update_client, srv["inbound_id"], client_uuid, client)
    await query.answer(msg)
    await show_client_detail(update, context, client_uuid)

async def delete_client_action(update: Update, context: ContextTypes.DEFAULT_TYPE, client_uuid: str):
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return
    query = update.callback_query
    srv = context.user_data.get("active_server")
    xui = get_client(srv)
    
    success, msg = await asyncio.to_thread(xui.delete_client, srv["inbound_id"], client_uuid)
    await query.answer(msg, show_alert=True)
    await show_client_list(update, context)

# ---------------------------------------------------------------------------
# ၈။ Add Key Wizard (With Strict Admin Check)
# ---------------------------------------------------------------------------
async def addkey_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    srv = context.user_data.get("active_server")
    
    buttons = [[InlineKeyboardButton("❌ လုပ်ဆောင်ချက် ဖျက်သိမ်းမည်", callback_data="menu:dashboard")]]
    await query.message.edit_text(
        f"➕ <b>Key အသစ် ဆောက်လုပ်ခြင်း [{srv['name']}]</b>\n\n"
        "အဆင့် (၁/၄) : Client အတွက် <b>အမည် (Name / Remarks)</b> ရိုက်ထည့်ပေးပါ 👇\n\n<i>(ဥပမာ - MyPhone, MgMg_Laptop)</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADD_NAME

async def addkey_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    client_name = update.message.text.strip().replace(" ", "_")
    context.user_data["add_name"] = client_name
    
    buttons = [
        [InlineKeyboardButton("♾️ အကန့်အသတ်မရှိ (Unlimited)", callback_data="add_exp:0")],
        [InlineKeyboardButton("၇ ရက်", callback_data="add_exp:7"), InlineKeyboardButton("၃၀ ရက်", callback_data="add_exp:30")],
        [InlineKeyboardButton("၆၀ ရက်", callback_data="add_exp:60"), InlineKeyboardButton("၉၀ ရက်", callback_data="add_exp:90")],
        [InlineKeyboardButton("❌ ဖျက်သိမ်းမည်", callback_data="menu:dashboard")]
    ]
    await update.message.reply_text(
        f"✅ အမည် <b>{client_name}</b> ကို မှတ်သားပြီးပါပြီ။\n\n"
        "⏳ <b>အဆင့် (၂/၄) : သက်တမ်း ရွေးချယ်ပါ</b>\n\n"
        "ရက်အရေအတွက် ရွေးချယ်ပါ သို့မဟုတ် ကိုယ်တိုင် ဂဏန်းရိုက်ထည့်ပါ 👇",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADD_EXPIRY

async def addkey_receive_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        expiry_days = int(query.data.split(":")[1])
        target_msg = query.message
    else:
        try:
            expiry_days = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("⚠️ ကျေးဇူးပြု၍ ရက်အရေအတွက်ကို ဂဏန်းသီးသန့် ရိုက်ထည့်ပါ သို့မဟုတ် ခလုတ်ကို နှိပ်ပါ။")
            return ADD_EXPIRY
        target_msg = update.message

    context.user_data["add_expiry"] = expiry_days
    expiry_str = f"{expiry_days} ရက်" if expiry_days > 0 else "အကန့်အသတ်မရှိ (Unlimited) ♾️"

    buttons = [
        [InlineKeyboardButton("♾️ အကန့်အသတ်မရှိ (Unlimited GB)", callback_data="add_gb:0")],
        [InlineKeyboardButton("10 GB", callback_data="add_gb:10"), InlineKeyboardButton("30 GB", callback_data="add_gb:30")],
        [InlineKeyboardButton("50 GB", callback_data="add_gb:50"), InlineKeyboardButton("100 GB", callback_data="add_gb:100")],
        [InlineKeyboardButton("❌ ဖျက်သိမ်းမည်", callback_data="menu:dashboard")]
    ]
    await target_msg.reply_text(
        f"✅ သက်တမ်း <b>{expiry_str}</b> သတ်မှတ်ပြီးပါပြီ။\n\n"
        "📊 <b>အဆင့် (၃/၄) : Data Usage Limit (GB) သတ်မှတ်ပါ</b>\n\n"
        "ခလုတ်များမှ ရွေးချယ်ပါ သို့မဟုတ် လိုချင်သော <b>GB ပမာဏကို ဂဏန်းဖြင့် ရိုက်ထည့်ပါ</b> 👇",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADD_DATA_LIMIT

async def addkey_receive_data_limit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        total_gb = float(query.data.split(":")[1])
        target_msg = query.message
    else:
        try:
            total_gb = float(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("⚠️ ကျေးဇူးပြု၍ GB ပမာဏကို ဂဏန်းသီးသန့်သာ ရိုက်ထည့်ပါ။")
            return ADD_DATA_LIMIT
        target_msg = update.message

    context.user_data["add_gb"] = total_gb
    gb_str = f"{total_gb} GB" if total_gb > 0 else "အကန့်အသတ်မရှိ (Unlimited GB) ♾️"

    buttons = [
        [InlineKeyboardButton("⚡ xtls-rprx-vision (Default - အကြံပြုသည်)", callback_data="add_flow:xtls-rprx-vision")],
        [InlineKeyboardButton("⚪ Flow မသုံးပါ (None / Empty)", callback_data="add_flow:")],
        [InlineKeyboardButton("❌ ဖျက်သိမ်းမည်", callback_data="menu:dashboard")]
    ]
    await target_msg.reply_text(
        f"✅ Data Limit <b>{gb_str}</b> သတ်မှတ်ပြီးပါပြီ။\n\n"
        "⚡ <b>အဆင့် (၄/၄) : Traffic Flow ရွေးချယ်ပါ 👇</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADD_FLOW

async def addkey_receive_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    flow = query.data.split(":")[1]

    srv = context.user_data.get("active_server")
    xui = get_client(srv)

    name = context.user_data.get("add_name", "User")
    expiry = context.user_data.get("add_expiry", 0)
    total_gb = context.user_data.get("add_gb", 0)

    await query.message.edit_text("⏳ <b>Key ဆောက်လုပ်နေပါသည်...</b> ခဏစောင့်ပေးပါ ခင်ဗျာ။ ⚙️", parse_mode="HTML")

    success, uuid_or_err, vless_link = await asyncio.to_thread(
        xui.add_client, srv["inbound_id"], name, expiry, total_gb, flow
    )

    if not success:
        await query.message.edit_text(f"❌ <b>Key ထုတ်ယူခြင်း မအောင်မြင်ပါ</b>\n\n{uuid_or_err}", parse_mode="HTML", reply_markup=get_back_to_dashboard_button())
        return ConversationHandler.END

    expiry_str = f"{expiry} ရက်" if expiry > 0 else "အကန့်အသတ်မရှိ (Unlimited) ♾️"
    gb_str = f"{total_gb} GB" if total_gb > 0 else "အကန့်အသတ်မရှိ (Unlimited GB) ♾️"

    success_msg = (
        f"✅ <b>[{srv['name']}] VLESS + Reality Key ထုတ်ယူခြင်း အောင်မြင်ပါသည်!</b> 🚀\n\n"
        f"👤 <b>အမည်:</b> <code>{name}</code>\n"
        f"⏳ <b>သက်တမ်း:</b> <code>{expiry_str}</code>\n"
        f"📊 <b>Data Limit:</b> <code>{gb_str}</code>\n"
        f"⚡ <b>Flow:</b> <code>{flow or 'None'}</code>\n\n"
        f"📋 <b>VLESS Link (တစ်ချက်နှိပ်၍ Copy ကူးပါ) -</b>\n\n"
        f"<code>{vless_link}</code>"
    )
    await query.message.edit_text(success_msg, parse_mode="HTML", reply_markup=get_back_to_dashboard_button())
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# ၉။ Edit Key Handlers (With Strict Admin Check & Delete Key)
# ---------------------------------------------------------------------------
async def show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, client_uuid: str):
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return
    query = update.callback_query
    context.user_data["edit_uuid"] = client_uuid
    srv = context.user_data.get("active_server")
    xui = get_client(srv)
    client = await asyncio.to_thread(xui.get_client_by_uuid, srv["inbound_id"], client_uuid)
    
    buttons = [
        [InlineKeyboardButton("⏳ သက်တမ်း ရက်တိုးရန် / ပြင်ဆင်ရန်", callback_data=f"edit_act:expiry:{client_uuid}")],
        [InlineKeyboardButton("👤 အမည် (Name/Remarks) ပြောင်းရန်", callback_data=f"edit_act:name:{client_uuid}")],
        [InlineKeyboardButton("⚡ Flow (xtls-rprx-vision) ပြောင်းရန်", callback_data=f"edit_act:flow:{client_uuid}")],
        [InlineKeyboardButton("🗑️ Key ကို အပြီးတိုင် ဖျက်သိမ်းမည် (Delete)", callback_data=f"del_client:{client_uuid}")],
        [InlineKeyboardButton("🔙 နောက်သို့ ပြန်သွားမည်", callback_data=f"client_detail:{client_uuid}")],
        [InlineKeyboardButton("🔙 Dashboard သို့", callback_data="menu:dashboard")]
    ]
    await query.message.edit_text(
        f"✏️ <b>{client.get('email')}</b> အတွက် ပြင်ဆင်လိုသည့် အပိုင်းကို ရွေးချယ်ပါ 👇",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )

async def edit_action_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action, client_uuid = parts[1], parts[2]
    context.user_data["edit_uuid"] = client_uuid
    srv = context.user_data.get("active_server")
    xui = get_client(srv)
    client = await asyncio.to_thread(xui.get_client_by_uuid, srv["inbound_id"], client_uuid)

    if action == "expiry":
        buttons = [
            [InlineKeyboardButton("➕ ၇ ရက် တိုးမည်", callback_data="edit_exp_add:7"), InlineKeyboardButton("➕ ၃၀ ရက် တိုးမည်", callback_data="edit_exp_add:30")],
            [InlineKeyboardButton("♾️ အကန့်အသတ်မရှိ ပြောင်းမည် (Unlimited)", callback_data="edit_exp_set:0")],
            [InlineKeyboardButton("🔙 နောက်သို့", callback_data=f"edit_menu:{client_uuid}")]
        ]
        await query.message.edit_text(
            f"⏳ <b>{client.get('email')}</b> အတွက် ရက်တိုးရန် ခလုတ်နှိပ်ပါ သို့မဟုတ် ကိုယ်တိုင် ဂဏန်းရိုက်ထည့်ပါ 👇",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return EDIT_EXPIRY_INPUT

    elif action == "name":
        buttons = [[InlineKeyboardButton("🔙 နောက်သို့", callback_data=f"edit_menu:{client_uuid}")]]
        await query.message.edit_text(
            f"👤 <b>{client.get('email')}</b> အတွက် အမည်အသစ် ရိုက်ထည့်ပေးပါ 👇",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return EDIT_NAME_INPUT

    elif action == "flow":
        new_flow = "xtls-rprx-vision" if not client.get("flow") else ""
        client["flow"] = new_flow
        success, msg = await asyncio.to_thread(xui.update_client, srv["inbound_id"], client_uuid, client)
        await query.answer(msg)
        await show_client_detail(update, context, client_uuid)
        return ConversationHandler.END

async def edit_receive_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    client_uuid = context.user_data.get("edit_uuid")
    srv = context.user_data.get("active_server")
    xui = get_client(srv)
    client = await asyncio.to_thread(xui.get_client_by_uuid, srv["inbound_id"], client_uuid)
    
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        mode, val = query.data.split(":")[0], int(query.data.split(":")[1])
        if mode == "edit_exp_add":
            current_exp = client.get("expiryTime", 0)
            base_time = max(current_exp, int(time.time() * 1000)) if current_exp > 0 else int(time.time() * 1000)
            client["expiryTime"] = base_time + (val * 86400 * 1000)
        else:
            client["expiryTime"] = 0
        target_msg = query.message
    else:
        try:
            days = int(update.message.text.strip())
            client["expiryTime"] = 0 if days <= 0 else int((time.time() + (days * 86400)) * 1000)
            target_msg = update.message
        except ValueError:
            await update.message.reply_text("⚠️ ကျေးဇူးပြု၍ ဂဏန်းကိန်းဂဏန်းသာ ရိုက်ထည့်ပါ။")
            return EDIT_EXPIRY_INPUT

    client["enable"] = True
    success, msg = await asyncio.to_thread(xui.update_client, srv["inbound_id"], client_uuid, client)
    await target_msg.reply_text(f"✅ သက်တမ်း ပြင်ဆင်ပြီးပါပြီ။ {msg}", reply_markup=get_back_to_dashboard_button())
    return ConversationHandler.END

async def edit_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.effective_user.id != ADMIN_TELEGRAM_ID: return ConversationHandler.END
    client_uuid = context.user_data.get("edit_uuid")
    srv = context.user_data.get("active_server")
    xui = get_client(srv)
    client = await asyncio.to_thread(xui.get_client_by_uuid, srv["inbound_id"], client_uuid)
    
    new_name = update.message.text.strip().replace(" ", "_")
    client["email"] = new_name
    success, msg = await asyncio.to_thread(xui.update_client, srv["inbound_id"], client_uuid, client)
    await update.message.reply_text(f"✅ အမည်အသစ် <b>{new_name}</b> သို့ ပြောင်းလဲပြီးပါပြီ။", parse_mode="HTML", reply_markup=get_back_to_dashboard_button())
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# ၁၀။ Main Application Setup
# ---------------------------------------------------------------------------
def main():
    init_db()  # Initialize SQLite Table
    logger.info("🚀 Starting Security-Hardened 3x-ui Multi-Server Bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    add_srv_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(srv_add_start, pattern="^srv_add$"),
            CommandHandler("start", start_command)
        ],
        states={
            ADD_SRV_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_receive_name)],
            ADD_SRV_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_receive_url)],
            ADD_SRV_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_receive_user)],
            ADD_SRV_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_receive_pass)],
            ADD_SRV_INBOUND: [MessageHandler(filters.TEXT & ~filters.COMMAND, srv_receive_inbound)]
        },
        fallbacks=[CallbackQueryHandler(menu_router, pattern="^menu:servers$")],
        allow_reentry=True
    )

    add_key_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(addkey_start, pattern="^menu:add_key$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addkey_receive_name)],
            ADD_EXPIRY: [
                CallbackQueryHandler(addkey_receive_expiry, pattern="^add_exp:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addkey_receive_expiry)
            ],
            ADD_DATA_LIMIT: [
                CallbackQueryHandler(addkey_receive_data_limit, pattern="^add_gb:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addkey_receive_data_limit)
            ],
            ADD_FLOW: [CallbackQueryHandler(addkey_receive_flow, pattern="^add_flow:")]
        },
        fallbacks=[CallbackQueryHandler(menu_router, pattern="^menu:dashboard$")],
        allow_reentry=True
    )

    edit_key_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_action_router, pattern="^edit_act:")],
        states={
            EDIT_EXPIRY_INPUT: [
                CallbackQueryHandler(edit_receive_expiry, pattern="^edit_exp_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_expiry)
            ],
            EDIT_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_name)]
        },
        fallbacks=[CallbackQueryHandler(menu_router, pattern="^menu:dashboard$")],
        allow_reentry=True
    )

    app.add_handler(add_srv_conv)
    app.add_handler(add_key_conv)
    app.add_handler(edit_key_conv)
    app.add_handler(CommandHandler(["start", "help", "menu"], start_command))
    app.add_handler(CallbackQueryHandler(menu_router))

    logger.info("✅ Bot is online, secured, and ready for deployment!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
