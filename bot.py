import os
import json
import uuid
import time
import logging
import asyncio
from urllib.parse import urlparse, quote
import requests
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# ၁။ Logging System သတ်မှတ်ခြင်း (Railway Logs တွင် အလွယ်တကူ စောင့်ကြည့်ရန်)
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ၂။ Environment Variables & Default Credentials
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8993287223:AAHnmFVfJTHkTURQNsFZeZJtRk1REfB5NEg")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", 1824573670))

XUI_PANEL_URL = os.getenv("XUI_PANEL_URL", "http://167.172.73.82:53073/KycDj1Uzisw3vpu").rstrip("/")
XUI_USERNAME = os.getenv("XUI_USERNAME", "auzbMTwGgX")
XUI_PASSWORD = os.getenv("XUI_PASSWORD", "skA9eqRFHv")
INBOUND_ID = int(os.getenv("INBOUND_ID", 1))

# Panel URL မှ VPS IP ကို အလိုအလျောက် ခွဲထုတ်ခြင်း
parsed_url = urlparse(XUI_PANEL_URL)
VPS_IP = parsed_url.hostname or "167.172.73.82"

# Conversation Step States
ASK_NAME, ASK_EXPIRY, ASK_FLOW = range(3)

# ---------------------------------------------------------------------------
# ၃။ 3x-ui API Client (Session Management & Dynamic Reality Link Creation)
# ---------------------------------------------------------------------------
class XUIClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.is_logged_in = False

    def login(self) -> bool:
        """3x-ui Panel သို့ Login ဝင်ရောက်ပြီး Session Cookie သိမ်းဆည်းသည်။"""
        url = f"{self.base_url}/login"
        payload = {"username": self.username, "password": self.password}
        try:
            res = self.session.post(url, data=payload, timeout=10)
            data = res.json()
            if data.get("success", False):
                self.is_logged_in = True
                logger.info("✅ 3x-ui Panel သို့ အောင်မြင်စွာ Login ဝင်ရောက်ပါပြီ။")
                return True
            else:
                logger.warning(f"❌ Login မအောင်မြင်ပါ: {data.get('msg', 'Invalid credentials')}")
                return False
        except Exception as e:
            logger.error(f"❌ X-UI Login Connection Error: {e}")
            return False

    def _request(self, method: str, endpoint: str, **kwargs) -> dict:
        """Session သက်တမ်းကုန်သွားပါက အလိုအလျောက် Re-login ပြန်လုပ်ပေးသည့် Internal Wrapper"""
        if not self.is_logged_in:
            if not self.login():
                return {"success": False, "msg": "Authentication failed."}

        url = f"{self.base_url}{endpoint}"
        try:
            res = self.session.request(method, url, timeout=10, **kwargs)
            # Session ကုန်သွားပြီး 401 သို့မဟုတ် Login page သို့ redirect ဖြစ်ပါက Re-authenticate လုပ်သည်
            if res.status_code == 401 or "/login" in res.url:
                logger.info("🔄 Session သက်တမ်းကုန်သွားပါသဖြင့် Re-authentication ပြုလုပ်နေသည်...")
                if self.login():
                    res = self.session.request(method, url, timeout=10, **kwargs)
                else:
                    return {"success": False, "msg": "Re-authentication failed."}
            return res.json()
        except Exception as e:
            logger.error(f"❌ API Request Error ({endpoint}): {e}")
            return {"success": False, "msg": str(e)}

    def get_inbound(self, inbound_id: int) -> dict:
        """Inbound ID အရ Reality Settings များကို လှမ်းဆွဲယူသည်။"""
        data = self._request("GET", f"/panel/api/inbounds/get/{inbound_id}")
        if data.get("success"):
            return data.get("obj", {})
        return {}

    def add_client(self, inbound_id: int, email: str, expiry_days: int, flow: str) -> tuple[bool, str, str]:
        """VLESS Reality Client အသစ်ထည့်ပြီး vless:// Link ကို အတိအကျ ထုတ်ပေးသည်။"""
        client_uuid = str(uuid.uuid4())
        
        # 3x-ui အတွက် Expiry Time ကို Milliseconds Timestamp ဖြင့် တွက်ချက်ခြင်း (0 = Unlimited)
        expiry_time = 0 if expiry_days <= 0 else int((time.time() + (expiry_days * 86400)) * 1000)

        client_data = {
            "id": client_uuid,
            "flow": flow,
            "email": email,
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": expiry_time,
            "enable": True,
            "tgId": "",
            "subId": ""
        }

        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client_data]})
        }

        # Client အား Inbound ထဲသို့ ပေါင်းထည့်ခြင်း
        res_data = self._request("POST", "/panel/api/inbounds/addClient", json=payload)
        if not res_data.get("success"):
            return False, f"Key ထည့်သွင်းခြင်း မအောင်မြင်ပါ - {res_data.get('msg', 'Unknown Error')}", ""

        # Dynamic vless:// Reality Link တည်ဆောက်ရန် Inbound Info ပြန်ဆွဲခြင်း
        inbound = self.get_inbound(inbound_id)
        if not inbound:
            fallback_link = f"vless://{client_uuid}@{VPS_IP}:443?security=reality#{quote(email)}"
            return True, client_uuid, fallback_link

        try:
            port = inbound.get("port", 443)
            stream_settings = inbound.get("streamSettings", {})
            if isinstance(stream_settings, str):
                stream_settings = json.loads(stream_settings)

            network = stream_settings.get("network", "tcp")
            reality_settings = stream_settings.get("realitySettings", {})
            settings_obj = reality_settings.get("settings", {})

            # Parameters များကို အန္တရာယ်ကင်းစွာ ဆွဲယူခြင်း
            pbk = settings_obj.get("publicKey") or reality_settings.get("publicKey", "")
            fp = settings_obj.get("fingerprint") or reality_settings.get("fingerprint", "chrome")

            server_names = settings_obj.get("serverNames") or reality_settings.get("serverNames") or ["www.amazon.com"]
            sni = server_names[0] if isinstance(server_names, list) and server_names else str(server_names).split(",")[0]

            short_ids = settings_obj.get("shortIds") or reality_settings.get("shortIds") or [""]
            sid = short_ids[0] if isinstance(short_ids, list) and short_ids else str(short_ids).split(",")[0]

            # Standard VLESS Link တည်ဆောက်ခြင်း
            query_params = f"type={network}&security=reality&pbk={pbk}&fp={fp}&sni={sni}"
            if sid:
                query_params += f"&sid={sid}"
            if flow:
                query_params += f"&flow={flow}"

            vless_link = f"vless://{client_uuid}@{VPS_IP}:{port}?{query_params}#{quote(email)}"
            return True, client_uuid, vless_link
        except Exception as e:
            logger.error(f"❌ Link Construction Error: {e}")
            fallback_link = f"vless://{client_uuid}@{VPS_IP}:{inbound.get('port', 443)}?security=reality#{quote(email)}"
            return True, client_uuid, fallback_link

