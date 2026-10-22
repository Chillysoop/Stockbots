"""The web side: Setup page, owner login, dashboard and its small JSON API."""
from __future__ import annotations

import logging
import os
import time

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from .alpaca import AlpacaClient, AlpacaError
from .app import PROVIDER_KEY, App
from .brains import BRAINS
from .config import BOTS, BOT_IDS
from .styles import STYLES

log = logging.getLogger("stockbots.web")
STATIC = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "sb_session"
AGREEMENTS = 3


def page(name: str) -> FileResponse:
    return FileResponse(os.path.join(STATIC, name), headers={"Cache-Control": "no-store"})


def json_error(message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message, **extra}, status_code=status)


def create_web(app: App, alpaca_check=None, data_factory=None) -> Starlette:
    failed_logins: list[float] = []

    def logged_in(request: Request) -> bool:
        return app.settings.valid_session(request.cookies.get(COOKIE))

    def set_session(resp: Response, request: Request) -> None:
        secure = request.url.scheme == "https"
        resp.set_cookie(COOKIE, app.settings.make_session(), max_age=30 * 86400, httponly=True,
                        samesite="strict", secure=secure, path="/")

    def guard_post(request: Request) -> JSONResponse | None:
        # Browsers can't add this header cross-site without asking first, which blocks CSRF.
        if request.headers.get("x-stockbots") != "1":
            return json_error("Missing request header.", 403)
        return None

    def require_owner(request: Request) -> JSONResponse | None:
        if not logged_in(request):
            return json_error("Please log in.", 401)
        return guard_post(request) if request.method == "POST" else None

    def check_alpaca(key: str, secret: str) -> dict:
        if alpaca_check:
            return alpaca_check(key, secret)
        # Setup always takes paper keys: they run the paper bots and feed market data.
        return AlpacaClient(key, secret, live=False).account()

    # ---- pages ----------------------------------------------------------

    async def index(request: Request):
        if not app.settings.setup_done:
            return RedirectResponse("/setup", status_code=302)
        if not logged_in(request):
            return RedirectResponse("/login", status_code=302)
        return page("dashboard.html")

    async def setup_page(request: Request):
        if app.settings.setup_done:
            return RedirectResponse("/login" if not logged_in(request) else "/", status_code=302)
        return page("setup.html")

    async def login_page(request: Request):
        if not app.settings.setup_done:
            return RedirectResponse("/setup", status_code=302)
        if logged_in(request):
            return RedirectResponse("/", status_code=302)
        return page("login.html")

    async def static_file(request: Request):
        name = request.path_params["name"]
        if name not in {"app.css", "dashboard.js", "setup.js", "login.js", "icon.svg"}:
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(os.path.join(STATIC, name), headers={"Cache-Control": "no-cache"})

    async def healthz(request: Request):
        return JSONResponse({"ok": True})

    # ---- setup ----------------------------------------------------------

    async def setup_status(request: Request):
        status = app.setup_status()
        status["mode"] = "live" if app.cfg.live else "paper"
        status["bots"] = [
            {
                "id": b["id"],
                "default_name": b["default_name"],
                "ai_label": b["ai_label"],
                "model": app.cfg.provider_model(b["provider"]),
                "style_label": STYLES[b["style"]].label,
                "rules": STYLES[b["style"]].default_rules,
                "key_field": PROVIDER_KEY[b["provider"]],
            }
            for b in BOTS
        ]
        return JSONResponse(status)

    async def setup_submit(request: Request):
        bad = guard_post(request)
        if bad:
            return bad
        status = app.setup_status()
        if not status["open"]:
            return json_error(status["reason"], 403)
        try:
            body = await request.json()
        except ValueError:
            return json_error("Bad request.")
        errors: dict[str, str] = {}
        agree = body.get("agree") or []
        if len([a for a in agree if a is True]) < AGREEMENTS:
            errors["agree"] = "Please tick all three statements."
        password = str(body.get("password") or "")
        if len(password) < 8:
            errors["password"] = "Use at least 8 characters."

        ai_keys = {k: str(body.get(k) or "").strip() for k in ("jev_key", "anthropic_key", "openai_key")}
        bots_in = body.get("bots") or {}
        bots: dict[str, dict] = {}
        for bot_id in BOT_IDS:
            b = bots_in.get(bot_id) or {}
            enabled = b.get("enabled", True) is not False
            bots[bot_id] = {
                "enabled": enabled,
                "name": str(b.get("name") or "").strip()[:40],
                "alpaca_key": str(b.get("alpaca_key") or "").strip() if enabled else "",
                "alpaca_secret": str(b.get("alpaca_secret") or "").strip() if enabled else "",
            }
            if enabled and (not bots[bot_id]["alpaca_key"] or not bots[bot_id]["alpaca_secret"]):
                errors[f"{bot_id}.alpaca"] = "Paste this bot's Alpaca key ID and secret."
        on = [b for b in BOTS if bots[b["id"]]["enabled"]]
        if not on:
            errors["bots"] = "Turn on at least one bot."
        needed = {PROVIDER_KEY[b["provider"]] for b in on}
        for field in needed:
            if not ai_keys[field]:
                errors[field] = "Paste this key."
        for field in set(ai_keys) - needed:
            ai_keys[field] = ""  # not used by any bot that's on: don't keep it
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)

        # Check every key for real (these calls are free).
        accounts: dict[str, str] = {}
        for bot_id, b in bots.items():
            if not b["enabled"]:
                continue
            try:
                acct = await run_in_threadpool(check_alpaca, b["alpaca_key"], b["alpaca_secret"])
                accounts[bot_id] = str(acct.get("account_number") or acct.get("id") or bot_id)
            except AlpacaError as exc:
                if exc.status in (401, 403):
                    msg = "Alpaca rejected these keys. Use the keys from this bot's paper account."
                else:
                    msg = f"Couldn't check these keys: {exc.message}"
                errors[f"{bot_id}.alpaca"] = msg
        if len(accounts) == len(on) and len(set(accounts.values())) < len(accounts):
            errors["accounts"] = ("Two bots are using the same Alpaca account. Give each bot its own paper "
                                  "account so their trades don't collide.")
        for bot in on:
            field = PROVIDER_KEY[bot["provider"]]
            if field in errors:
                continue
            brain = BRAINS[bot["provider"]](app.cfg, ai_keys[field])
            ok, msg = await run_in_threadpool(brain.check_key)
            if not ok:
                errors[field] = msg
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=400)

        s = app.settings
        s.data["bots"] = bots
        s.data.update(ai_keys)
        s.set_password(password)
        s.data["setup_done_at"] = int(time.time())
        await run_in_threadpool(s.save)
        app.db.add_event(None, "info", "Setup finished; bots started in " + ("LIVE" if app.cfg.live else "paper") + " mode")
        await run_in_threadpool(app.start_bots)
        resp = JSONResponse({"ok": True})
        set_session(resp, request)
        return resp

    # ---- login ----------------------------------------------------------

    async def login(request: Request):
        bad = guard_post(request)
        if bad:
            return bad
        now = time.time()
        recent = [t for t in failed_logins if now - t < 900]
        failed_logins[:] = recent
        if len(recent) >= 8:
            return json_error("Too many wrong tries. Wait 15 minutes.", 429)
        try:
            body = await request.json()
        except ValueError:
            return json_error("Bad request.")
        password = str(body.get("password") or "")
        ok = await run_in_threadpool(app.settings.check_password, password)
        if not ok:
            failed_logins.append(now)
            return json_error("Wrong password.", 401)
        resp = JSONResponse({"ok": True})
        set_session(resp, request)
        return resp

    async def setup_reopen(request: Request):
        bad = require_owner(request)
        if bad:
            return bad
        await run_in_threadpool(app.reopen_setup)
        resp = JSONResponse({"ok": True, "minutes": app.cfg.setup_window_min})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    async def logout(request: Request):
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    # ---- dashboard API --------------------------------------------------

    async def state(request: Request):
        bad = require_owner(request)
        if bad:
            return bad
        snap = await run_in_threadpool(app.engine.snapshot)
        return JSONResponse(snap, headers={"Cache-Control": "no-store"})

    async def bot_action(request: Request):
        bad = require_owner(request)
        if bad:
            return bad
        bot_id = request.path_params["bot_id"]
        action = request.path_params["action"]
        runner = app.engine.runners.get(bot_id)
        if bot_id not in BOT_IDS or runner is None:
            return json_error("Unknown bot.", 404)
        if action == "pause":
            app.db.set_paused(bot_id, True)
            app.db.add_event(bot_id, "info", "Owner paused the bot")
            return JSONResponse({"ok": True, "message": "Paused: no new trades. Stops stay in place."})
        if action == "resume":
            app.db.set_paused(bot_id, False)
            runner.finish_move()
            app.db.add_event(bot_id, "info", f"Owner resumed the bot on {runner.broker_label}")
            return JSONResponse({"ok": True, "message": f"Resumed on {runner.broker_label}."})
        if action == "prepare-move":
            try:
                msg = await run_in_threadpool(runner.prepare_move)
            except AlpacaError as exc:
                return json_error(f"{runner.broker_label}: {exc.message}", 502)
            return JSONResponse({"ok": True, "message": msg})
        if action == "flatten":
            try:
                msg = await run_in_threadpool(runner.flatten)
            except AlpacaError as exc:
                return json_error(f"Alpaca: {exc.message}", 502)
            return JSONResponse({"ok": True, "message": msg})
        if action == "rules":
            try:
                body = await request.json()
            except ValueError:
                return json_error("Bad request.")
            rules = str(body.get("rules") or "").strip()
            if len(rules) > 1500:
                return json_error("Keep the rules under 1,500 characters.")
            app.db.set_rules(bot_id, rules)
            app.db.add_event(bot_id, "info", "Owner updated the rules" if rules else "Owner reset the rules")
            return JSONResponse({"ok": True, "rules": runner.rules()})
        return json_error("Unknown action.", 404)

    # ---- backtests ------------------------------------------------------

    async def backtests_list(request: Request):
        bad = require_owner(request)
        if bad:
            return bad
        return JSONResponse(app.backtests.list(), headers={"Cache-Control": "no-store"})

    async def backtests_start(request: Request):
        bad = require_owner(request)
        if bad:
            return bad
        try:
            body = await request.json()
        except ValueError:
            return json_error("Bad request.")
        bot_id = str(body.get("bot") or "")
        if bot_id not in BOT_IDS:
            return json_error("Unknown bot.", 404)
        try:
            months = int(body.get("months") or 24)
        except (TypeError, ValueError):
            return json_error("Pick a period.")
        if not 1 <= months <= 36:
            return json_error("Pick a period between 1 and 36 months.")
        keys = app.settings.bot(bot_id)
        if not keys.get("alpaca_key") or not keys.get("alpaca_secret"):
            return json_error("This bot needs its Alpaca paper keys (from Setup) to download past prices.")
        runner = app.engine.runners.get(bot_id)
        name = runner.display_name if runner else bot_id
        factory = data_factory or (app.client_factory if app.custom_clients
                                   else (lambda k, s: AlpacaClient(k, s, live=False)))
        data_client = factory(keys["alpaca_key"], keys["alpaca_secret"])
        try:
            job = app.backtests.start(bot_id, months, data_client, name)
        except RuntimeError as exc:
            return json_error(str(exc), 409)
        return JSONResponse({"ok": True, "id": job})

    async def backtest_report(request: Request):
        if not logged_in(request):
            return RedirectResponse("/login", status_code=302)
        path = app.backtests.report_path(request.path_params["job_id"])
        if not path:
            return PlainTextResponse("Report not found", status_code=404)
        return FileResponse(path, media_type="text/html", headers={"Cache-Control": "no-store"})

    routes = [
        Route("/", index),
        Route("/setup", setup_page),
        Route("/login", login_page),
        Route("/static/{name}", static_file),
        Route("/healthz", healthz),
        Route("/api/setup", setup_status, methods=["GET"]),
        Route("/api/setup", setup_submit, methods=["POST"]),
        Route("/api/setup/reopen", setup_reopen, methods=["POST"]),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/state", state),
        Route("/api/bots/{bot_id}/{action}", bot_action, methods=["POST"]),
        Route("/api/backtests", backtests_list, methods=["GET"]),
        Route("/api/backtests", backtests_start, methods=["POST"]),
        Route("/backtests/{job_id}", backtest_report),
    ]
    return SecurityHeaders(Starlette(routes=routes))


class SecurityHeaders:
    """Adds a few protective headers to every response."""

    EXTRA = [
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
    ]

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + self.EXTRA
            await send(message)

        return await self.inner(scope, receive, send_with_headers)
