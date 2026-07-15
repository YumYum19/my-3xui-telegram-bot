import os
import json
import uuid
import time
import logging
import asyncio
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
# ၁။ Logging Setup (Railway တွင် စောင့်ကြည့်ရန်)
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ၂။ Environment Variables & Fallback Credentials
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8993287223:AAHnmFVfJTHkTURQNsFZeZJtRk1REfB5NEg")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", 1824573670))

XUI_PANEL_URL = os.getenv("XUI_PANEL_URL", "http://167.172.73.82:53073/KycDj1Uzisw3vpu").rstrip("/")
XUI_USERNAME = os.getenv("XUI_USERNAME", "auzbMTwGgX")
XUI_PASSWORD = os.getenv("XUI_PASSWORD", "skA9eqRFHv")
INBOUND_ID = int(os.getenv("INBOUND_ID", 1))

parsed_url = urlparse(XUI_PANEL_URL)
VPS_IP = parsed_url.hostname or "167.172.73.82"

# Conversation States
(
    ADD_NAME, ADD_EXPIRY, ADD_FLOW,
    EDIT_EXPIRY_INPUT, EDIT_NAME_INPUT
) = range(5)

# ---------------------------------------------------------------------------
# ၃။ 3x-ui API Client (Dynamic Links & Smart Session)
# ---------------------------------------------------------------------------
class XUIClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.is_logged_in = False

    def login(self) -> bool:
        url = f"{self.base_url}/login"
        payload = {"username": self.username, "password": self.password}
        try:
            res = self.session.post(url, data=payload, timeout=10)
            data = res.json()
            if data.get("success", False):
                self.is_logged_in = True
                return True
        except Exception as e:
            logger.error(f"Login Error: {e}")
        return False

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        if not self.is_logged_in and not self.login():
            return {"success": False, "msg": "Authentication failed."}
        url = f"{self.base_url}{endpoint}"
        try:
            res = self.session.request(method, url, timeout=10, **kwargs)
            if res.status_code == 401 or "/login" in res.url:
                if self.login():
                    res = self.session.request(method, url, timeout=10, **kwargs)
                else:
                    return {"success": False, "msg": "Re-authentication failed."}
            return res.json()
        except Exception as e:
            return {"success": False, "msg": str(e)}

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

    def add_client(self, inbound_id: int, email: str, expiry_days: int, flow: str) -> tuple[bool, str, str]:
        client_uuid = str(uuid.uuid4())
        expiry_time = 0 if expiry_days <= 0 else int((time.time() + (expiry_days * 86400)) * 1000)

        client_data = {
            "id": client_uuid, "flow": flow, "email": email,
            "limitIp": 0, "totalGB": 0, "expiryTime": expiry_time,
            "enable": True, "tgId": "", "subId": ""
        }
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [client_data]})}
        res = self._request("POST", "/panel/api/inbounds/addClient", json=payload)
        
        if not res.get("success"):
            return False, f"Key မဆောက်နိုင်ပါ: {res.get('msg', 'Error')}", ""
        return True, client_uuid, self.build_vless_link(inbound_id, client_uuid, email, flow)

    def update_client(self, inbound_id: int, client_uuid: str, updated_data: dict) -> tuple[bool, str]:
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [updated_data]})}
        res = self._request("POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json=payload)
        if res.get("success"):
            return True, "✅ ပြင်ဆင်မှု အောင်မြင်ပါသည်။"
        return False, f"❌ ပြင်ဆင်မှု မအောင်မြင်ပါ: {res.get('msg', 'Unknown')}"

    def build_vless_link(self, inbound_id: int, client_uuid: str, email: str, flow: str) -> str:
        inbound = self.get_inbound(inbound_id)
        if not inbound:
            return f"vless://{client_uuid}@{VPS_IP}:443?security=reality#{quote(email)}"
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
            return f"vless://{client_uuid}@{VPS_IP}:{port}?{params}#{quote(email)}"
        except Exception:
            return f"vless://{client_uuid}@{VPS_IP}:443?security=reality#{quote(email)}"

xui = XUIClient(XUI_PANEL_URL, XUI_USERNAME, XUI_PASSWORD)

# ---------------------------------------------------------------------------
# ၄။ UI Helpers (Main Menu & Back Buttons)
# ---------------------------------------------------------------------------
def get_main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ VLESS Key အသစ်ဆောက်ရန်", callback_data="menu:add_key")],
        [InlineKeyboardButton("📋 Key စာရင်းများ စီမံရန် (Enable/Disable/Edit)", callback_data="menu:list_keys")],
        [InlineKeyboardButton("🔄 Panel အခြေအနေ ပြန်လည်စစ်ဆေးရန်", callback_data="menu:refresh")]
    ])

