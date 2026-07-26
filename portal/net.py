"""Cloudflared process, Quick Tunnel and idempotent Named Tunnel helpers."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import secrets
import socket
import stat
import time

import config

try:
    import httpx
    _HAVE_HTTPX = True
except ImportError:  # pragma: no cover
    _HAVE_HTTPX = False

CF_API = "https://api.cloudflare.com/client/v4"
CF_BIN_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
QUICK_RE = r"https://[a-zA-Z0-9.-]+\.trycloudflare\.com"
TUNNEL_NAME = "rubika-portal"
_DATA_DIR = getattr(config, "DATA_DIR", None) or "data"
_DRAIN_TASKS: dict[int, asyncio.Task] = {}


class TunnelError(Exception):
    def __init__(self, stage: str, detail: str):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}")


def _cf_bin_path() -> str:
    return os.path.join(_DATA_DIR, "cloudflared")


async def ensure_cloudflared() -> str:
    import shutil
    found = shutil.which("cloudflared")
    if found:
        return found
    local = _cf_bin_path()
    if os.path.exists(local) and os.access(local, os.X_OK):
        return local
    if not _HAVE_HTTPX:
        raise TunnelError("cloudflared", "cloudflared نصب نیست و httpx برای دانلود موجود نیست")
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        async with httpx.AsyncClient(follow_redirects=True, timeout=180) as client:
            response = await client.get(CF_BIN_URL)
            response.raise_for_status()
            with open(local, "wb") as handle:
                handle.write(response.content)
        os.chmod(local, os.stat(local).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return local
    except Exception as exc:
        raise TunnelError("cloudflared", f"دانلود cloudflared نشد: {repr(exc)[:120]}") from exc


async def _drain(proc, pattern: str | None, match_future: asyncio.Future | None) -> None:
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            if pattern and match_future and not match_future.done():
                match = re.search(pattern, raw.decode(errors="ignore"))
                if match:
                    match_future.set_result(match.group(0))
    finally:
        if match_future and not match_future.done():
            match_future.set_result(None)


async def _spawn(cmd: list[str], pattern: str | None = None):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    future = asyncio.get_running_loop().create_future() if pattern else None
    task = asyncio.create_task(_drain(proc, pattern, future))
    _DRAIN_TASKS[id(proc)] = task
    task.add_done_callback(lambda _task: _DRAIN_TASKS.pop(id(proc), None))
    return proc, future


async def stop_process(proc, timeout: float = 10.0) -> None:
    if proc is None:
        return
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=timeout)
    else:
        with contextlib.suppress(Exception):
            await proc.wait()
    task = _DRAIN_TASKS.pop(id(proc), None)
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def verify_url(url: str, timeout: float = 90.0) -> bool:
    if not _HAVE_HTTPX:
        return False
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(url.rstrip("/") + "/ping")
                if response.status_code == 200 and bool(response.json().get("ok")):
                    return True
            except Exception:
                pass
            await asyncio.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
    return False


async def verify_local(port: int, timeout: float = 30.0) -> bool:
    return await verify_url(f"http://127.0.0.1:{int(port)}", timeout)


async def start_quick(port: int):
    cf = await ensure_cloudflared()
    proc, url_future = await _spawn([cf, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"], QUICK_RE)
    try:
        url = await asyncio.wait_for(asyncio.shield(url_future), timeout=30)
        if not url:
            raise TunnelError("tunnel", "لینک Quick Tunnel دریافت نشد")
        if not await verify_url(url, timeout=45):
            raise TunnelError("verify", "Quick Tunnel به /ping پاسخ نداد")
        return proc, url, {"dns": "ok", "ssl": "ok", "domain_ping": "ok"}
    except asyncio.CancelledError:
        await asyncio.shield(stop_process(proc))
        raise
    except Exception:
        await stop_process(proc)
        raise


async def _cf_api(client, method: str, path: str, token: str, **kwargs):
    response = await client.request(
        method, CF_API + path,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        **kwargs,
    )
    try:
        data = response.json()
    except Exception as exc:
        raise TunnelError("cloudflare-api", f"{path} → پاسخ نامعتبر ({response.status_code})") from exc
    if not data.get("success"):
        if path.endswith("/tokens/verify"):
            raise TunnelError("token", "توکن نامعتبر است")
        raise TunnelError("cloudflare-api", f"{path} → {str(data.get('errors') or response.status_code)[:160]}")
    return data.get("result")


def _root_domain(domain: str) -> str:
    parts = [p for p in domain.lower().strip(".").split(".") if p]
    if len(parts) < 2:
        raise TunnelError("domain", "دامنه نامعتبر است")
    return ".".join(parts[-2:])


async def _account_zone(client, token: str, domain: str):
    await _cf_api(client, "GET", "/user/tokens/verify", token)
    zones = await _cf_api(client, "GET", f"/zones?name={_root_domain(domain)}", token)
    if not zones:
        raise TunnelError("domain", f"zone برای {_root_domain(domain)} یافت نشد")
    zone = zones[0]
    account_id = str((zone.get("account") or {}).get("id") or "")
    if not account_id:
        raise TunnelError("cloudflare-api", "Cloudflare account مربوط به zone مشخص نشد")
    return account_id, zone["id"]


async def inspect_domain(domain: str, token: str, ping_timeout: float = 15.0) -> dict:
    result = {"token": "failed", "dns": "failed", "ssl": "failed", "domain_ping": "failed", "detail": ""}
    if not (_HAVE_HTTPX and domain and token):
        result["detail"] = "دامنه یا توکن تنظیم نشده"
        return result
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            _account, zone = await _account_zone(client, token, domain)
            result["token"] = "ok"
            records = await _cf_api(client, "GET", f"/zones/{zone}/dns_records?name={domain}", token)
            result["dns"] = "ok" if records else "missing"
        try:
            await asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, domain, 443)
            if result["dns"] != "ok":
                result["dns"] = "resolving"
        except Exception:
            pass
        if await verify_url(f"https://{domain}", timeout=ping_timeout):
            result["ssl"] = "ok"
            result["domain_ping"] = "ok"
        else:
            result["detail"] = "HTTPS /ping پاسخ معتبر نداد"
    except TunnelError as exc:
        result["detail"] = exc.detail
    except Exception as exc:
        result["detail"] = repr(exc)[:160]
    return result


async def _ensure_named_tunnel(client, account_id: str, token: str) -> tuple[str, str]:
    existing = await _cf_api(
        client, "GET", f"/accounts/{account_id}/cfd_tunnel?name={TUNNEL_NAME}&is_deleted=false", token,
    ) or []
    active = next((item for item in existing if not item.get("deleted_at")), None)
    if active:
        tunnel_id = active["id"]
    else:
        secret = base64.b64encode(secrets.token_bytes(32)).decode()
        try:
            tunnel = await _cf_api(
                client, "POST", f"/accounts/{account_id}/cfd_tunnel", token,
                json={"name": TUNNEL_NAME, "tunnel_secret": secret},
            )
            tunnel_id = tunnel["id"]
        except TunnelError:
            # Concurrent/repeated activation may win the create race. Refetch
            # by the stable name and reuse instead of creating duplicates.
            existing = await _cf_api(
                client, "GET", f"/accounts/{account_id}/cfd_tunnel?name={TUNNEL_NAME}&is_deleted=false", token,
            ) or []
            active = next((item for item in existing if not item.get("deleted_at")), None)
            if not active:
                raise
            tunnel_id = active["id"]
    connector_token = await _cf_api(
        client, "GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token", token,
    )
    return tunnel_id, connector_token


async def start_domain(port: int, domain: str, api_token: str):
    if not _HAVE_HTTPX:
        raise TunnelError("domain", "httpx نصب نیست")
    domain = (domain or "").strip().lower().rstrip(".")
    if not api_token:
        raise TunnelError("token", "توکن Cloudflare تنظیم نشده")
    if not domain:
        raise TunnelError("domain", "دامنه تنظیم نشده")
    cf = await ensure_cloudflared()
    async with httpx.AsyncClient(timeout=30) as client:
        account_id, zone_id = await _account_zone(client, api_token, domain)
        tunnel_id, connector_token = await _ensure_named_tunnel(client, account_id, api_token)
        await _cf_api(
            client, "PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations", api_token,
            json={"config": {"ingress": [
                {"hostname": domain, "service": f"http://127.0.0.1:{port}"},
                {"service": "http_status:404"},
            ]}},
        )
        target = f"{tunnel_id}.cfargotunnel.com"
        records = await _cf_api(client, "GET", f"/zones/{zone_id}/dns_records?name={domain}", api_token) or []
        record = {"type": "CNAME", "name": domain, "content": target, "proxied": True}
        cname = next((item for item in records if item.get("type") == "CNAME"), None)
        conflicts = [item for item in records if item.get("type") in ("A", "AAAA")]
        if conflicts:
            kinds = ", ".join(sorted({str(item.get("type")) for item in conflicts}))
            raise TunnelError("dns", f"رکورد متعارض {kinds} برای {domain} وجود دارد؛ ابتدا آن را حذف کنید")
        if cname:
            await _cf_api(client, "PUT", f"/zones/{zone_id}/dns_records/{cname['id']}", api_token, json=record)
        else:
            await _cf_api(client, "POST", f"/zones/{zone_id}/dns_records", api_token, json=record)
    proc, _ = await _spawn([cf, "tunnel", "--no-autoupdate", "run", "--token", connector_token])
    url = f"https://{domain}"
    try:
        if not await verify_url(url, timeout=90):
            raise TunnelError("verify", "DNS/SSL/domain /ping در مهلت مقرر آماده نشد")
        return proc, url, {"dns": "ok", "ssl": "ok", "domain_ping": "ok", "tunnel_id": tunnel_id}
    except asyncio.CancelledError:
        await asyncio.shield(stop_process(proc))
        raise
    except Exception:
        await stop_process(proc)
        raise
