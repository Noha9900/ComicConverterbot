"""
A small aiohttp server that runs alongside the Pyrogram client.

Important honesty note: Pyrogram talks to Telegram over MTProto (a
persistent socket), not the classic Bot-API HTTP long-poll/webhook
model, so there is no "receive Telegram updates via HTTP POST" mode to
turn on here -- that concept doesn't apply to this stack. What this
module actually provides:

  GET  /health            -- liveness/readiness probe (always on if
                              ENABLE_HEALTH_SERVER=true)
  POST /notify?secret=... -- (only if WEBHOOK_MODE=true) lets an
                              external system ask the bot to push a
                              message into a chat, e.g. from a CI job
                              or another service. Requires
                              WEBHOOK_SECRET to be set and matched.

This is the closest honest equivalent to "webhook mode" for an MTProto
bot: an HTTP surface the bot exposes, not one Telegram pushes updates
into.
"""

import json
import logging
import time

from aiohttp import web

import config
import utils.admin_store as admin_store

logger = logging.getLogger(__name__)

_start_time = time.time()


def build_app(pyrogram_client) -> web.Application:
    app = web.Application()

    async def health(_request):
        stats = admin_store.stats_snapshot()
        return web.json_response(
            {
                "status": "ok",
                "uptime_s": round(time.time() - _start_time, 1),
                "known_users": stats["known_users"],
                "total_conversions": stats["total_conversions"],
            }
        )

    app.router.add_get("/health", health)

    if config.WEBHOOK_MODE:
        async def notify(request: web.Request):
            if not config.WEBHOOK_SECRET or request.query.get("secret") != config.WEBHOOK_SECRET:
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                body = await request.json()
                chat_id = int(body["chat_id"])
                text = str(body["text"])
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                return web.json_response({"error": "expected JSON {chat_id, text}"}, status=400)
            try:
                await pyrogram_client.send_message(chat_id, text)
            except Exception as e:  # noqa: BLE001
                logger.exception("notify() failed to send message")
                return web.json_response({"error": str(e)}, status=500)
            return web.json_response({"status": "sent"})

        app.router.add_post("/notify", notify)
        logger.info("Webhook /notify endpoint enabled on port %s", config.HEALTH_CHECK_PORT)

    return app


async def start(pyrogram_client) -> web.AppRunner:
    app = build_app(pyrogram_client)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.HEALTH_CHECK_PORT)
    await site.start()
    logger.info("Health-check server listening on :%s/health", config.HEALTH_CHECK_PORT)
    return runner