def get_back_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 ပင်မစာမျက်နှာသို့ ပြန်သွားမည်", callback_data="menu:home")]
    ])

# ---------------------------------------------------------------------------
# ၅။ Main Menu Handlers
# ---------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_TELEGRAM_ID:
        return
    msg = (
        "🚀 <b>3x-ui Reality VPN Control Panel</b>\n\n"
        "လိုချင်သော လုပ်ဆောင်ချက်ကို အောက်ပါ ခလုတ်များမှ တစ်ဆင့် ရွေးချယ်ပါ ခင်ဗျာ 👇"
    )
    if update.message:
        await update.message.reply_text(msg, parse_mode="HTML", reply_markup=get_main_menu_keyboard())
    elif update.callback_query:
        await update.callback_query.message.edit_text(msg, parse_mode="HTML", reply_markup=get_main_menu_keyboard())

async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_TELEGRAM_ID:
        return

    data = query.data
    if data in ["menu:home", "menu:refresh"]:
        await start_command(update, context)
        return ConversationHandler.END

    elif data == "menu:list_keys":
        await show_client_list(update, context)

    elif data.startswith("client_detail:"):
        client_uuid = data.split(":")[1]
        await show_client_detail(update, context, client_uuid)

    elif data.startswith("toggle:"):
        client_uuid = data.split(":")[1]
        await toggle_client_status(update, context, client_uuid)

    elif data.startswith("edit_menu:"):
        client_uuid = data.split(":")[1]
        await show_edit_menu(update, context, client_uuid)

# ---------------------------------------------------------------------------
# ၆။ Key စာရင်းနှင့် စီမံခန့်ခွဲမှု (List, Disable, Enable & Edit)
# ---------------------------------------------------------------------------
async def show_client_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    clients = await asyncio.to_thread(xui.get_all_clients, INBOUND_ID)
    
    if not clients:
        await query.message.edit_text("⚠️ <b>လက်ရှိ Inbound ထဲတွင် Key တစ်ခုမှ မရှိသေးပါ။</b>", parse_mode="HTML", reply_markup=get_back_button())
        return

    buttons = []
    for c in clients:
        status_emoji = "🟢" if c.get("enable", False) else "🔴"
        expiry_time = c.get("expiryTime", 0)
        is_expired = expiry_time > 0 and expiry_time < int(time.time() * 1000)
        if is_expired: status_emoji = "⚠️(ရက်လွန်)"
        
        btn_text = f"{status_emoji} {c.get('email', 'Unnamed')} | Flow: {c.get('flow', 'None') or 'Default'}"
        buttons.append([InlineKeyboardButton(btn_text, callback_data=f"client_detail:{c.get('id')}")])

    buttons.append([InlineKeyboardButton("🏠 ပင်မစာမျက်နှာသို့ ပြန်သွားမည်", callback_data="menu:home")])
    
    await query.message.edit_text(
        "📋 <b>စီမံလိုသော Key အား အောက်ပါစာရင်းမှ ရွေးချယ်ပါ -</b>\n\n<i>(🟢 ဖွင့်ထားသည် | 🔴 ပိတ်ထားသည် | ⚠️ ရက်လွန်)</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def show_client_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, client_uuid: str):
    query = update.callback_query
    client = await asyncio.to_thread(xui.get_client_by_uuid, INBOUND_ID, client_uuid)
    if not client:
        await query.message.edit_text("❌ Key အချက်အလက် ရှာမတွေ့ပါ။", reply_markup=get_back_button())
        return

    email = client.get("email", "Unnamed")
    flow = client.get("flow", "") or "Default"
    enable = client.get("enable", False)
    expiry_time = client.get("expiryTime", 0)

    if expiry_time == 0:
        exp_str = "အကန့်အသတ်မရှိ (Unlimited) ♾️"
    else:
        days_left = int((expiry_time - int(time.time() * 1000)) / (1000 * 86400))
        exp_str = f"{time.strftime('%Y-%m-%d', time.localtime(expiry_time/1000))} ({days_left} ရက် လိုသေးသည်)" if days_left >= 0 else f"⚠️ ရက်လွန်သွားပါပြီ ({abs(days_left)} ရက်လွန်)"

    status_str = "🟢 အသုံးပြုခွင့် ဖွင့်ထားသည် (Enabled)" if enable else "🔴 ပိတ်ထားသည် (Disabled)"
    vless_link = xui.build_vless_link(INBOUND_ID, client_uuid, email, flow)

    toggle_btn_text = "🔴 ယာယီ ပိတ်မည် (Disable)" if enable else "🟢 ပြန်လည် ဖွင့်မည် (Enable)"
    
    buttons = [
        [InlineKeyboardButton(toggle_btn_text, callback_data=f"toggle:{client_uuid}")],
        [InlineKeyboardButton("✏️ အမည်/သက်တမ်း ပြင်ဆင်မည် (Edit)", callback_data=f"edit_menu:{client_uuid}")],
        [InlineKeyboardButton("📋 Key စာရင်းသို့ ပြန်သွားမည်", callback_data="menu:list_keys")],
        [InlineKeyboardButton("🏠 ပင်မစာမျက်နှာသို့", callback_data="menu:home")]
    ]

    msg = (
        f"👤 <b>Client အချက်အလက် စီမံရန်</b>\n\n"
        f"🔹 <b>အမည်:</b> <code>{email}</code>\n"
        f"🔹 <b>အခြေအနေ:</b> {status_str}\n"
        f"⏳ <b>သက်တမ်း:</b> <code>{exp_str}</code>\n"
        f"⚡ <b>Flow:</b> <code>{flow}</code>\n\n"
        f"📋 <b>VLESS Link:</b>\n<code>{vless_link}</code>"
    )
    await query.message.edit_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons))