xui = XUIClient(XUI_PANEL_URL, XUI_USERNAME, XUI_PASSWORD)

# ---------------------------------------------------------------------------
# ၄။ Security Middleware (Admin User ID သီးသန့် အသုံးပြုခွင့် ကန့်သတ်ချက်)
# ---------------------------------------------------------------------------
def admin_only(func):
    """Admin ID မှလွဲ၍ တခြားသူများ လုံးဝ အသုံးပြုခွင့် မရှိအောင် ကာကွယ်ပေးသည့် Decorator"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_TELEGRAM_ID:
            logger.warning(f"⚠️ ခွင့်ပြုချက်မရှိသူ အသုံးပြုရန် ကြိုးပမ်းမှု - User ID: {user_id}")
            unauth_msg = (
                "⚠️ <b>တောင်းပန်ပါတယ် ခင်ဗျာ။</b>\n\n"
                "ဤ Bot အား အသုံးပြုခွင့် မရှိပါ။ စနစ်ပိုင်ရှင် (Admin) သာလျှင် သီးသန့် အသုံးပြုနိုင်ပါသည်။ 🛡️"
            )
            if update.message:
                await update.message.reply_text(unauth_msg, parse_mode="HTML")
            return ConversationHandler.END
        return await func(update, context, *args, **kwargs)
    return wrapper

# ---------------------------------------------------------------------------
# ၅။ Telegram Handlers & Step-by-Step Conversation Flow
# ---------------------------------------------------------------------------
@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start နှင့် /help Command များအတွက် ကြိုဆိုနှုတ်ဆက်ခြင်း"""
    welcome_msg = (
        "🚀 <b>3x-ui Reality VPN Key Manager</b> မှ ကြိုဆိုပါတယ်။\n\n"
        "ဒီ Bot ကနေ VLESS + Reality Key များကို လွယ်ကူလျင်မြန်စွာ ထုတ်ယူ စီမံနိုင်ပါတယ်။\n\n"
        "🔹 <code>/addkey</code> - VLESS Key အသစ်တစ်ခု ထုတ်ရန်\n"
        "🔹 <code>/cancel</code> - လက်ရှိ လုပ်ဆောင်ချက်ကို ဖျက်သိမ်းရန်\n"
        "🔹 <code>/help</code> - အကူအညီနှင့် ညွှန်ကြားချက်များ ကြည့်ရန်"
    )
    await update.message.reply_text(welcome_msg, parse_mode="HTML")

