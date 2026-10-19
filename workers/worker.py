"""
Worker subsystem (master side).
================================

This module lets the MASTER orchestrate one or more WORKER servers so that
login / send jobs are spread across several clean IPs. It deliberately does
NOT touch the sending/login logic in `rubika_client.py`; workers run that
SAME unchanged code behind a small API (see `worker_api.py`).

Responsibilities here (master side):
  * generate worker tags  (e.g. "#W8_819"),
  * provision a fresh server over SSH + Docker,
  * keep a secure SSH tunnel to each worker's loopback-only API,
  * call the worker API (login relay, send, health),
  * round-robin selection of a worker for a NEW account, with failover,
  * health checks (run in parallel) + a tiny in-memory cache,
  * pull worker session files into a backup zip.

Heavy third-party deps (asyncssh, httpx) are imported lazily INSIDE the
functions that need them, so simply importing this module (e.g. from the
backup hook in bot.py) never fails on a machine without them installed.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import time

import config
import crypto_util
import db

# Where the worker checkout / data live on the remote server.
REMOTE_DIR = "~/v2rubby_worker"
REMOTE_DATA = "~/v2rubby_worker_data"
CONTAINER = "v2rubby-worker"
IMAGE = "v2rubby-worker"

# in-memory: worker_id -> {"conn":.., "listener":.., "local_port":int}
_tunnels: dict = {}
_tunnel_locks: dict = {}
# in-memory health cache: worker_id -> {"status","ping_ms","file_ok","ts","mono"}
_health_cache: dict = {}
# in-memory last failure reason per worker (diagnostic), worker_id -> str|None
_health_detail: dict = {}
# in-memory per-worker tunnel supervisor tasks: worker_id -> asyncio.Task
_supervisors: dict = {}


# --------------------------------------------------------------------------- #
# Tags
# --------------------------------------------------------------------------- #
def gen_tag(is_master: bool = False) -> str:
    """Random worker tag like '#W8_819'. Master uses the '#W0_xxx' family."""
    existing = {w["tag"] for w in db.list_workers()}
    for _ in range(200):
        lead = "0" if is_master else str(random.randint(1, 9))
        tag = f"#W{lead}_{random.randint(100, 999)}"
        if tag not in existing:
            return tag
    return f"#W{secrets.token_hex(2)}"


# --------------------------------------------------------------------------- #
# Master-as-worker bootstrap
# --------------------------------------------------------------------------- #
def ensure_master_worker() -> dict:
    """Make sure a local 'master' worker row exists (runs jobs in-process)."""
    m = db.get_master_worker()
    if m:
        return m
    if not config.MASTER_AS_WORKER:
        return None
    tag = gen_tag(is_master=True)
    wid = db.add_worker(
        tag=tag, ip="local", ssh_port=0, ssh_user="", ssh_pass_enc="",
        api_port=0, api_token_enc="", is_master=1,
    )
    return db.get_worker(wid)


def is_local(worker: dict) -> bool:
    return bool(worker and worker.get("is_master"))


# --------------------------------------------------------------------------- #
# Colour / formatting helpers (shared with bot.py logging)
# --------------------------------------------------------------------------- #
def status_emoji(worker: dict) -> str:
    if not worker.get("file_ok"):
        return "🔴"
    ping = worker.get("ping_ms", -1)
    if ping is None or ping < 0:
        return "🟡"
    if ping <= config.PING_GREEN_MS:
        return "🟢"
    if ping <= config.PING_YELLOW_MS:
        return "🟡"
    return "🔴"


def file_label(worker: dict) -> str:
    return "File ok" if worker.get("file_ok") else "Blocked"


# --------------------------------------------------------------------------- #
# Low-level SSH helpers (asyncssh, lazy import)
# --------------------------------------------------------------------------- #
async def _ssh_connect(ip: str, port: int, user: str, password: str,
                       keepalive: bool = True):
    """Open an SSH connection with hard timeouts so a slow/flaky server can
    NEVER hang a caller forever.

    - connect_timeout / login_timeout (8s): bound the TCP + auth handshake.
    - keepalive=True (default, used ONLY by the persistent tunnel): keepalive
      15s x3 keeps a warm link alive and detects a dead one within ~45s.
    - keepalive=False (one-shot admin ops: update / provision / restart /
      teardown / session backup): NO keepalive is passed, so a long
      `docker build` on a loaded server can't be dropped mid-flight by a missed
      keepalive.
    - the whole connect is wrapped in asyncio.wait_for(10) as a
      version-independent backstop (older asyncssh may lack connect_timeout)."""
    import asyncssh  # lazy
    base = dict(
        host=ip, port=int(port or 22), username=user, password=password,
        known_hosts=None,  # personal tool: trust on first use
        login_timeout=8,
    )
    if keepalive:
        base["keepalive_interval"] = 15
        base["keepalive_count_max"] = 3

    async def _do():
        try:
            return await asyncssh.connect(connect_timeout=8, **base)
        except TypeError:
            # very old asyncssh without the connect_timeout kwarg
            return await asyncssh.connect(**base)

    return await asyncio.wait_for(_do(), timeout=10)


async def _run(conn, command: str, check: bool = False):
    """Run a command over an open SSH connection -> (exit_status, stdout, stderr)."""
    res = await conn.run(command, check=check)
    return res.exit_status, (res.stdout or ""), (res.stderr or "")


# --------------------------------------------------------------------------- #
# Provisioning: SSH in, install Docker, clone repo, build + run worker.
# `on_progress` is an async callback(str) for live updates in Telegram.
# Returns dict {ok, tag, api_port, api_token, error}.
# --------------------------------------------------------------------------- #
async def provision_worker(ip: str, ssh_port: int, ssh_user: str, ssh_pass: str,
                           tag: str = None, on_progress=None) -> dict:
    async def say(msg: str):
        if on_progress:
            try:
                await on_progress(msg)
            except Exception:
                pass

    api_port = config.WORKER_API_PORT
    api_token = secrets.token_urlsafe(24)
    tag = tag or gen_tag()

    try:
        import asyncssh  # noqa: F401  (fail early with a clear message)
    except ImportError:
        return {"ok": False, "error": "بسته‌ی asyncssh روی مستر نصب نیست (pip install asyncssh)."}

    conn = None
    try:
        await say("🔌 اتصال SSH به سرور ...")
        conn = await _ssh_connect(ip, ssh_port, ssh_user, ssh_pass, keepalive=False)

        await say("🐳 بررسی/نصب Docker (با صبر برای قفلِ apt) ...")
        # Fresh Ubuntu servers run unattended-upgrades right after boot, which
        # holds the dpkg lock for minutes; that previously made the docker
        # install silently fail and the later `docker build` die with
        # "docker: command not found". So: stop auto-upgrades for this run, tell
        # apt to WAIT for the lock (DPkg::Lock::Timeout), install docker from the
        # distro repo (most reliable), fall back to get.docker.com, then VERIFY.
        install_script = (
            "export DEBIAN_FRONTEND=noninteractive\n"
            "systemctl stop unattended-upgrades >/dev/null 2>&1 || true\n"
            "if ! command -v docker >/dev/null 2>&1; then\n"
            "  apt-get -o DPkg::Lock::Timeout=180 update -qq || true\n"
            "  apt-get -o DPkg::Lock::Timeout=180 install -y -qq "
            "ca-certificates curl git docker.io || true\n"
            "fi\n"
            "command -v docker >/dev/null 2>&1 || "
            "{ curl -fsSL https://get.docker.com | sh; } || true\n"
            "command -v git >/dev/null 2>&1 || "
            "apt-get -o DPkg::Lock::Timeout=180 install -y -qq git || true\n"
            "systemctl enable --now docker >/dev/null 2>&1 || true\n"
            "if command -v docker >/dev/null 2>&1; then docker --version; "
            "echo DOCKER_OK; else echo DOCKER_MISSING; fi\n"
        )
        code, out, err = await _run(conn, install_script)
        if "DOCKER_OK" not in (out or ""):
            return {"ok": False,
                    "error": ("نصبِ Docker روی سرور ناموفق بود (احتمالاً قفلِ apt یا "
                              "نبودِ اینترنت). روی همون سرور دستی بزن:  "
                              "apt-get install -y docker.io && systemctl enable --now docker  "
                              "بعد دوباره «افزودن ورکر». جزئیات: "
                              + ((err or out) or "")[-300:])}

        await say("📥 دریافت سورس از گیت‌هاب ...")
        code, out, err = await _run(
            conn,
            f"rm -rf {REMOTE_DIR} && "
            f"git clone --depth 1 -b {config.GIT_BRANCH} {config.GIT_REPO_URL} {REMOTE_DIR}",
        )
        if code != 0:
            return {"ok": False, "error": f"git clone شکست خورد: {err[:200] or out[:200]}"}

        await say("📝 نوشتن تنظیمات ورکر (.env) ...")
        env_lines = (
            "MODE=worker\n"
            f"WORKER_API_TOKEN={api_token}\n"
            f"WORKER_API_PORT={api_port}\n"
            # With host networking the API binds to the host's loopback, which
            # is private (only the master's SSH tunnel reaches it).
            "WORKER_BIND_HOST=127.0.0.1\n"
            f"TIMEZONE={config.TIMEZONE}\n"
        )
        # write .env safely via a heredoc
        await _run(conn, f"mkdir -p {REMOTE_DATA}")
        await _run(
            conn,
            f"cat > {REMOTE_DIR}/.env <<'ENVEOF'\n{env_lines}ENVEOF",
        )

        await say("🏗 ساخت ایمیج Docker (ممکنه چند دقیقه طول بکشه) ...")
        # --network=host lets build steps use the SERVER's network/DNS, which
        # avoids the common "Docker build container can't resolve DNS / reach
        # PyPI" failure on fresh servers.
        code, out, err = await _run(
            conn, f"cd {REMOTE_DIR} && docker build --network=host -t {IMAGE} .")
        if code != 0:
            return {"ok": False,
                    "error": f"docker build شکست خورد: {(err or out)[-600:]}"}

        await say("🚀 اجرای کانتینر ورکر ...")
        # --network=host so the container uses the SERVER's DNS/network. The
        # default bridge network has broken DNS on many fresh servers, which
        # would make the worker unable to resolve Rubika (-> always "Blocked").
        # With host networking, WORKER_BIND_HOST=127.0.0.1 keeps the API private
        # (only the master's SSH tunnel can reach it).
        run_cmd = (
            f"docker rm -f {CONTAINER} 2>/dev/null; "
            f"docker run -d --name {CONTAINER} --restart always "
            f"--network=host "
            f"--env-file {REMOTE_DIR}/.env "
            f"-v {REMOTE_DATA}:/app/data {IMAGE}"
        )
        code, out, err = await _run(conn, run_cmd)
        if code != 0:
            return {"ok": False, "error": f"docker run شکست خورد: {err[:200] or out[:200]}"}

        await say("✅ نصب کامل شد.")
        return {"ok": True, "tag": tag, "api_port": api_port, "api_token": api_token}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


async def register_provisioned(ip, ssh_port, ssh_user, ssh_pass, prov: dict) -> int:
    """Persist a successfully provisioned worker (encrypting secrets)."""
    return db.add_worker(
        tag=prov["tag"], ip=ip, ssh_port=int(ssh_port or 22), ssh_user=ssh_user,
        ssh_pass_enc=crypto_util.encrypt(ssh_pass),
        api_port=int(prov["api_port"]),
        api_token_enc=crypto_util.encrypt(prov["api_token"]),
        is_master=0,
    )


# --------------------------------------------------------------------------- #
# Remote lifecycle ops (restart / update / teardown) over SSH.
# --------------------------------------------------------------------------- #
async def _with_conn(worker: dict, keepalive: bool = True):
    return await _ssh_connect(
        worker["ip"], worker["ssh_port"], worker["ssh_user"],
        crypto_util.decrypt(worker["ssh_pass_enc"]),
        keepalive=keepalive,
    )


async def restart_worker(worker: dict) -> tuple:
    conn = await _with_conn(worker, keepalive=False)
    try:
        return await _run(conn, f"docker restart {CONTAINER}")
    finally:
        conn.close()


async def update_worker(worker: dict) -> tuple:
    """Force the worker checkout to config.GIT_BRANCH's latest, rebuild the
    image, recreate the container. Robust to a worker stuck on the wrong branch
    (uses fetch + checkout -B FETCH_HEAD) and surfaces a build failure as a
    non-zero exit (so a silent old image isn't reported as success)."""
    conn = await _with_conn(worker, keepalive=False)  # long build: no keepalive
    try:
        br = config.GIT_BRANCH
        repo = config.GIT_REPO_URL
        cmd = (
            f"cd {REMOTE_DIR} && "
            # Repoint origin to the CURRENT repo first, so a worker cloned from
            # an older repo/branch is moved onto the active code with one click
            # from the panel (no manual SSH per worker ever again).
            f"git remote set-url origin '{repo}' && "
            f"git fetch --depth 1 origin {br} && "
            f"git checkout -B {br} FETCH_HEAD && "
            f"docker build --network=host -t {IMAGE} . && "
            f"(docker rm -f {CONTAINER} 2>/dev/null || true) && "
            f"docker run -d --name {CONTAINER} --restart always --network=host "
            f"--env-file {REMOTE_DIR}/.env -v {REMOTE_DATA}:/app/data {IMAGE}"
        )
        return await _run(conn, cmd)
    finally:
        conn.close()


async def teardown_worker(worker: dict):
    """Stop + remove the container and the checkout on the remote server."""
    await close_tunnel(worker["id"])
    try:
        conn = await _with_conn(worker, keepalive=False)
        try:
            await _run(conn, f"docker rm -f {CONTAINER} 2>/dev/null; rm -rf {REMOTE_DIR}")
        finally:
            conn.close()
    except Exception:
        pass  # best-effort cleanup; still remove from DB by caller


# --------------------------------------------------------------------------- #
# SSH tunnel to the worker's loopback-only API.
# --------------------------------------------------------------------------- #
def _lock_for(worker_id: int) -> asyncio.Lock:
    if worker_id not in _tunnel_locks:
        _tunnel_locks[worker_id] = asyncio.Lock()
    return _tunnel_locks[worker_id]


async def open_tunnel(worker: dict) -> int:
    """Open (or reuse) an SSH local-port-forward to the worker API.
    Returns the local port on the master that maps to the worker's API.
    """
    wid = worker["id"]
    async with _lock_for(wid):
        existing = _tunnels.get(wid)
        if existing:
            return existing["local_port"]
        conn = await _with_conn(worker)
        listener = await conn.forward_local_port(
            "127.0.0.1", 0, "127.0.0.1", int(worker["api_port"]),
        )
        local_port = listener.get_port()
        _tunnels[wid] = {"conn": conn, "listener": listener, "local_port": local_port}
        return local_port


async def close_tunnel(worker_id: int):
    t = _tunnels.pop(worker_id, None)
    if not t:
        return
    try:
        t["listener"].close()
    except Exception:
        pass
    try:
        t["conn"].close()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Persistent per-worker tunnel supervisor.
# Keeps a warm SSH tunnel to each REMOTE worker alive (asyncssh keepalive) and,
# when it dies, rebuilds it in the BACKGROUND with capped backoff + jitter.
# This restores the old "warm connection" behaviour: after startup a route is
# built ONCE and reused; api_call / the snapshot loop then hit an already-open
# tunnel (fail-fast) instead of paying a cold SSH connect every time.
# open_tunnel already serialises per-worker via _lock_for(wid), so only ONE
# connect happens per worker at a time (supervisor + any api_call share it).
# --------------------------------------------------------------------------- #
async def _supervisor_loop(worker_id: int):
    backoff = [5, 15, 30, 60]
    idx = 0
    while True:
        try:
            w = db.get_worker(worker_id)
            if not w or is_local(w) or not w.get("enabled"):
                return  # worker removed/disabled/became master -> stop
            await open_tunnel(w)          # reuse if warm, else connect (bounded)
            idx = 0                       # connected -> reset backoff
            t = _tunnels.get(worker_id)
            conn = t["conn"] if t else None
            if conn is None:
                raise RuntimeError("tunnel missing right after open")
            await conn.wait_closed()      # block until the SSH link dies
            await close_tunnel(worker_id)
            # loop immediately to reconnect (idx still 0 -> short first wait)
        except asyncio.CancelledError:
            raise
        except Exception:
            await close_tunnel(worker_id)
            base = backoff[min(idx, len(backoff) - 1)]
            idx += 1
            delay = base + random.uniform(0, base * 0.3)  # jitter
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise


def start_supervisor(worker: dict) -> None:
    """Ensure a live tunnel-supervisor task exists for one ENABLED remote worker."""
    if not worker or is_local(worker) or not worker.get("enabled"):
        return
    wid = worker["id"]
    t = _supervisors.get(wid)
    if t and not t.done():
        return
    _supervisors[wid] = asyncio.create_task(_supervisor_loop(wid))


async def stop_supervisor(worker_id: int) -> None:
    """Cancel a worker's supervisor and drop its warm tunnel."""
    t = _supervisors.pop(worker_id, None)
    if t and not t.done():
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    await close_tunnel(worker_id)


async def start_all_supervisors() -> None:
    """Start supervisors for every enabled remote worker (called at startup)."""
    for w in db.list_workers():
        if not is_local(w) and w.get("enabled"):
            start_supervisor(w)


async def prewarm_all() -> None:
    """Open tunnels to all enabled remote workers in parallel (each bounded by
    the connect timeout) so the FIRST health cycle after startup doesn't report
    false TIMEOUTs while cold connections are still being built."""
    ws = [w for w in db.list_workers() if not is_local(w) and w.get("enabled")]
    if not ws:
        return
    await asyncio.gather(*[open_tunnel(w) for w in ws], return_exceptions=True)


def snapshot_all() -> list:
    """Return the current in-memory health snapshots (no probing)."""
    return list(_health_cache.values())


# --------------------------------------------------------------------------- #
# API client (master -> worker, through the tunnel).
# --------------------------------------------------------------------------- #
async def api_call(worker: dict, method: str, path: str, payload: dict = None,
                   timeout: int = 120) -> dict:
    """Call the worker API. Raises on transport/HTTP error."""
    import httpx  # lazy
    local_port = await open_tunnel(worker)
    token = crypto_util.decrypt(worker["api_token_enc"])
    url = f"http://127.0.0.1:{local_port}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.request(method, url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except Exception:
        # a broken tunnel is the usual cause -> drop it so next call reopens
        await close_tunnel(worker["id"])
        raise


# --------------------------------------------------------------------------- #
# Health checks (parallel + cache).
# --------------------------------------------------------------------------- #
async def _tcp_ping(host: str, port: int, timeout: float = 5.0) -> int:
    """Return latency in ms to open a TCP connection, or -1 on failure."""
    start = time.monotonic()
    try:
        fut = asyncio.open_connection(host, int(port or 22))
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return int((time.monotonic() - start) * 1000)
    except Exception:
        return -1


def _mk_summary(worker: dict, status: str, ping: int, api_ok: bool,
                detail=None) -> dict:
    """Build + persist + cache one worker's health snapshot.

    NOTE: the ``file_ok`` field is kept for backward compatibility with the
    presentation helpers, but it now means "the worker API answered" (SSH +
    /ping), NOT "the worker can reach Rubika". Rubika reachability was removed
    from health on purpose so a transient Rubika 503 can't mark healthy workers
    as blocked. Worker selection relies on the existing send/login failover for
    a worker that momentarily can't reach Rubika."""
    wid = worker["id"]
    _health_detail[wid] = detail
    try:
        db.update_worker_health(wid, status, ping, api_ok)
    except Exception:
        pass
    summary = {"id": wid, "tag": worker["tag"], "ip": worker["ip"],
               "status": status, "ping_ms": ping, "file_ok": api_ok,
               "detail": detail, "ts": config.now_str(),
               "mono": time.monotonic()}
    _health_cache[wid] = summary
    return summary


async def check_worker(worker: dict, warm_only: bool = False) -> dict:
    """Measure one worker's health via SSH reachability + the worker API's
    instant /ping (NOT /health, which probes Rubika and is slow). Persists,
    caches, and returns a summary.

    warm_only=True (used by the background snapshot loop): never OPEN a new SSH
    tunnel; if no warm tunnel exists yet (supervisor still (re)connecting) the
    worker is reported as reconnecting instead of forcing a cold connect.
    """
    wid = worker["id"]
    if is_local(worker):
        # master runs jobs in-process: no SSH, no worker API, no Rubika probe.
        return _mk_summary(worker, "ok", 1, True, None)

    ping = await _tcp_ping(worker["ip"], worker["ssh_port"], timeout=3.0)
    if ping < 0:
        return _mk_summary(worker, "down", ping, False, "ssh unreachable")

    if warm_only and wid not in _tunnels:
        # supervisor is (re)establishing the tunnel; don't cold-connect here.
        return _mk_summary(worker, "blocked", ping, False, "reconnecting")

    try:
        # /ping is instant and does NO Rubika check.
        await api_call(worker, "GET", "/ping", timeout=8)
        return _mk_summary(worker, "ok", ping, True, None)
    except Exception as e:  # noqa: BLE001
        return _mk_summary(worker, "blocked", ping, False,
                           f"api error: {type(e).__name__}: {str(e)[:120]}")


async def check_all(workers: list = None, warm_only: bool = False) -> list:
    """Run health checks for all ENABLED workers IN PARALLEL, with an
    independent per-worker deadline so one slow/flaky worker can NEVER block
    the whole cycle (and thus the status card)."""
    if workers is None:
        workers = db.list_workers()
    probe = [w for w in workers if w.get("enabled")]  # skip Disabled entirely
    if not probe:
        return []

    async def _guarded(w):
        return await asyncio.wait_for(check_worker(w, warm_only=warm_only),
                                      timeout=15)

    results = await asyncio.gather(*[_guarded(w) for w in probe],
                                   return_exceptions=True)
    out = []
    for w, r in zip(probe, results):
        if isinstance(r, Exception):
            reason = ("timeout>15s" if isinstance(r, asyncio.TimeoutError)
                      else f"check crashed: {type(r).__name__}")
            out.append(_mk_summary(w, "down", -1, False, reason))
        else:
            out.append(r)
    return out


def health_detail(worker_id: int):
    """Last diagnostic reason for a worker being unhealthy (or None)."""
    return _health_detail.get(worker_id)


def cached_health(worker_id: int):
    return _health_cache.get(worker_id)


def is_healthy(worker: dict) -> bool:
    c = _health_cache.get(worker["id"])
    if c:
        return c["status"] == "ok"
    # never checked yet -> treat enabled workers as tentatively usable
    return bool(worker.get("enabled"))


# --------------------------------------------------------------------------- #
# Selection: round-robin with failover for a NEW account login.
# --------------------------------------------------------------------------- #
async def pick_worker_for_login(verify: bool = True, exclude_id=None) -> dict:
    """Choose the healthy enabled worker with the fewest accounts (= round-robin
    as accounts are added one at a time). Verifies health right before use.
    If exclude_id is given, that worker is NOT considered (used for "worker
    transfer": re-login the account on a DIFFERENT server than the current one).
    Returns a worker dict or None if none are usable.
    """
    # Make sure a master row exists (creates it once if missing), but routing
    # uses only ENABLED workers, so a disabled local master is respected.
    ensure_master_worker()
    workers = db.list_enabled_workers()
    if not workers:
        return None

    remotes = [w for w in workers if not is_local(w)]
    # Only spend time on health checks when there are real remote workers;
    # a local-only (master-as-worker) setup behaves exactly like before.
    if verify and remotes:
        await check_all(workers)
        workers = db.list_enabled_workers()  # reload fresh health

    def load(w):
        return db.count_accounts_on_worker(w["id"])

    # local master is always usable; remotes must be healthy ("ok").
    pool = [w for w in workers if (is_local(w) or w.get("status") == "ok")]
    if exclude_id is not None:
        pool = [w for w in pool if w["id"] != exclude_id]
    if not pool:
        return None
    pool.sort(key=lambda w: (load(w), w["id"]))
    return pool[0]


def worker_for_account(account: dict) -> dict:
    """The worker that owns an account (session affinity)."""
    wid = account.get("worker_id")
    if wid:
        return db.get_worker(int(wid))
    return db.get_master_worker()


# --------------------------------------------------------------------------- #
# Backup hook: pull each remote worker's session files into the zip.
# Called by bot.build_backup_archive() via _add_worker_sessions().
# --------------------------------------------------------------------------- #
async def collect_sessions_into_zip(zf):
    """Download every non-master worker's session files into zf under
    'sessions/<tag>/'. Best-effort; never raises out."""
    try:
        import asyncssh  # noqa: F401
    except ImportError:
        return
    for w in db.list_workers():
        if is_local(w):
            continue
        try:
            conn = await _with_conn(w, keepalive=False)
        except Exception:
            continue
        try:
            sftp = await conn.start_sftp_client()
            remote_sessions = f"{REMOTE_DATA}/sessions"
            try:
                names = await sftp.listdir(remote_sessions)
            except Exception:
                names = []
            for name in names:
                if name in (".", ".."):
                    continue
                rpath = f"{remote_sessions}/{name}"
                try:
                    data = await _read_remote_file(sftp, rpath)
                    safe_tag = w["tag"].replace("#", "").replace("/", "_")
                    zf.writestr(f"sessions/{safe_tag}/{name}", data)
                except Exception:
                    continue
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


async def _read_remote_file(sftp, path: str) -> bytes:
    async with sftp.open(path, "rb") as f:
        return await f.read()


async def shutdown():
    """Cancel supervisors + close all open tunnels (call on master shutdown)."""
    for wid in list(_supervisors.keys()):
        await stop_supervisor(wid)
    for wid in list(_tunnels.keys()):
        await close_tunnel(wid)