async def toggle_client_status(update: Update, context: ContextTypes.DEFAULT_TYPE, client_uuid: str):
    query = update.callback_query
    client = await asyncio.to_thread(xui.get_client_by_uuid, INBOUND_ID, client_uuid)
    if not client:
        return
    
    client["enable"] = not client.get("enable", False)
    success, msg = await asyncio.to_thread(xui.update_client, INBOUND_ID, client_uuid, client)
    
    await query.answer(msg)
    await show_client_detail(update, context, client_uuid)

# ---------------------------------------------------------------------------
# ၇။ Add Key Step-by-Step (100% Button Driven)
# ---------------------------------------------------------------------------
async def addkey_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    
    buttons = [[InlineKeyboardButton("❌ လုပ်ဆောင်ချက် ဖျက်သိမ်းမည်", callback_data="menu:home")]]
    await query.message.edit_text(
        "➕ <b>Key အသစ် ဆောက်လုပ်ခြင်း</b>\n\n"
        "အဆင့် (၁/၃) : Client အတွက် <b>အမည် (Name / Remarks)</b> ရိုက်ထည့်ပေးပါ 👇\n\n<i>(ဥပမာ - MyPhone, MgMg_Laptop)</i>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADD_NAME

async def addkey_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    client_name = update.message.text.strip().replace(" ", "_")
    context.user_data["add_name"] = client_name
    
    buttons = [
        [InlineKeyboardButton("♾️ အကန့်အသတ်မရှိ (Unlimited)", callback_data="add_exp:0")],
        [InlineKeyboardButton("၇ ရက်", callback_data="add_exp:7"), InlineKeyboardButton("၃၀ ရက်", callback_data="add_exp:30")],
        [InlineKeyboardButton("၆၀ ရက်", callback_data="add_exp:60"), InlineKeyboardButton("၉၀ ရက်", callback_data="add_exp:90")],
        [InlineKeyboardButton("❌ ဖျက်သိမ်းမည်", callback_data="menu:home")]
    ]
    
    await update.message.reply_text(
        f"✅ အမည် <b>{client_name}</b> ကို မှတ်သားပြီးပါပြီ။\n\n"
        "⏳ <b>အဆင့် (၂/၃) : သက်တမ်း ရွေးချယ်ပါ</b>\n\n"
        "အောက်ပါ ခလုတ်များမှ သက်တမ်း ရက်အရေအတွက် ရွေးချယ်ပါ သို့မဟုတ် ကိုယ်တိုင် ဂဏန်းရိုက်ထည့်ပါ 👇",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADD_EXPIRY

async def addkey_receive_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        query = update.callback_query
        await query.answer()
        expiry_days = int(query.data.split(":")[1])
        target_msg = query.message
    else:
        try:
            expiry_days = int(update.message.text.strip())
        except ValueError:
            await update.message.reply_text("⚠️ ကျေးဇူးပြု၍ ဂဏန်းကိန်းဂဏန်းသာ ရိုက်ထည့်ပါ သို့မဟုတ် ခလုတ်ကို နှိပ်ပါ။")
            return ADD_EXPIRY
        target_msg = update.message

    context.user_data["add_expiry"] = expiry_days
    expiry_str = f"{expiry_days} ရက်" if expiry_days > 0 else "အကန့်အသတ်မရှိ (Unlimited) ♾️"

    buttons = [
        [InlineKeyboardButton("⚡ xtls-rprx-vision (Default - အကြံပြုသည်)", callback_data="add_flow:xtls-rprx-vision")],
        [InlineKeyboardButton("⚪ Flow မသုံးပါ (None / Empty)", callback_data="add_flow:")],
        [InlineKeyboardButton("❌ ဖျက်သိမ်းမည်", callback_data="menu:home")]
    ]
    
    await target_msg.reply_text(
        f"✅ သက်တမ်း <b>{expiry_str}</b> သတ်မှတ်ပြီးပါပြီ။\n\n"
        "⚡ <b>အဆင့် (၃/၃) : Traffic Flow ရွေးချယ်ပါ 👇</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ADD_FLOW

async def addkey_receive_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    flow = query.data.split(":")[1]

    name = context.user_data.get("add_name", "User")
    expiry = context.user_data.get("add_expiry", 0)

    await query.message.edit_text("⏳ <b>Key ဆောက်လုပ်နေပါသည်...</b> ခဏစောင့်ပေးပါ ခင်ဗျာ။ ⚙️", parse_mode="HTML")

    success, uuid_or_err, vless_link = await asyncio.to_thread(xui.add_client, INBOUND_ID, name, expiry, flow)

    if not success:
        await query.message.edit_text(f"❌ <b>Key ထုတ်ယူခြင်း မအောင်မြင်ပါ</b>\n\n{uuid_or_err}", parse_mode="HTML", reply_markup=get_back_button())
        return ConversationHandler.END

    expiry_str = f"{expiry} ရက်" if expiry > 0 else "အကန့်အသတ်မရှိ (Unlimited) ♾️"
    success_msg = (
        "✅ <b>VLESS + Reality Key ထုတ်ယူခြင်း အောင်မြင်ပါသည်!</b> 🚀\n\n"
        f"👤 <b>အမည်:</b> <code>{name}</code>\n"
        f"⏳ <b>သက်တမ်း:</b> <code>{expiry_str}</code>\n"
        f"⚡ <b>Flow:</b> <code>{flow or 'None'}</code>\n\n"
        f"📋 <b>VLESS Link (တစ်ချက်နှိပ်၍ Copy ကူးပါ) -</b>\n\n"
        f"<code>{vless_link}</code>"
    )
    await query.message.edit_text(success_msg, parse_mode="HTML", reply_markup=get_back_button())
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# ၈။ Edit Key Handlers (100% Button Driven)
# ---------------------------------------------------------------------------
async def show_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, client_uuid: str):
    query = update.callback_query
    context.user_data["edit_uuid"] = client_uuid
    client = await asyncio.to_thread(xui.get_client_by_uuid, INBOUND_ID, client_uuid)
    
    buttons = [
        [InlineKeyboardButton("⏳ သက်တမ်း ရက်တိုးရန် / ပြင်ဆင်ရန်", callback_data=f"edit_act:expiry:{client_uuid}")],
        [InlineKeyboardButton("👤 အမည် (Name/Remarks) ပြောင်းရန်", callback_data=f"edit_act:name:{client_uuid}")],
        [InlineKeyboardButton("⚡ Flow (xtls-rprx-vision) ပြောင်းရန်", callback_data=f"edit_act:flow:{client_uuid}")],
        [InlineKeyboardButton("🔙 နောက်သို့ ပြန်သွားမည်", callback_data=f"client_detail:{client_uuid}")],
        [InlineKeyboardButton("🏠 ပင်မစာမျက်နှာသို့", callback_data="menu:home")]
    ]
    await query.message.edit_text(
        f"✏️ <b>{client.get('email')}</b> အတွက် ပြင်ဆင်လိုသည့် အပိုင်းကို ရွေးချယ်ပါ 👇",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
    )

async def edit_action_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action, client_uuid = parts[1], parts[2]
    context.user_data["edit_uuid"] = client_uuid
    client = await asyncio.to_thread(xui.get_client_by_uuid, INBOUND_ID, client_uuid)

    if action == "expiry":
        buttons = [
            [InlineKeyboardButton("➕ ၇ ရက် တိုးမည်", callback_data="edit_exp_add:7"), InlineKeyboardButton("➕ ၃၀ ရက် တိုးမည်", callback_data="edit_exp_add:30")],
            [InlineKeyboardButton("♾️ အကန့်အသတ်မရှိ ပြောင်းမည် (Unlimited)", callback_data="edit_exp_set:0")],
            [InlineKeyboardButton("🔙 နောက်သို့", callback_data=f"edit_menu:{client_uuid}"), InlineKeyboardButton("🏠 ပင်မသို့", callback_data="menu:home")]
        ]
        await query.message.edit_text(
            f"⏳ <b>{client.get('email')}</b> အတွက် ရက်တိုးရန် ခလုတ်နှိပ်ပါ၊ သို့မဟုတ် သတ်မှတ်လိုသည့် ရက်ပေါင်းအသစ်ကို ဂဏန်းဖြင့် ရိုက်ထည့်ပါ 👇",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return EDIT_EXPIRY_INPUT

    elif action == "name":
        buttons = [[InlineKeyboardButton("🔙 နောက်သို့", callback_data=f"edit_menu:{client_uuid}"), InlineKeyboardButton("🏠 ပင်မသို့", callback_data="menu:home")]]
        await query.message.edit_text(
            f"👤 <b>{client.get('email')}</b> အတွက် အမည်အသစ် (New Name) ရိုက်ထည့်ပေးပါ 👇",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(buttons)
        )
        return EDIT_NAME_INPUT

    elif action == "flow":
        new_flow = "xtls-rprx-vision" if not client.get("flow") else ""
        client["flow"] = new_flow
        success, msg = await asyncio.to_thread(xui.update_client, INBOUND_ID, client_uuid, client)
        await query.answer(msg)
        await show_client_detail(update, context, client_uuid)
        return ConversationHandler.END

async def edit_receive_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    client_uuid = context.user_data.get("edit_uuid")
    client = await asyncio.to_thread(xui.get_client_by_uuid, INBOUND_ID, client_uuid)
    
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
    success, msg = await asyncio.to_thread(xui.update_client, INBOUND_ID, client_uuid, client)
    
    await target_msg.reply_text(f"✅ သက်တမ်း ပြင်ဆင်ပြီးပါပြီ။ {msg}", reply_markup=get_back_button())
    return ConversationHandler.END

async def edit_receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    client_uuid = context.user_data.get("edit_uuid")
    client = await asyncio.to_thread(xui.get_client_by_uuid, INBOUND_ID, client_uuid)
    
    new_name = update.message.text.strip().replace(" ", "_")
    client["email"] = new_name
    success, msg = await asyncio.to_thread(xui.update_client, INBOUND_ID, client_uuid, client)
    
    await update.message.reply_text(f"✅ အမည်အသစ် <b>{new_name}</b> သို့ ပြောင်းလဲပြီးပါပြီ။", parse_mode="HTML", reply_markup=get_back_button())
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# ၉။ Main Application Setup
# ---------------------------------------------------------------------------
def main():
    logger.info("🚀 Starting Button-Driven 3x-ui Bot...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(addkey_start, pattern="^menu:add_key$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addkey_receive_name)],
            ADD_EXPIRY: [
                CallbackQueryHandler(addkey_receive_expiry, pattern="^add_exp:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, addkey_receive_expiry)
            ],
            ADD_FLOW: [CallbackQueryHandler(addkey_receive_flow, pattern="^add_flow:")]
        },
        fallbacks=[CallbackQueryHandler(menu_router, pattern="^menu:home$")],
        allow_reentry=True
    )

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_action_router, pattern="^edit_act:")],
        states={
            EDIT_EXPIRY_INPUT: [
                CallbackQueryHandler(edit_receive_expiry, pattern="^edit_exp_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_expiry)
            ],
            EDIT_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_receive_name)]
        },
        fallbacks=[CallbackQueryHandler(menu_router, pattern="^menu:home$")],
        allow_reentry=True
    )

    app.add_handler(add_conv)
    app.add_handler(edit_conv)
    app.add_handler(CommandHandler(["start", "help", "menu"], start_command))
    app.add_handler(CallbackQueryHandler(menu_router))

    logger.info("✅ Bot is online and listening for button clicks...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