@admin_only
async def addkey_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """အဆင့် (၁/၃): Client အမည် မေးမြန်းခြင်း"""
    context.user_data.clear()
    msg = (
        "🔑 <b>VLESS Key အသစ် ထုတ်ယူခြင်း</b>\n\n"
        "အဆင့် (၁/၃) : Client အတွက် <b>အမည် (Name / Remarks)</b> သတ်မှတ်ပေးပါ။\n\n"
        "<i>(ဥပမာ - MyPhone, MgMg_Laptop)</i>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")
    return ASK_NAME

@admin_only
async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """အမည်ကို မှတ်သားပြီး အဆင့် (၂/၃): သက်တမ်း မေးမြန်းခြင်း"""
    client_name = update.message.text.strip().replace(" ", "_")
    context.user_data["name"] = client_name
    
    msg = (
        f"✅ အမည် <b>{client_name}</b> ကို မှတ်သားပြီးပါပြီ။\n\n"
        "⏳ <b>အဆင့် (၂/၃) : သက်တမ်း (Expiry Date) သတ်မှတ်ခြင်း</b>\n\n"
        "အသုံးပြုခွင့် သက်တမ်းကို <b>ရက်ပေါင်း (Days)</b> ဖြင့် ထည့်သွင်းပေးပါ။ <i>(ဥပမာ - 30)</i>\n\n"
        "💡 <i>သက်တမ်းအကန့်အသတ်မရှိ (Unlimited) ထားလိုပါက <code>0</code> ဟု ရိုက်ထည့်ပါ သို့မဟုတ် <code>/skip</code> ကို နှိပ်ပါ။</i>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")
    return ASK_EXPIRY

@admin_only
async def receive_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """သက်တမ်းကို မှတ်သားပြီး အဆင့် (၃/၃): Flow မေးမြန်းခြင်း"""
    text = update.message.text.strip().lower()
    
    if text == "/skip" or text == "" or text == "0":
        expiry_days = 0
    else:
        try:
            expiry_days = int(text)
            if expiry_days < 0:
                expiry_days = 0
        except ValueError:
            await update.message.reply_text("⚠️ ကျေးဇူးပြု၍ ကိန်းဂဏန်းသီးသန့်သာ ထည့်သွင်းပေးပါ (ဥပမာ - <code>30</code>) သို့မဟုတ် <code>/skip</code> ကို နှိပ်ပါ။", parse_mode="HTML")
            return ASK_EXPIRY

    context.user_data["expiry"] = expiry_days
    expiry_str = f"{expiry_days} ရက်" if expiry_days > 0 else "အကန့်အသတ်မရှိ (Unlimited) ♾️"

    msg = (
        f"✅ သက်တမ်း <b>{expiry_str}</b> သတ်မှတ်ပြီးပါပြီ။\n\n"
        "⚡ <b>အဆင့် (၃/၃) : Traffic Flow ရွေးချယ်ခြင်း</b>\n\n"
        "အသုံးပြုမည့် <b>Flow</b> ကို ထည့်သွင်းပါ။\n\n"
        "💡 <i>ပုံမှန် VLESS Reality အတွက် Default အတိုင်း ထားလိုပါက ဘာမှမထည့်ဘဲ <code>/skip</code> ကိုသာ နှိပ်ပေးပါ။ (<code>xtls-rprx-vision</code> ကို အလိုအလျောက် သတ်မှတ်ပေးပါမည်)</i>"
    )
    await update.message.reply_text(msg, parse_mode="HTML")
    return ASK_FLOW

