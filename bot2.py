import os
import asyncio
import logging
import traceback
import requests
import re
import json
from datetime import datetime
from pymongo import MongoClient
from bson import ObjectId
from telethon import TelegramClient, events, Button, errors
from telethon.tl.functions.channels import GetParticipantRequest
from dotenv import load_dotenv

# ---------- ADDED FOR RENDER PORT BINDING ----------
from flask import Flask
from threading import Thread

load_dotenv()

# ---------- CONFIG ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "MyTelethon")
SOLD_CHANNEL = os.getenv("SOLD_CHANNEL", "@chfkfhjfod")
FORCE_CHANNELS_JSON = os.getenv("FORCE_CHANNELS", '[{"username":"@moppybots","link":"https://t.me/moppybots"},{"username":"@chfkfhjfod","link":"https://t.me/chfkfhjfod"}]')
FORCE_CHANNELS = json.loads(FORCE_CHANNELS_JSON)

# Payment API Key & UPI ID (from environment)
PAYMENT_API_KEY = os.getenv("PAYMENT_API_KEY", "")
UPI_ID = os.getenv("UPI_ID", "guptaits@fam")

os.makedirs(SESSIONS_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("bot2.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ---------- MONGODB ----------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["referral_bot"]

users_coll = db["users"]
accounts_coll = db["store_accounts"]
deposits_coll = db["store_deposits"]
settings_coll = db["store_settings"]
redeem_codes_coll = db["redeem_codes"]

accounts_coll.create_index("phone", unique=True)
accounts_coll.create_index("category")
redeem_codes_coll.create_index("code", unique=True)

# ---------- HELPERS ----------
def get_default_price():
    doc = settings_coll.find_one({"_id": "default_price"})
    return doc.get("value", 100.0) if doc else 100.0

def set_default_price(price):
    settings_coll.update_one({"_id": "default_price"}, {"$set": {"value": price}}, upsert=True)

def get_user_balance(user_id):
    user = users_coll.find_one({"user_id": user_id})
    return user.get("balance", 0.0) if user else 0.0

def update_user_balance(user_id, amount):
    result = users_coll.update_one({"user_id": user_id}, {"$inc": {"balance": amount}}, upsert=True)
    return result.modified_count > 0

def init_user(user_id):
    users_coll.update_one({"user_id": user_id}, {"$setOnInsert": {"balance": 0.0, "is_owner": user_id in ADMIN_IDS}}, upsert=True)

def get_stats():
    u_count = users_coll.count_documents({})
    a_count = accounts_coll.count_documents({"status": "available"})
    return u_count, a_count

def is_utr_used(utr):
    return deposits_coll.find_one({"utr": utr}) is not None

def mask_phone(phone):
    phone = str(phone)
    if len(phone) <= 6:
        return "*" * len(phone)
    return phone[:5] + "*" * (len(phone) - 8) + phone[-3:]

# ---------- KEYBOARDS ----------
def get_owner_keyboard():
    return [
        [Button.text("➕ Add Account", resize=True), Button.text("📢 Announcement")],
        [Button.text("💰 Add Money"), Button.text("📊 Stats")],
        [Button.text("🔍 Check Fund"), Button.text("🗑️ Delete Fund")],
        [Button.text("🏷️ Change Price")],
        [Button.text("🗑️ Remove Account")],
        [Button.text("✏️ Edit Account")]
    ]

def get_user_keyboard():
    return [
        [Button.text("👤 My Account", resize=True), Button.text("🛒 Buy ID")],
        [Button.text("💳 Deposit")],
        [Button.text("🆘 Support")]
    ]

def cancel_button():
    return [Button.text("❌ Cancel Operation", resize=True)]

# ---------- GLOBALS FOR BOT ----------
bot = None
user_states = {}
active_otp_clients = {}

# ---------- FORCE SUBSCRIPTION ----------
async def check_force_sub(user_id):
    missing = []
    for ch in FORCE_CHANNELS:
        try:
            await bot(GetParticipantRequest(ch["username"], user_id))
        except:
            missing.append(ch)
    return missing

# ---------- REDEEM LOGIC ----------
async def process_redeem_code(user_id, code):
    doc = redeem_codes_coll.find_one({"code": code, "used": False})
    if not doc:
        return False, "Invalid or already used code."

    result = redeem_codes_coll.update_one({"_id": doc["_id"]}, {"$set": {"used": True, "used_at": datetime.now(), "used_by": user_id}})
    if result.modified_count == 0:
        return False, "Code already used (concurrent)."

    service_tag = doc.get("service_tag", "")
    query = {"status": "available"}
    if service_tag:
        query["category"] = service_tag
    account = accounts_coll.find_one(query)

    if not account:
        account = accounts_coll.find_one({"status": "available"})
        if not account:
            return False, "No accounts available at the moment. Please contact admin."

    accounts_coll.update_one({"_id": account["_id"]}, {"$set": {"status": "sold", "sold_to": user_id, "sold_at": datetime.now()}})

    await bot.send_message(
        SOLD_CHANNEL,
        f"""
<b>🎁 FREE REDEMPTION</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Product:</b> Telegram Account
📱 <b>Number:</b> <code>{mask_phone(account['phone'])}</code>
🌎 <b>Region:</b> <code>{account['country']}</code>
🏷️ <b>Category:</b> <code>{account.get('category', 'N/A')}</code>
🆔 <b>Redeemed by:</b> <code>{user_id}</code>
🧾 <b>Code:</b> <code>{code}</code>
━━━━━━━━━━━━━━━━━━━━
""",
        buttons=[Button.url("🤖 Buy Accounts Here", "http://t.me/Moppytgxbot")],
        parse_mode="html"
    )

    asyncio.create_task(start_otp_forwarding(account['phone'], user_id))
    return True, account

# ---------- OTP FORWARDING (FIXED) ----------
async def start_otp_forwarding(phone, user_id):
    if phone in active_otp_clients:
        return
    session_file = os.path.join(SESSIONS_DIR, phone.replace("+", ""))
    client = TelegramClient(session_file, API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        logger.warning(f"Session not authorized for {phone}")
        return
    active_otp_clients[phone] = client
    logger.info(f"OTP forwarding started for {phone}")

    @client.on(events.NewMessage(from_users=777000))
    async def otp_handler(event):
        text = event.raw_text
        
        # Directly extract OTP from "Login code: XXXXX" format
        otp_match = re.search(r'Login code:\s*(\d+)', text)
        
        if otp_match:
            otp = otp_match.group(1)
        else:
            # Fallback: extract any 5-6 digit number
            otp_match = re.search(r'\b(\d{5,6})\b', text)
            otp = otp_match.group(1) if otp_match else "N/A"
        
        account = accounts_coll.find_one({"phone": phone})
        password = account.get("password", "N/A") if account else "N/A"
        otp_msg = (
            "<b>📩 NEW OTP RECEIVED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Account:</b> <code>{phone}</code>\n"
            f"🔐 <b>OTP:</b> <code>{otp}</code>\n"
            f"🔑 <b>PASS:</b> <code>{password}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await bot.send_message(user_id, otp_msg, parse_mode="html")

    try:
        while True:
            await asyncio.sleep(60)  # Keep alive forever
    finally:
        await client.disconnect()
        active_otp_clients.pop(phone, None)

# ---------- MAIN FUNCTION ----------
async def main():
    global bot
    bot = TelegramClient("bot2_session", API_ID, API_HASH)
    await bot.start(bot_token=BOT_TOKEN)
    logger.info("Bot2 started successfully.")

    # ---------- START HANDLER (with redeem) ----------
    @bot.on(events.NewMessage(pattern='/start(?:\\s+(.*))?'))
    async def start_handler(event):
        user_id = event.sender_id
        init_user(user_id)

        args = event.pattern_match.group(1)
        if args and args.startswith("redeem_"):
            code = args.replace("redeem_", "")
            success, result = await process_redeem_code(user_id, code)
            if success:
                await event.respond(
                    f"🎉 <b>Redemption successful!</b>\n"
                    f"📱 Account: <code>{mask_phone(result['phone'])}</code>\n"
                    f"🌍 Country: <code>{result['country']}</code>\n"
                    f"🏷️ Category: <code>{result.get('category', 'N/A')}</code>\n"
                    f"🔑 Password: <code>{result.get('password', 'N/A')}</code>\n\n"
                    f"<i>OTP forwarding is active. You'll receive codes here.</i>",
                    parse_mode="html",
                    buttons=get_user_keyboard() if user_id not in ADMIN_IDS else get_owner_keyboard()
                )
            else:
                await event.respond(f"❌ {result}", buttons=get_user_keyboard())
            return

        # normal start flow
        missing = await check_force_sub(user_id)
        if missing:
            buttons = []
            for ch in missing:
                name = ch["username"].replace("@", "")
                buttons.append([Button.url(f"📢 {name}", ch["link"])])
            buttons.append([Button.inline("✅ Verify Join", b"verify_sub")])
            await event.respond(
                """
<emoji id="5040042498634810056">❌</emoji> <b>You must subscribe to all official channels to use this bot.</b>
━━━━━━━━━━━━━━━━━━
📢 <b>Required Channels</b>

Join all channels and press Verify.
━━━━━━━━━━━━━━━━━━
""",
                buttons=buttons,
                parse_mode="html"
            )
            return

        welcome_msg = (
            "<b>👑 PREMIUM TELEGRAM ACCOUNT STORE</b>\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "⚡ Instant Account Delivery\n"
            "🔐 Secure & Verified Accounts\n"
            "💰 Fast & Easy Payments\n"
            "📩 Automatic OTP Forwarding\n"
            "🌍 Multiple Countries Available\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
            "🚀 Welcome to the ultimate marketplace for premium Telegram accounts.\n\n"
            "<i>Select an option below to get started.</i>"
        )
        if user_id in ADMIN_IDS:
            await event.respond(f"<b>👋 Welcome Boss!</b>\nAdmin Dashboard is ready.",
                                buttons=get_owner_keyboard(), parse_mode='html')
        else:
            await event.respond(welcome_msg, buttons=get_user_keyboard(), parse_mode='html')

    # ---------- MESSAGE HANDLER ----------
    @bot.on(events.NewMessage)
    async def message_handler(event):
        user_id = event.sender_id
        text = event.raw_text
        state_info = user_states.get(user_id, {})
        state = state_info.get('state')

        if text in ["/cancel", "❌ Cancel Operation"]:
            user_states.pop(user_id, None)
            reply = "<b>⚠️ Operation Cancelled.</b>"
            kb = get_owner_keyboard() if user_id in ADMIN_IDS else get_user_keyboard()
            await event.respond(reply, buttons=kb, parse_mode='html')
            return

        # ----- ADMIN FEATURES -----
        if user_id in ADMIN_IDS:
            if text == "➕ Add Account":
                user_states[user_id] = {'state': 'AWAITING_PHONE'}
                await event.respond("<b>📱 Adding New Account</b>\n━\nPlease send the phone number with country code\nExample: <code>+919876543210</code>",
                                    buttons=cancel_button(), parse_mode='html')
                return
            elif text == "📢 Announcement":
                user_states[user_id] = {'state': 'AWAITING_ANNOUNCEMENT', 'messages': []}
                await event.respond("<b>📢 Broadcast Mode</b>\n━\nSend any message (Text, Photo, Video, File).\n\n✅ Press <b>/done</b> when finished.\n❌ Press <b>/cancel</b> to abort.",
                                    buttons=cancel_button(), parse_mode='html')
                return
            elif text == "💰 Add Money":
                user_states[user_id] = {'state': 'AWAITING_ADD_MONEY_CHAT_ID'}
                await event.respond("<b>💸 Add Funds</b>\n━\nPlease enter the <b>User ID</b> you want to credit:", buttons=cancel_button(), parse_mode='html')
                return
            elif text == "📊 Stats":
                u_count, a_count = get_stats()
                await event.respond(
                    f"<b>📊 Global Statistics</b>\n━━━━━━━━━━━━━━━━━━━━\n👥 Total Users: <code>{u_count}</code>\n📦 Available IDs: <code>{a_count}</code>\n━━━━━━━━━━━━━━━━━━━━",
                    parse_mode='html'
                )
                return
            elif text == "🔍 Check Fund":
                user_states[user_id] = {'state': 'AWAITING_CHECK_FUND_CHAT_ID'}
                await event.respond("<b>🔍 Balance Check</b>\n━\nEnter the <b>User ID</b> to view their balance:", buttons=cancel_button(), parse_mode='html')
                return
            elif text == "🗑️ Delete Fund":
                user_states[user_id] = {'state': 'AWAITING_DELETE_FUND_CHAT_ID'}
                await event.respond("<b>🗑️ Remove Funds</b>\n━\nEnter the <b>User ID</b> to deduct money from:", buttons=cancel_button(), parse_mode='html')
                return
            elif text == "🏷️ Change Price":
                curr = get_default_price()
                user_states[user_id] = {'state': 'AWAITING_NEW_PRICE'}
                await event.respond(f"<b>🏷️ Price Configuration</b>\n━\nCurrent Default: <code>{curr} INR</code>\n\nEnter the new price:",
                                    buttons=cancel_button(), parse_mode='html')
                return
            elif text == "✏️ Edit Account":
                user_states[user_id] = {"state": "AWAITING_EDIT_PHONE"}
                await event.respond("📱 Account number bhejo\n\nExample:\n+8801973766586")
                return
            elif text == "🗑️ Remove Account":
                user_states[user_id] = {"state": "AWAITING_REMOVE_PHONE"}
                await event.respond("📱 Account number bhejo\n\nExample:\n+8801973766586")
                return

            # ---------- ADMIN STATES ----------
            if state == 'AWAITING_PHONE':
                phone = text.strip().replace(" ", "")
                session_file = os.path.join(SESSIONS_DIR, phone.replace("+", ""))
                client = TelegramClient(session_file, API_ID, API_HASH)
                await client.connect()
                try:
                    if not await client.is_user_authorized():
                        sent_code = await client.send_code_request(phone)
                        user_states[user_id] = {
                            'state': 'AWAITING_OTP',
                            'phone': phone,
                            'client': client,
                            'phone_code_hash': sent_code.phone_code_hash
                        }
                        await event.respond(f"<b>📩 OTP Sent!</b>\nEnter the code received on <code>{phone}</code>:", buttons=cancel_button(), parse_mode='html')
                    else:
                        user_states[user_id] = {
                            "state": "AWAITING_COUNTRY",
                            "phone": phone,
                            "client": client
                        }
                        await event.respond("🌍 Country bhejo\n\nExample:\nIndia\nPakistan\nBangladesh")
                except Exception as e:
                    await event.respond(f"<b>❌ Error:</b>\n<code>{str(e)}</code>", buttons=get_owner_keyboard(), parse_mode='html')
                    user_states.pop(user_id)
                return

            elif state == 'AWAITING_OTP':
                otp = text.strip()
                try:
                    await state_info['client'].sign_in(state_info['phone'], otp, phone_code_hash=state_info['phone_code_hash'])
                    user_states[user_id] = {
                        "state": "AWAITING_COUNTRY",
                        "phone": state_info["phone"],
                        "client": state_info["client"]
                    }
                    await event.respond("🌍 Country bhejo\n\nExample:\nIndia\nPakistan\nBangladesh")
                    return
                except errors.SessionPasswordNeededError:
                    user_states[user_id]['state'] = 'AWAITING_2FA'
                    await event.respond("<b>🔐 2FA Detected</b>\nPlease enter the Cloud Password:", buttons=cancel_button(), parse_mode='html')
                except Exception as e:
                    await event.respond(f"<b>❌ Error:</b>\n<code>{str(e)}</code>", buttons=get_owner_keyboard(), parse_mode='html')
                    user_states.pop(user_id)
                return

            elif state == 'AWAITING_2FA':
                try:
                    pwd = text.strip()
                    await state_info['client'].sign_in(password=pwd)
                    user_states[user_id] = {
                        "state": "AWAITING_COUNTRY",
                        "phone": state_info["phone"],
                        "client": state_info["client"],
                        "password": pwd
                    }
                    await event.respond("🌍 Country bhejo\n\nExample:\nIndia\nPakistan\nBangladesh")
                    return
                except Exception as e:
                    await event.respond(f"<b>❌ 2FA Error:</b>\n<code>{str(e)}</code>", buttons=get_owner_keyboard(), parse_mode='html')
                    user_states.pop(user_id)
                return

            elif state == 'AWAITING_COUNTRY':
                country = text.strip()
                user_states[user_id] = {
                    "state": "AWAITING_ACCOUNT_PRICE",
                    "phone": state_info["phone"],
                    "country": country,
                    "client": state_info.get("client"),
                    "password": state_info.get("password")
                }
                await event.respond(f"💰 {country} ka price")
                return

            elif state == 'AWAITING_ACCOUNT_PRICE':
                try:
                    price = float(text)
                    user_states[user_id] = {
                        "state": "AWAITING_ACCOUNT_CATEGORY",
                        "phone": state_info["phone"],
                        "country": state_info["country"],
                        "price": price,
                        "password": state_info.get("password", "N/A"),
                        "client": state_info.get("client")
                    }
                    await event.respond("🏷️ Enter category for this account (e.g., 'usa', 'india', 'spam'):", buttons=cancel_button())
                except ValueError:
                    await event.respond("❌ Invalid price.")
                return

            elif state == 'AWAITING_ACCOUNT_CATEGORY':
                category = text.strip().lower()
                if not category:
                    await event.respond("❌ Category cannot be empty.")
                    return
                accounts_coll.insert_one({
                    "phone": state_info["phone"],
                    "country": state_info["country"],
                    "price": state_info["price"],
                    "password": state_info.get("password", "N/A"),
                    "category": category,
                    "status": "available",
                    "created_at": datetime.now()
                })
                await event.respond(
                    f"""
✅ Account Added
📱 {state_info['phone']}
🌍 {state_info['country']}
💰 ₹{state_info['price']}
🏷️ Category: {category}
""",
                    buttons=get_owner_keyboard()
                )
                user_states.pop(user_id, None)
                if state_info.get("client"):
                    await state_info["client"].disconnect()
                return

            elif state == 'AWAITING_REMOVE_PHONE':
                phone = text.strip()
                result = accounts_coll.delete_one({"phone": phone})
                if result.deleted_count == 0:
                    await event.respond("❌ Account not found.", buttons=get_owner_keyboard())
                    user_states.pop(user_id, None)
                    return
                session_file = os.path.join(SESSIONS_DIR, phone.replace("+", ""))
                try:
                    client = TelegramClient(session_file, API_ID, API_HASH)
                    await client.connect()
                    if await client.is_user_authorized():
                        await client.log_out()
                    await client.disconnect()
                except:
                    pass
                try:
                    os.remove(session_file + ".session")
                except:
                    pass
                user_states.pop(user_id, None)
                await event.respond(f"✅ Account Removed Successfully\n\n<code>{phone}</code>", buttons=get_owner_keyboard(), parse_mode="html")
                return

            elif state == 'AWAITING_EDIT_PHONE':
                phone = text.strip()
                account = accounts_coll.find_one({"phone": phone})
                if not account:
                    await event.respond("❌ Account not found")
                    return
                user_states[user_id] = {
                    "state": "AWAITING_EDIT_COUNTRY",
                    "phone": phone
                }
                await event.respond(f"Current Country: {account['country']}\n\nSend New Country")
                return

            elif state == 'AWAITING_EDIT_COUNTRY':
                country = text.strip()
                user_states[user_id]["country"] = country
                user_states[user_id]["state"] = "AWAITING_EDIT_PRICE"
                await event.respond("💰 New Price bhejo")
                return

            elif state == 'AWAITING_EDIT_PRICE':
                try:
                    price = float(text)
                except:
                    await event.respond("❌ Invalid Price")
                    return
                phone = state_info["phone"]
                country = state_info["country"]
                accounts_coll.update_one({"phone": phone}, {"$set": {"country": country, "price": price}})
                user_states.pop(user_id, None)
                await event.respond(
                    f"""
✅ Account Updated
📱 Phone: <code>{phone}</code>
🌍 Country: <code>{country}</code>
💰 Price: <code>₹{price}</code>
""",
                    parse_mode="html",
                    buttons=get_owner_keyboard()
                )
                return

            elif state == 'AWAITING_ANNOUNCEMENT':
                if text == "/done":
                    messages = state_info.get('messages', [])
                    if not messages:
                        await event.respond("<b>⚠️ No messages provided.</b> Announcement cancelled.", buttons=get_owner_keyboard(), parse_mode='html')
                    else:
                        progress_msg = await event.respond("<b>⏳ Starting Broadcast...</b>", parse_mode='html')
                        all_users = [doc["user_id"] for doc in users_coll.find({}, {"user_id": 1})]
                        success_count = 0
                        for i, uid in enumerate(all_users):
                            try:
                                for msg in messages:
                                    await bot.send_message(uid, msg)
                                success_count += 1
                                if (i + 1) % 5 == 0:
                                    await progress_msg.edit(f"<b>⏳ Broadcasting...</b>\nProgress: <code>{success_count}/{len(all_users)}</code>", parse_mode='html')
                            except:
                                continue
                            await asyncio.sleep(0.05)
                        await progress_msg.edit(f"<b>✅ Broadcast Complete!</b>\nSent to: <code>{success_count}</code> users.", buttons=get_owner_keyboard(), parse_mode='html')
                    user_states.pop(user_id)
                else:
                    state_info['messages'].append(event.message)
                    await event.respond("<b>📥 Message Captured.</b>\nSend another message or type <code>/done</code> to broadcast.", parse_mode='html')
                return

            elif state == 'AWAITING_ADD_MONEY_CHAT_ID':
                try:
                    target_id = int(text)
                    user_states[user_id] = {'state': 'AWAITING_ADD_MONEY_AMOUNT', 'target_id': target_id}
                    await event.respond(f"<b>💰 Amount to Add</b>\n━\nEnter the amount for user <code>{target_id}</code>:", buttons=cancel_button(), parse_mode='html')
                except ValueError:
                    await event.respond("<b>❌ Invalid ID.</b> Please send a numeric Chat ID.", parse_mode='html')
                return

            elif state == 'AWAITING_ADD_MONEY_AMOUNT':
                try:
                    amount = float(text)
                    target_id = state_info['target_id']
                    init_user(target_id)
                    if update_user_balance(target_id, amount):
                        await event.respond(f"<b>✅ Balance Updated</b>\n━\nUser: <code>{target_id}</code>\nAdded: <code>+{amount} INR</code>", buttons=get_owner_keyboard(), parse_mode='html')
                        await bot.send_message(target_id, f"<b>💰 Funds Added!</b>\nYour account has been credited with <code>{amount} INR</code>.", parse_mode='html')
                    else:
                        await event.respond("<b>❌ Database Error.</b>", buttons=get_owner_keyboard(), parse_mode='html')
                    user_states.pop(user_id)
                except ValueError:
                    await event.respond("<b>❌ Invalid Amount.</b> Send a number.", parse_mode='html')
                return

            elif state == 'AWAITING_CHECK_FUND_CHAT_ID':
                try:
                    target_id = int(text)
                    balance = get_user_balance(target_id)
                    await event.respond(f"<b>🔍 User Balance</b>\n━\nUser: <code>{target_id}</code>\nBalance: <code>{balance} INR</code>", buttons=get_owner_keyboard(), parse_mode='html')
                    user_states.pop(user_id)
                except ValueError:
                    await event.respond("<b>❌ Invalid ID.</b>", parse_mode='html')
                return

            elif state == 'AWAITING_DELETE_FUND_CHAT_ID':
                try:
                    target_id = int(text)
                    user_states[user_id] = {'state': 'AWAITING_DELETE_FUND_AMOUNT', 'target_id': target_id}
                    await event.respond(f"<b>🗑️ Amount to Deduct</b>\n━\nEnter amount to remove from <code>{target_id}</code>:", buttons=cancel_button(), parse_mode='html')
                except ValueError:
                    await event.respond("<b>❌ Invalid ID.</b>", parse_mode='html')
                return

            elif state == 'AWAITING_DELETE_FUND_AMOUNT':
                try:
                    amount = float(text)
                    target_id = state_info['target_id']
                    if update_user_balance(target_id, -amount):
                        await event.respond(f"<b>✅ Balance Deducted</b>\n━\nUser: <code>{target_id}</code>\nRemoved: <code>-{amount} INR</code>", buttons=get_owner_keyboard(), parse_mode='html')
                    else:
                        await event.respond("<b>❌ Database Error.</b>", parse_mode='html')
                    user_states.pop(user_id)
                except ValueError:
                    await event.respond("<b>❌ Invalid Amount.</b>", parse_mode='html')
                return

            elif state == 'AWAITING_NEW_PRICE':
                try:
                    new_price = float(text)
                    set_default_price(new_price)
                    await event.respond(f"<b>✅ Price Updated!</b>\nNew Default: <code>{new_price} INR</code>", buttons=get_owner_keyboard(), parse_mode='html')
                    user_states.pop(user_id)
                except ValueError:
                    await event.respond("<b>❌ Invalid Price.</b>", parse_mode='html')
                return

        # ----- USER FEATURES -----
        if state == "AWAITING_DEPOSIT_AMOUNT":
            try:
                amount = float(text)
                user_states[user_id] = {"state": "AWAITING_UTR", "amount": amount}
                loading = await event.respond("⏳ Generating Payment QR...\nPlease wait...")
                qr_url = f"https://fampay-w4t8.onrender.com/qr?upi={UPI_ID}&amount={amount}"
                img = requests.get(qr_url).content
                with open("deposit_qr.png", "wb") as f:
                    f.write(img)
                await loading.delete()
                await bot.send_file(
                    event.chat_id,
                    "deposit_qr.png",
                    caption=f"""
<b>💎 WALLET DEPOSIT</b>
━━━━━━━━━━━━━━━━━━━━
💰 <b>Deposit Amount:</b>
<code>₹{amount}</code>
━━━━━━━━━━━━━━━━━━━━
📋 After completing the payment,
send your <b>UTR / Transaction ID</b>.
""",
                    parse_mode="html"
                )
            except Exception as e:
                await event.respond("❌ Invalid amount.")
            return

        elif state == "AWAITING_UTR":
            ref = text.strip()
            if not ref.isalnum() or len(ref) < 5:
                await event.respond("❌ Please send a valid UTR / Transaction ID.")
                return
            if is_utr_used(ref):
                await event.respond("❌ This UTR has already been used.")
                return
            amount = state_info["amount"]
            try:
                checking = await event.respond("⏳ Checking payment...")
                api_url = f"https://fampay.anujbots.xyz/verify.php?order_id={ref}&api_key={PAYMENT_API_KEY}"
                r = requests.get(api_url, timeout=30)
                try:
                    data = r.json()
                except:
                    data = {"status": "error", "message": "Invalid response"}
                await checking.delete()
                if data.get("status") == "success":
                    if is_utr_used(ref):
                        await event.respond("❌ This UTR has already been processed.")
                        return
                    api_amount = float(data.get("data", {}).get("amount", amount))
                    init_user(user_id)
                    update_user_balance(user_id, api_amount)
                    deposits_coll.insert_one({
                        "user_id": user_id,
                        "utr": ref,
                        "amount": api_amount,
                        "status": "completed",
                        "created_at": datetime.now()
                    })
                    balance = get_user_balance(user_id)
                    await event.respond(
                        f"""
<b>🎉 DEPOSIT SUCCESSFUL</b>
━━━━━━━━━━━━━━━━━━━━
💰 <b>Amount Credited</b>
<code>₹{api_amount}</code>
🧾 <b>UTR ID</b>
<code>{ref}</code>
🏦 <b>Updated Balance</b>
<code>₹{balance}</code>
━━━━━━━━━━━━━━━━━━━━
✅ Payment Verified Successfully
🚀 Funds Added To Your Wallet
""",
                        parse_mode="html",
                        buttons=get_user_keyboard()
                    )
                    user_states.pop(user_id, None)
                else:
                    error_msg = data.get("message", "Payment not received. Please try again.")
                    user_states.pop(user_id, None)
                    await event.respond(
                        f"""
❌ <b>PAYMENT VERIFICATION FAILED</b>
━━━━━━━━━━━━━━━━━━━━
📝 <b>Reason:</b> <code>{error_msg}</code>
━━━━━━━━━━━━━━━━━━━━
⏳ Wait 1–2 minutes and try again.
🧾 Make sure the UTR is correct.
📞 If you have already made the payment,
contact the <b>Owner/Admin</b> with payment proof.
""",
                        parse_mode="html",
                        buttons=get_user_keyboard()
                    )
            except requests.exceptions.Timeout:
                user_states.pop(user_id, None)
                await event.respond("❌ Verification timeout. Please try again later.", buttons=get_user_keyboard())
            except Exception as e:
                user_states.pop(user_id, None)
                await event.respond(f"❌ Verification Error\n\n<code>{e}</code>", parse_mode="html")
            return

        # regular commands
        if text == "👤 My Account":
            balance = get_user_balance(user_id)
            await event.respond(
                f"<b>👤 Your Account Dashboard</b>\n━━━━━━━━━━━━━━━━━━━━\n🆔 User ID: <code>{user_id}</code>\n💰 Balance: <b>{balance} INR</b>\n━━━━━━━━━━━━━━━━━━━━\n<i>To add funds, contact the administrator.</i>",
                parse_mode='html'
            )
            return

        elif text == "🆘 Support":
            await event.respond("🆘 Support :\nhttps://t.me/CrazyAbhii")
            return

        elif text == "🛒 Buy ID":
            countries = list(accounts_coll.aggregate([
                {"$match": {"status": "available"}},
                {"$group": {"_id": {"country": "$country", "price": "$price"}, "total": {"$sum": 1}}},
                {"$sort": {"_id.price": 1}}
            ]))
            if not countries:
                await event.respond("📭 Out Of Stock!")
                return
            msg = "✨ Select Account\n\n"
            buttons = []
            row = []
            for c in countries:
                country = c["_id"]["country"]
                price = c["_id"]["price"]
                total = c["total"]
                msg += f"{country} | ₹{price} | {total} In Stock\n"
                row.append(Button.inline(f"{country}", data=f"country_{country}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([Button.inline("🏠 Main Menu", b"main_menu")])
            await event.respond(msg, buttons=buttons)

        elif text == "💳 Deposit":
            user_states[user_id] = {"state": "AWAITING_DEPOSIT_AMOUNT"}
            await event.respond("Enter the amount you wish to deposit", parse_mode="html")
            return

    # ---------- CALLBACK HANDLER ----------
    @bot.on(events.CallbackQuery)
    async def callback_handler(event):
        user_id = event.sender_id
        data = event.data.decode("utf-8")

        if data == "main_menu":
            kb = get_user_keyboard() if user_id not in ADMIN_IDS else get_owner_keyboard()
            await event.edit("🏠 Main Menu", buttons=kb)
            return

        if data == "back_countries":
            countries = list(accounts_coll.aggregate([
                {"$match": {"status": "available"}},
                {"$group": {"_id": {"country": "$country", "price": "$price"}, "total": {"$sum": 1}}},
                {"$sort": {"_id.price": 1}}
            ]))
            msg = "✨ Select Account\n\n"
            buttons = []
            row = []
            for c in countries:
                country = c["_id"]["country"]
                price = c["_id"]["price"]
                total = c["total"]
                msg += f"{country} | ₹{price} | {total} In Stock\n"
                row.append(Button.inline(f"{country}", data=f"country_{country}"))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            buttons.append([Button.inline("🏠 Main Menu", b"main_menu")])
            await event.edit(msg, buttons=buttons)
            return

        if data.startswith("country_"):
            country = data.replace("country_", "")
            acc = accounts_coll.find_one({"country": country, "status": "available"}, sort=[("_id", 1)])
            if not acc:
                await event.answer("❌ No stock available", alert=True)
                return
            await event.edit(
                f"""
<b>🛒 ACCOUNT DETAILS</b>
━━━━━━━━━━━━━━━━━━━━
🌍 <b>Country:</b> <code>{country}</code>
📱 <b>Number:</b> <code>{mask_phone(acc['phone'])}</code>
💰 <b>Price:</b> <code>₹{acc['price']}</code>
━━━━━━━━━━━━━━━━━━━━
⚡ Verified Account
🔐 OTP Forwarding Supported
<i>Click the button below to continue.</i>
""",
                buttons=[
                    [Button.inline("✅ Buy", f"buy_{acc['_id']}")],
                    [Button.inline("🔙 Back", b"back_countries")]
                ],
                parse_mode="html"
            )
            return

        if data.startswith("buy_"):
            acc_id = data.split("_")[1]
            acc = accounts_coll.find_one({"_id": ObjectId(acc_id)})
            if not acc or acc['status'] != 'available':
                await event.answer("⚠️ This ID was just sold!", alert=True)
                return
            balance = get_user_balance(user_id)
            if balance < acc['price']:
                await event.answer(f"❌ Insufficient Balance! (Needs {acc['price']} INR)", alert=True)
                return
            confirm_text = (
                "<b>🧾 Purchase Confirmation</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 Number: <code>{mask_phone(acc['phone'])}</code>\n"
                f"💵 Price: <b>{acc['price']} INR</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Note: OTPs will be automatically forwarded to this chat after purchase.</i>"
            )
            await event.edit(confirm_text, buttons=[
                [Button.inline("✅ Confirm & Pay", f"confirm_{acc['_id']}")],
                [Button.inline("❌ Cancel", b"cancel_buy")]
            ], parse_mode='html')
            return

        if data.startswith("confirm_"):
            acc_id = data.split("_")[1]
            acc = accounts_coll.find_one({"_id": ObjectId(acc_id)})
            if not acc or acc['status'] != 'available':
                await event.answer("Error: ID unavailable.")
                return
            balance = get_user_balance(user_id)
            if balance < acc['price']:
                await event.answer("Insufficient funds.")
                return
            result = users_coll.update_one({"user_id": user_id, "balance": {"$gte": acc['price']}}, {"$inc": {"balance": -acc['price']}})
            if result.modified_count == 0:
                await event.answer("Insufficient funds (concurrent).")
                return
            accounts_coll.update_one({"_id": ObjectId(acc_id)}, {"$set": {"status": "sold", "sold_to": user_id, "sold_at": datetime.now()}})
            await bot.send_message(
                SOLD_CHANNEL,
                f"""
<b>🚀 NEW SALE COMPLETED</b>
━━━━━━━━━━━━━━━━━━━━
📦 <b>Product:</b> Telegram Account
📱 <b>Number:</b> <code>{mask_phone(acc['phone'])}</code>
🌎 <b>Region:</b> <code>{acc['country']}</code>
💵 <b>Sale Price:</b> <code>₹{acc['price']} INR</code>
🆔 <b>Buyer:</b> <code>{user_id}</code>
━━━━━━━━━━━━━━━━━━━━
""",
                buttons=[Button.url("🤖 Buy Accounts Here", "https://t.me/Abhiidbot")],
                parse_mode="html"
            )
            success_ui = (
                "<b>🎉 Purchase Successful!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"📦 <b>Account:</b> <code>{acc['phone']}</code>\n"
                "📩 <b>OTP Status:</b> Monitoring for codes...\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "<i>Please wait for the official Telegram OTP.</i>"
            )
            await event.edit(success_ui, parse_mode='html')
            asyncio.create_task(start_otp_forwarding(acc['phone'], user_id))
            return

        if data == "cancel_buy":
            await event.edit("<b>⚠️ Purchase cancelled.</b>", parse_mode='html')
            return

        if data == "verify_sub":
            missing = await check_force_sub(user_id)
            if not missing:
                kb = get_user_keyboard() if user_id not in ADMIN_IDS else get_owner_keyboard()
                await event.edit("✅ Verification Successful\n\nWelcome To The Bot.", buttons=kb)
            else:
                await event.answer("❌ Join all channels first.", alert=True)

    # ---------- RUN ----------
    await bot.run_until_disconnected()

# ---------- DUMMY HTTP SERVER (keeps Render Web Service alive) ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def health():
    return "Bot is running", 200

def run_http():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ---------- ENTRY POINT ----------
if __name__ == "__main__":
    Thread(target=run_http, daemon=True).start()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.critical(f"Crashed: {e}\n{traceback.format_exc()}")
