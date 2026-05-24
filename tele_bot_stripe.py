#!/usr/bin/env python3
"""
tele_bot_stripe.py — Stripe/Payments slice factored out of tele_bot.py

This module groups ALL Stripe/Telegram-payments related config and handlers.
Wire it into your bot with register_stripe_handlers(app) and boot_stripe_webhook_once().

Env vars used (same as before):
  STRIPE_SECRET_KEY        — your Stripe secret key
  STRIPE_PRICE_STARTER     — price id for Starter plan (recurring)
  STRIPE_PRICE_PRO         — price id for Pro plan (recurring)
  STRIPE_PRICE_DEALER      — price id for Dealer plan (recurring)
  STRIPE_WEBHOOK_SECRET    — webhook signing secret (from Stripe)
  STRIPE_WEBHOOK_PORT      — optional, default 9090
  TELEGRAM_PROVIDER_TOKEN  — BotFather/Stripe provider token (for native TG payments)
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta, timezone

import stripe
from flask import Flask, request, jsonify

from telegram import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    LinkPreviewOptions
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# ====== DB hooks (same as in tele_bot.py) ======
from db import upsert_plan, get_plan, get_plan_limit

from telegram.error import BadRequest, Forbidden

import secrets
from collections import deque

# Short-lived token -> session mapping (in-memory)
SESSION_TOKENS: dict[str, dict] = {}
SESSION_ORDER = deque()  # to purge old tokens
MAX_SESSION_TOKENS = 500

def _put_token(token: str, data: dict) -> None:
    SESSION_TOKENS[token] = data
    SESSION_ORDER.append(token)
    # simple LRU-ish cap
    while len(SESSION_ORDER) > MAX_SESSION_TOKENS:
        old = SESSION_ORDER.popleft()
        SESSION_TOKENS.pop(old, None)

def _get_token(token: str) -> dict | None:
    return SESSION_TOKENS.get(token)


# --------------------------------------------------------------------------------------
# Stripe & Telegram Payments configuration
# --------------------------------------------------------------------------------------
stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
PRICE_IDS = {
    "starter": os.getenv("STRIPE_PRICE_STARTER", ""),
    "pro":     os.getenv("STRIPE_PRICE_PRO", ""),
    "dealer":  os.getenv("STRIPE_PRICE_DEALER", ""),
}
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PLAN_DUR_DAYS = 30

# Telegram native payments config
PROVIDER_TOKEN = os.getenv("TELEGRAM_PROVIDER_TOKEN", "")  # via @BotFather
CURRENCY = "USD"
PLAN_PRICES_MINOR = {
    "starter":  999,
    "pro":     1799,
    "dealer":  3999,
}

# Webhook Flask app (runs in background thread)
webhook_app = Flask("stripe_webhook")

# --------------------------------------------------------------------------------------
# Utilities
# --------------------------------------------------------------------------------------

def _iso_plus_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

# --------------------------------------------------------------------------------------
# Commands / callbacks for PLANS & PAYMENTS (Telegram & Stripe Checkout)
# --------------------------------------------------------------------------------------

async def my_plan(update, context):
    tg_id = update.effective_user.id
    plan, limit_ = get_plan_limit(tg_id)
    p = get_plan(tg_id) or {}
    exp = p.get("expires_at") or "—"
    await update.message.reply_text(
        f"📦 Plan: {plan}\n"
        f"🧩 Subscriptions limit: {limit_}\n"
        f"⏳ Expires: {exp}"
    )

async def upgrade(update, context):
    kb = [
        [InlineKeyboardButton("Starter (10 subs) – $9.99", callback_data="pay:starter")],
        [InlineKeyboardButton("Pro (20 subs) – $17.99",   callback_data="pay:pro")],
        [InlineKeyboardButton("Dealer (50 subs) – $39.99",callback_data="pay:dealer")],
    ]
    await update.message.reply_text("Choose a plan:", reply_markup=InlineKeyboardMarkup(kb))

# ===== Native Telegram Payments (uses provider token) =====
async def on_pay_button(update, context):
    q = update.callback_query
    await q.answer()
    plan = q.data.split(":")[1]
    amount_minor = PLAN_PRICES_MINOR[plan]
    title = f"{plan.title()} Plan — {CURRENCY} {amount_minor/100:.2f}"
    await context.bot.send_invoice(
        chat_id=q.message.chat_id,
        title=title,
        description=f"{plan.title()} plan for Yad2 alerts",
        payload=f"plan:{plan}:{q.from_user.id}",
        provider_token=PROVIDER_TOKEN,
        currency=CURRENCY,
        prices=[LabeledPrice(label=title, amount=amount_minor)],
        start_parameter=f"buy_{plan}",
        need_name=False, need_phone_number=False, need_email=False,
    )

async def pre_checkout(update, context):
    # Telegram requires an explicit OK here
    await update.pre_checkout_query.answer(ok=True)

async def paid(update, context):
    sp = update.message.successful_payment
    payload = sp.invoice_payload  # "plan:<plan>:<tg_id>"
    _, plan, tg_id = payload.split(":")
    tg_id = int(tg_id)
    expires_at = _iso_plus_days(PLAN_DUR_DAYS)
    upsert_plan(tg_id, plan, expires_at)
    await update.message.reply_text(
        f"✅ Payment received. Your plan is now *{plan.title()}* (valid until {expires_at}).",
        parse_mode=ParseMode.MARKDOWN,
    )



# async def register_plan_cb(update, context):
#     q = update.callback_query
#     await q.answer()
#     await q.message.delete()
#     # ✅ Delete the message the user clicked on
#     await q.message.delete()

    # kb = InlineKeyboardMarkup([
    #     [InlineKeyboardButton("Starter • 10 מנויים — $9.99", callback_data="buy:starter")],
    #     [InlineKeyboardButton("Pro • 20 מנויים — $17.99",   callback_data="buy:pro")],
    #     [InlineKeyboardButton("Dealer • 50 מנויים — $39.99", callback_data="buy:dealer")],
    #     [InlineKeyboardButton("🏠 תפריט ראשי", callback_data="home")],
    # ])

async def register_plan_cb(update, context):
    q = update.callback_query
    await q.answer()

    # Delete (or at least clear) the message that had the button
    if q.message:
        try:
            await q.message.delete()
        except (BadRequest, Forbidden):
            try:
                await q.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        except Exception:
            pass

    kb = InlineKeyboardMarkup([
    [InlineKeyboardButton("מתחיל • 10 דגמים — ‎150‎ ₪",   callback_data="buy:starter")],
    [InlineKeyboardButton("מתקדם • 20 דגמים — ‎200‎ ₪",   callback_data="buy:pro")],
    [InlineKeyboardButton("דילר • 50 דגמים — ‎400‎ ₪",    callback_data="buy:dealer")],
    [InlineKeyboardButton("🏠 תפריט ראשי", callback_data="home")],
])


    await q.message.chat.send_message(
        text="בחר/י מסלול ותקבל/י קישור תשלום מאובטח (Stripe):",
        reply_markup=kb,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )


async def buy_plan_cb(update, context):
    q = update.callback_query
    await q.answer()
    plan = q.data.split(":")[1]
    price_id = PRICE_IDS.get(plan)
    if not price_id:
        await q.edit_message_text("⚠️ תמחור לא מוגדר עדיין. פנה למנהל.")
        return

    tg_id = q.from_user.id
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url="https://t.me/%s?start=success" % (context.bot.username,),
            cancel_url="https://t.me/%s?start=cancel" % (context.bot.username,),
            metadata={"telegram_id": str(tg_id), "plan": plan},
        )
        token = secrets.token_urlsafe(8)  # short (≈11 chars)
        _put_token(token, {
            "session_id": session.id,
            "plan": plan,
            "telegram_id": tg_id,
        })

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("פתח קישור תשלום", url=session.url)],
            [InlineKeyboardButton("בדוק סטטוס", callback_data=f"check_paid:{token}")],
        ])

        await q.edit_message_text(
            "נוצר קישור תשלום. השלם/י את התשלום בדפדפן ואז חזר/י לכאן ולחץ/י על 'בדוק סטטוס'.",
            reply_markup=kb,
        )
    except Exception as e:
        await q.edit_message_text(f"שגיאת Stripe: {e}")

async def check_paid_cb(update, context):
    q = update.callback_query
    await q.answer()
    _, token = q.data.split(":", 1)

    info = _get_token(token)
    if not info:
        await q.edit_message_text("הטוקן לא נמצא/פג. צור לינק חדש בבקשה.")
        return

    session_id = info["session_id"]
    plan = info["plan"]

    try:
        session = stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
        if session.get("status") == "complete" or session.get("payment_status") == "paid":
            expires_at = _iso_plus_days(PLAN_DUR_DAYS)
            upsert_plan(int(session.metadata["telegram_id"]), plan, expires_at)
            await q.edit_message_text(f"✅ שודרגת למסלול {plan.title()} (עד {expires_at}).")
        else:
            await q.answer("עדיין לא שולם / לא הושלם", show_alert=True)
    except Exception as e:
        await q.edit_message_text(f"שגיאה בבדיקה: {e}")


# --------------------------------------------------------------------------------------
# Webhook server (Stripe -> us)
# --------------------------------------------------------------------------------------
@webhook_app.post("/stripe/webhook")
def stripe_webhook():
    payload = request.data
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig, secret=WEBHOOK_SECRET)
    except Exception as e:
        return (str(e), 400)

    if event["type"] == "checkout.session.completed":
        sess = event["data"]["object"]
        plan = (sess.get("metadata") or {}).get("plan")
        tg_id = (sess.get("metadata") or {}).get("telegram_id")
        if plan and tg_id:
            try:
                expires_at = _iso_plus_days(PLAN_DUR_DAYS)
                upsert_plan(int(tg_id), plan, expires_at)
            except Exception as e:
                print(f"[webhook] upsert_plan error: {e}")

    # could also handle invoice.paid/customer.subscription.updated etc.
    return jsonify({"status": "ok"})


def boot_stripe_webhook_once():
    """Run Flask in a background daemon thread. Call exactly once at startup."""
    def _run():
        port = int(os.getenv("STRIPE_WEBHOOK_PORT", "9090"))
        print(f"[stripe-webhook] listening on 0.0.0.0:{port}")
        webhook_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
    t = threading.Thread(target=_run, daemon=True)
    t.start()

# --------------------------------------------------------------------------------------
# Registration helper for tele_bot.py
# --------------------------------------------------------------------------------------

def register_stripe_handlers(app: Application) -> None:
    """Attach all commands/callbacks pertaining to payments.
    Usage in tele_bot.py:
        from tele_bot_stripe import register_stripe_handlers, boot_stripe_webhook_once
        register_stripe_handlers(app)
        boot_stripe_webhook_once()
    """
    # Commands
    app.add_handler(CommandHandler("upgrade", upgrade))
    app.add_handler(CommandHandler("my_plan", my_plan))

    # Native Telegram Payments flow
    app.add_handler(CallbackQueryHandler(on_pay_button, pattern=r"^pay:(starter|pro|dealer)$"))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, paid))

    # Stripe Checkout flow
    app.add_handler(CallbackQueryHandler(register_plan_cb, pattern=r"^register_plan$"))
    app.add_handler(CallbackQueryHandler(buy_plan_cb,       pattern=r"^buy:(starter|pro|dealer)$"))
    app.add_handler(CallbackQueryHandler(check_paid_cb, pattern=r"^check_paid:[A-Za-z0-9_\-]+$"))
    # Webhook server (background)
    # NOTE: call boot_stripe_webhook_once() separately in your startup after Application.build()
    # to avoid accidental multiple threads during reloads.