@admin_only
async def receive_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Flow ကို မှတ်သားပြီး 3x-ui API သို့ ချိတ်ဆက်ကာ Key ထုတ်ပေးခြင်း"""
    text = update.message.text.strip()
    
    if text.lower() == "/skip" or text == "":
        flow = "xtls-rprx-vision"
    else:
        flow = text

    name = context.user_data.get("name", "User")
    expiry = context.user_data.get("expiry", 0)

    # Key ထုတ်နေစဉ် စောင့်ဆိုင်းရန် အသိပေးခြင်း
    await update.message.reply_text("⏳ <b>Key ထုတ်ယူနေပါသည်...</b> ခဏစောင့်ပေးပါ ခင်ဗျာ။ ⚙️", parse_mode="HTML")

    # API Request ကို Async Event Loop မပိတ်ဆို့စေရန် Thread ဖြင့် ခေါ်ယူခြင်း
    success, uuid_or_err, vless_link = await asyncio.to_thread(
        xui.add_client, INBOUND_ID, name, expiry, flow
    )

    if not success:
        await update.message.reply_text(f"❌ <b>Key ထုတ်ယူခြင်း မအောင်မြင်ပါ</b>\n\n{uuid_or_err}", parse_mode="HTML")
        return ConversationHandler.END

    expiry_str = f"{expiry} ရက်" if expiry > 0 else "အကန့်အသတ်မရှိ (Unlimited) ♾️"
    
    success_msg = (
        "✅ <b>VLESS + Reality Key ထုတ်ယူခြင်း အောင်မြင်ပါသည်!</b> 🚀\n\n"
        f"👤 <b>အမည်:</b> <code>{name}</code>\n"
        f"⏳ <b>သက်တမ်း:</b> <code>{expiry_str}</code>\n"
        f"⚡ <b>Flow:</b> <code>{flow}</code>\n"
        f"🛡️ <b>Inbound ID:</b> <code>{INBOUND_ID}</code>\n\n"
        "📋 <b>အောက်ပါ VLESS Link အား တစ်ချက်နှိပ်၍ Copy ကူးယူနိုင်ပါသည် -</b>\n\n"
        f"<code>{vless_link}</code>\n\n"
        "<i>(V2Ray, Hiddify, NekoBox သို့မဟုတ် သက်ဆိုင်ရာ VPN Client တွင် Paste ချ၍ ချက်ချင်း အသုံးပြုနိုင်ပါပြီ)</i> 🎉"
    )
    await update.message.reply_text(success_msg, parse_mode="HTML")
    return ConversationHandler.END

@admin_only
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """လုပ်ဆောင်ချက်ကို ရပ်စဲခြင်း"""
    await update.message.reply_text("❌ <b>လုပ်ဆောင်ချက်ကို ရပ်စဲလိုက်ပါပြီ။</b>\n\nKey အသစ် ပြန်ထုတ်လိုပါက <code>/addkey</code> ကို နှိပ်ပါ။", parse_mode="HTML")
    return ConversationHandler.END

# ---------------------------------------------------------------------------
# ၆။ Main Application Bootstrapper
# ---------------------------------------------------------------------------
def main():
    logger.info("🚀 3x-ui Telegram Bot အား စတင် Run နေပါပြီ...")
    
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("addkey", addkey_start)],
        states={
            ASK_NAME: [
                CommandHandler("cancel", cancel_command),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)
            ],
            ASK_EXPIRY: [
                CommandHandler("cancel", cancel_command),
                CommandHandler("skip", receive_expiry),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_expiry)
            ],
            ASK_FLOW: [
                CommandHandler("cancel", cancel_command),
                CommandHandler("skip", receive_flow),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_flow)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True
    )

    application.add_handler(conv_handler)
    application.add_handler(CommandHandler(["start", "help"], start_command))

    logger.info("✅ Bot is polling and ready for commands...")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
