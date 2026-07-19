"""
Worker API node (MODE=worker).
==============================

A headless FastAPI service that the master reaches over an SSH tunnel. It
executes login / send jobs by calling the EXISTING, UNCHANGED functions in
`rubika_client.py`. There is no Telegram panel here — this process only takes
orders from the master.

Endpoints (all require `Authorization: Bearer <WORKER_API_TOKEN>`):
  GET  /health                 -> {file_ok, status_code}     (Rubika route check)
  POST /login/start            -> {ok, needs_password, needs_code, status}
  POST /login/password         -> {ok, needs_code}
  POST /login/code             -> {ok, name, guid, contacts, groups, with_chat}
  POST /send/start             -> {ok, job_id, total, marker_found}
  GET  /send/status/{job_id}   -> {ok, fail, total, done, stopped, reason}
  POST /send/stop/{job_id}     -> {stopped: true}

It binds to loopback only; the master maps a local port to it via SSH, so the
API is never exposed to the public internet.
"""
import asyncio
import random
import uuid

import account_conn
import config
import rubika_client as rb

# In worker mode we only need FastAPI + uvicorn + httpx; import lazily so this
# file can still be byte-compiled on the master without those installed.
try:
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel
    _HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    FastAPI = object  # type: ignore
    Header = None  # type: ignore
    HTTPException = Exception  # type: ignore
    BaseModel = object  # type: ignore
    _HAVE_FASTAPI = False


# --------------------------------------------------------------------------- #
# In-memory state (lives only inside this worker process).
# --------------------------------------------------------------------------- #
_login_ctx: dict = {}   # phone -> rubpy login context dict
_jobs: dict = {}        # job_id -> job state dict
_automations: dict = {}  # phone -> automation state dict
_secretaries: dict = {}  # phone -> secretary state dict
_channelreports: dict = {}  # phone -> channel-report state dict
_replies: dict = {}     # phone -> reply-responder state dict
_linkdoonis: dict = {}  # phone -> linkdooni send-loop state dict (YoudonoaAx Item 3)
_extras_logs: list = []  # queued log-card strings; drained by the master

LINE = "━━━━━━━━━━━━━━━━"


def _now() -> str:
    return config.now_str()


def _wcard(title: str, rows: list) -> str:
    rows = [r for r in rows if r is not None]
    return f"{title}\n{LINE}\n" + "\n".join(rows)


def _qlog(text: str):
    """Queue a log card for the master to drain via GET /extras/logs."""
    _extras_logs.append(text)
    if len(_extras_logs) > 500:           # keep the queue bounded
        del _extras_logs[:len(_extras_logs) - 500]


def _worker_code_version() -> str:
    """Short git revision of this worker's code (YoudonoaAx UPDATE, step 6),
    reported via /health so the master can show a versions table. Best-effort."""
    import os
    base = os.path.dirname(os.path.abspath(__file__))
    # 1) the git binary (works on the master/host where git is installed)
    try:
        import subprocess
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=base, capture_output=True, text=True, timeout=10)
        rev = (out.stdout or "").strip()
        if rev:
            return rev
    except Exception:  # noqa: BLE001
        pass
    # 2) inside the Docker worker `git` is NOT installed, but the `.git` dir IS
    #    copied into the image — so read the commit straight from .git (no binary).
    try:
        git_dir = os.path.join(base, ".git")
        head = open(os.path.join(git_dir, "HEAD"), encoding="utf-8").read().strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            ref_path = os.path.join(git_dir, ref)
            sha = ""
            if os.path.exists(ref_path):
                sha = open(ref_path, encoding="utf-8").read().strip()
            else:                                   # packed-refs fallback
                pr = os.path.join(git_dir, "packed-refs")
                if os.path.exists(pr):
                    for line in open(pr, encoding="utf-8"):
                        line = line.strip()
                        if line and not line.startswith(("#", "^")) and line.endswith(ref):
                            sha = line.split(" ", 1)[0]
                            break
        else:
            sha = head                              # detached HEAD = raw sha
        if sha:
            return sha[:7]
    except Exception:  # noqa: BLE001
        pass
    return "—"


async def _worker_on_invalid(phone: str):
    _qlog(_wcard("🔐 INVALID_AUTH", [
        f"👤 Account : {phone}",
        "سشن این اکانت باطل شده — باید دوباره لاگین شه (باگ نیست).",
        f"🕒 {_now()}"]))


async def _handle_auth_error(phone: str) -> bool:
    """Confirm a suspected dead session with a FRESH connection before declaring
    it dead. Returns True if the loop should stop (truly dead), False if it was
    a transient error (banned/muted group, hiccup) and the loop should go on."""
    try:
        dead = await account_conn.verify_session_dead(phone)
    except Exception:
        dead = False
    if dead:
        await account_conn.notify_invalid(phone)
        return True
    return False


async def _apply_profile(client, first, last, bio):
    """Update name/bio only if different from current. Returns True if changed."""
    cur = await rb.get_my_profile(client)
    same = ((cur.get("first_name") or "") == first
            and (cur.get("last_name") or "") == last
            and (cur.get("bio") or "") == bio)
    if same:
        return False
    await rb.update_profile(client, first_name=first, last_name=last, bio=bio)
    return True


def _build_app():
    app = FastAPI(title="V2Rubby Worker", docs_url=None, redoc_url=None)

    def _auth(authorization: str):
        expected = config.WORKER_API_TOKEN
        if not expected:
            raise HTTPException(status_code=500, detail="worker token not configured")
        if not authorization or authorization != f"Bearer {expected}":
            raise HTTPException(status_code=401, detail="unauthorized")

    # ----- request models -----
    class StartLogin(BaseModel):
        phone: str
        pass_key: str = None

    class CodeIn(BaseModel):
        phone: str
        code: str

    class PasswordIn(BaseModel):
        phone: str
        password: str

    class SendIn(BaseModel):
        phone: str
        marker: str
        delay: float = 1.0
        max_errors: int = 3
        send_timeout: int = 60
        resume_wait: int = 300
        max_retries: int = 2
        text2: str = ""          # step 5: optional Rubika second text (always text)
        recipients: list = []    # resume fix: explicit remaining list (frozen order)

    class AutomationIn(BaseModel):
        phone: str
        texts: list = []
        interval: int = 30

    class GroupJoinIn(BaseModel):
        phone: str
        links: list = []

    class PrepareIn(BaseModel):
        phone: str
        marker: str

    class PhoneIn(BaseModel):
        phone: str

    class SessionImportIn(BaseModel):
        phone: str
        auth: str = None
        private_key: str = None
        guid: str = None
        user_agent: str = None

    class GroupLeaveIn(BaseModel):
        phone: str
        group_guid: str

    class GenCreateIn(BaseModel):
        phone: str
        kind: str = "channel"
        title: str

    class GenJoinIn(BaseModel):
        phone: str
        username: str

    class GenAdminIn(BaseModel):
        phone: str
        object_guid: str
        user_guid: str

    class GenLinkIn(BaseModel):
        phone: str
        object_guid: str

    class GenSeedIn(BaseModel):
        phone: str
        kind: str = "channel"
        object_guid: str
        target: int = 300
        batch: int = 80
        delay: float = 2.0
        exclude: list = []

    class GenSelfIn(BaseModel):
        phone: str

    class BroadcastIn(BaseModel):
        phone: str
        title: str
        username_seed: str = "ch"
        marker: str = ""
        member_target: int = 300

    class PvExportIn(BaseModel):
        phone: str
        max_chats: int = 1000
        max_photos: int = 2000

    class ContactsAddIn(BaseModel):
        phone: str
        numbers: list = []
        delay: float = 1.0
        default_first: str = "Friend"

    class SendListIn(BaseModel):
        phone: str
        marker: str
        guids: list = []
        delay: float = 1.0
        max_errors: int = 5
        send_timeout: int = 60
        mode: str = "marker"      # 'marker' (forward) or 'text' (send_text)
        text: str = ""
        text2: str = ""          # step 5: optional Rubika second text (always text)
        order: bool = False       # reorder guids: chat-first -> online -> last-seen

    class LinkdooniIn(BaseModel):
        phone: str
        group_guids: list = []
        texts: list = []
        interval: int = 1800

    class LinkdooniExtractIn(BaseModel):
        phone: str
        channels: list = []
        scan: int = 100

    class ChannelCreateIn(BaseModel):
        phone: str
        marker: str
        title: str

    class ChannelAddIn(BaseModel):
        phone: str
        channel_guid: str
        target: int = 300
        batch: int = 80
        delay: float = 2.0

    class SecretaryIn(BaseModel):
        phone: str
        mode: str = "marker"
        text: str = ""
        marker: str = ""
        interval: int = 600

    class ChannelReportIn(BaseModel):
        phone: str
        channel_guid: str = ""
        channel_title: str = ""
        interval: int = 600

    class ReplyIn(BaseModel):
        phone: str
        text: str = ""
        delay: float = 2.0

    class ProfileIn(BaseModel):
        phone: str
        first_name: str = ""
        last_name: str = ""
        bio: str = ""

    @app.on_event("startup")
    async def _startup():
        account_conn.set_invalid_auth_handler(_worker_on_invalid)
        account_conn.start_janitor()

    # ----- ping (NO token; just proves the API process is alive) -----
    @app.get("/ping")
    async def ping():
        return {"ok": True, "service": "v2rubby-worker"}

    # ----- health -----
    @app.get("/health")
    async def health(authorization: str = Header(None)):
        _auth(authorization)
        import httpx
        code = None
        file_ok = False
        try:
            async with httpx.AsyncClient(timeout=config.HEALTH_TIMEOUT) as c:
                r = await c.get(config.HEALTH_URL)
                code = r.status_code
                file_ok = code in (200, 404)
        except Exception:
            file_ok = False
        return {"file_ok": file_ok, "status_code": code, "version": _worker_code_version()}

    # ----- login relay -----
    @app.post("/login/start")
    async def login_start(body: StartLogin, authorization: str = Header(None)):
        _auth(authorization)
        ctx = await rb.start_login(body.phone, pass_key=body.pass_key)
        _login_ctx[rb.normalize_phone(body.phone)] = ctx
        status = str(ctx.get("status") or "").upper()
        needs_password = "PASS" in status
        needs_code = (not needs_password) and bool(ctx.get("phone_code_hash"))
        return {"ok": True, "status": status,
                "needs_password": needs_password, "needs_code": needs_code}

    @app.post("/login/password")
    async def login_password(body: PasswordIn, authorization: str = Header(None)):
        _auth(authorization)
        ctx = await rb.start_login(body.phone, pass_key=body.password)
        _login_ctx[rb.normalize_phone(body.phone)] = ctx
        return {"ok": True, "needs_code": bool(ctx.get("phone_code_hash"))}

    @app.post("/login/code")
    async def login_code(body: CodeIn, authorization: str = Header(None)):
        _auth(authorization)
        key = rb.normalize_phone(body.phone)
        ctx = _login_ctx.get(key)
        if not ctx:
            raise HTTPException(status_code=400, detail="no login in progress")
        code = "".join(ch for ch in body.code if ch.isdigit())
        await rb.finish_login(ctx, code)
        client = ctx["client"]
        try:
            me = await client.get_me()
            guid = rb._guid_of(me) or "-"
            name = rb._name_of(me)
            _ordered, stats = await rb.get_ordered_recipients(client)
            return {"ok": True, "name": name, "guid": str(guid),
                    "contacts": stats["contacts"], "groups": stats["groups"],
                    "with_chat": stats["with_chat"], "phone": ctx["phone"],
                    # v4: portable session values so the master can store the
                    # session and re-import it onto any worker WITHOUT a code.
                    "auth": getattr(client, "auth", None),
                    "private_key": getattr(client, "private_key", None),
                    "user_agent": getattr(client, "user_agent", None)}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
            _login_ctx.pop(key, None)

    # ----- send -----
    @app.post("/prepare")
    async def prepare(body: PrepareIn, authorization: str = Header(None)):
        _auth(authorization)
        await account_conn.close(body.phone)   # ensure single connection (Feature 6)
        client = rb.open_client(body.phone)
        try:
            await rb.connect_ready(client)
            saved_guid, mid = await rb.find_marked_message(client, body.marker)
            if not mid:
                return {"ok": True, "marker_found": False, "total": 0}
            ordered, _stats = await rb.get_ordered_recipients(client)
            return {"ok": True, "marker_found": True, "total": len(ordered)}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ----- channel send mode -----
    @app.post("/channel/create")
    async def channel_create(body: ChannelCreateIn, authorization: str = Header(None)):
        _auth(authorization)
        await account_conn.close(body.phone)   # ensure single connection (Feature 6)
        client = rb.open_client(body.phone)
        try:
            await rb.connect_ready(client)
            saved_guid, mid = await rb.find_marked_message(client, body.marker)
            channel_guid = await rb.create_channel(client, body.title)
            forwarded = False
            if mid:
                try:
                    await rb.forward_message(client, saved_guid, channel_guid, mid)
                    forwarded = True
                except Exception:
                    forwarded = False
            return {"ok": True, "channel_guid": channel_guid,
                    "marker_found": bool(mid), "forwarded": forwarded}
        except Exception as e:  # noqa: BLE001
            # no-500: surface the REAL reason to the master instead of a raw 500
            return {"ok": False, "error": repr(e)[:200]}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @app.post("/channel/add")
    async def channel_add(body: ChannelAddIn, authorization: str = Header(None)):
        _auth(authorization)
        await account_conn.close(body.phone)   # ensure single connection (Feature 6)
        client = rb.open_client(body.phone)
        try:
            await rb.connect_ready(client)
            added = await rb.seed_channel_with_contacts(
                client, body.channel_guid, target=body.target,
                batch=body.batch, delay=body.delay)
            return {"ok": True, "added": added}
        except Exception as e:  # noqa: BLE001
            # no-500: surface the REAL reason to the master instead of a raw 500
            return {"ok": False, "error": repr(e)[:200]}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @app.post("/send/start")
    async def send_start(body: SendIn, authorization: str = Header(None)):
        _auth(authorization)
        await account_conn.close(body.phone)   # ensure single connection (Feature 6)
        client = rb.open_client(body.phone)
        await rb.connect_ready(client)
        saved_guid, mid = await rb.find_marked_message(client, body.marker)
        if not mid:
            try:
                await client.disconnect()
            except Exception:
                pass
            return {"ok": False, "marker_found": False, "total": 0}
        # YoudonoaAx UPDATE (resume fix): if the master supplies an explicit
        # recipient list (a resume / worker-transfer of the REMAINING list),
        # send EXACTLY that list in the given order. Otherwise build the full
        # ordered list as before.
        if getattr(body, "recipients", None):
            recipients = list(body.recipients)
        else:
            ordered, _stats = await rb.get_ordered_recipients(client)
            recipients = [r["guid"] for r in ordered]

        job_id = uuid.uuid4().hex[:12]
        job = {"phone": body.phone, "total": len(recipients), "ok": 0, "fail": 0,
               "done": False, "stopped": False, "reason": None,
               "retry_count": 0, "state": "sending"}
        _jobs[job_id] = job
        asyncio.create_task(_run_send(client, job, saved_guid, mid, recipients, body))
        # return the EXACT ordered guids so the master can mirror the list and
        # compute the remaining slice (guids[ok+fail:]) for a precise resume.
        return {"ok": True, "marker_found": True, "job_id": job_id,
                "total": len(recipients), "guids": recipients}

    @app.get("/send/status/{job_id}")
    async def send_status(job_id: str, authorization: str = Header(None)):
        _auth(authorization)
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.post("/send/stop/{job_id}")
    async def send_stop(job_id: str, authorization: str = Header(None)):
        _auth(authorization)
        job = _jobs.get(job_id)
        if job:
            job["stopped"] = True
        return {"stopped": True}

    # ----- automation (rotating texts to the account's groups) -----
    @app.post("/automation/start")
    async def automation_start(body: AutomationIn, authorization: str = Header(None)):
        _auth(authorization)
        # idempotent: stop any existing loop for this phone first
        await _stop_automation(body.phone)
        state = {"stop": False, "sent": 0, "groups": 0, "skipped": set(),
                 "texts": list(body.texts or []),
                 "interval": config.clamp_interval(body.interval), "task": None}
        if not state["texts"]:
            return {"ok": False, "error": "no texts"}
        state["task"] = asyncio.create_task(_run_automation(body.phone, state))
        _automations[rb.normalize_phone(body.phone)] = state
        return {"ok": True}

    @app.post("/automation/stop")
    async def automation_stop(body: AutomationIn, authorization: str = Header(None)):
        _auth(authorization)
        sent = await _stop_automation(body.phone)
        return {"ok": True, "sent": sent}

    @app.get("/automation/status")
    async def automation_status(phone: str, authorization: str = Header(None)):
        _auth(authorization)
        st = _automations.get(rb.normalize_phone(phone))
        if not st:
            return {"running": False, "sent": 0, "groups": 0, "skipped": 0}
        return {"running": not st["stop"], "sent": st["sent"],
                "groups": st["groups"], "skipped": len(st["skipped"])}

    @app.post("/group/join")
    async def group_join(body: GroupJoinIn, authorization: str = Header(None)):
        _auth(authorization)
        await account_conn.close(body.phone)   # ensure single connection (Feature 6)
        client = rb.open_client(body.phone)
        joined = 0
        failed = 0
        joined_links = []
        try:
            await rb.connect_ready(client)
            for link in (body.links or []):
                try:
                    await asyncio.wait_for(rb.join_group_by_link(client, link), timeout=60)
                    joined += 1
                    joined_links.append(link)
                except Exception:
                    failed += 1
                await asyncio.sleep(config.GROUP_JOIN_DELAY)
            return {"ok": True, "joined": joined, "failed": failed,
                    "joined_links": joined_links}
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    # ----- linkdooni engine: scheduled send to a SPECIFIC set of groups -----
    @app.post("/linkdooni/start")
    async def linkdooni_start(body: LinkdooniIn, authorization: str = Header(None)):
        _auth(authorization)
        await _stop_linkdooni(body.phone)
        if not body.texts or not body.group_guids:
            return {"ok": False, "error": "no texts or no groups"}
        st = {"stop": False, "sent": 0, "task": None,
              "guids": list(body.group_guids),
              "texts": list(body.texts),
              "interval": config.clamp_linkdooni_interval(body.interval)}
        st["task"] = asyncio.create_task(_run_linkdooni(body.phone, st))
        _linkdoonis[rb.normalize_phone(body.phone)] = st
        return {"ok": True}

    @app.post("/linkdooni/stop")
    async def linkdooni_stop(body: LinkdooniIn, authorization: str = Header(None)):
        _auth(authorization)
        sent = await _stop_linkdooni(body.phone)
        return {"ok": True, "sent": sent}

    @app.get("/linkdooni/status")
    async def linkdooni_status(phone: str, authorization: str = Header(None)):
        _auth(authorization)
        st = _linkdoonis.get(rb.normalize_phone(phone))
        if not st:
            return {"running": False, "sent": 0}
        return {"running": not st["stop"], "sent": st["sent"]}

    @app.post("/linkdooni/extract")
    async def linkdooni_extract(body: LinkdooniExtractIn,
                                authorization: str = Header(None)):
        _auth(authorization)
        async def _do(client):
            links = []
            for ch in (body.channels or []):
                try:
                    guid, _title = await asyncio.wait_for(
                        rb.resolve_channel(client, ch), timeout=60)
                except Exception:
                    continue
                try:
                    msgs = await asyncio.wait_for(
                        rb.get_recent_messages(client, guid, body.scan), timeout=90)
                except Exception:
                    msgs = []
                for m in msgs:
                    for lk in rb.extract_group_links(rb._msg_text_of(m)):
                        if lk not in links:
                            links.append(lk)
            return links
        try:
            links = await account_conn.call(body.phone, _do, timeout=600)
            return {"ok": True, "links": links}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "links": [], "error": repr(e)[:200]}


    # ----- automation EXTRAS: secretary / channel report / reply / profile ----
    @app.post("/secretary/start")
    async def secretary_start(body: SecretaryIn, authorization: str = Header(None)):
        _auth(authorization)
        await _stop_secretary(body.phone)
        st = {"stop": False, "mode": body.mode or "marker", "text": body.text or "",
              "marker": body.marker or "", "primed": False, "state": "",
              "replied_users": set(), "self_guid": None, "sent": 0, "task": None,
              "interval": config.clamp_secretary_interval(body.interval)}
        st["task"] = asyncio.create_task(_run_secretary(body.phone, st))
        _secretaries[rb.normalize_phone(body.phone)] = st
        return {"ok": True}

    @app.post("/secretary/stop")
    async def secretary_stop(body: SecretaryIn, authorization: str = Header(None)):
        _auth(authorization)
        await _stop_secretary(body.phone)
        return {"ok": True}

    @app.get("/secretary/status")
    async def secretary_status(phone: str, authorization: str = Header(None)):
        _auth(authorization)
        st = _secretaries.get(rb.normalize_phone(phone))
        if not st:
            return {"running": False, "sent": 0}
        return {"running": not st["stop"], "sent": st["sent"]}

    @app.post("/channelreport/start")
    async def channelreport_start(body: ChannelReportIn, authorization: str = Header(None)):
        _auth(authorization)
        await _stop_channelreport(body.phone)
        st = {"stop": False, "channel_guid": body.channel_guid or "",
              "channel_title": body.channel_title or "", "task": None,
              "interval": config.clamp_channel_report_interval(body.interval)}
        st["task"] = asyncio.create_task(_run_channelreport(body.phone, st))
        _channelreports[rb.normalize_phone(body.phone)] = st
        return {"ok": True}

    @app.post("/channelreport/stop")
    async def channelreport_stop(body: ChannelReportIn, authorization: str = Header(None)):
        _auth(authorization)
        await _stop_channelreport(body.phone)
        return {"ok": True}

    @app.get("/channelreport/status")
    async def channelreport_status(phone: str, authorization: str = Header(None)):
        _auth(authorization)
        st = _channelreports.get(rb.normalize_phone(phone))
        if not st:
            return {"running": False}
        return {"running": not st["stop"]}

    @app.post("/reply/start")
    async def reply_start(body: ReplyIn, authorization: str = Header(None)):
        _auth(authorization)
        await _stop_reply(body.phone)
        st = {"stop": False, "text": body.text or "",
              "delay": config.clamp_reply_delay(body.delay), "primed": False,
              "state": "", "self_guid": None, "done": set(), "sent": 0, "task": None}
        st["task"] = asyncio.create_task(_run_reply(body.phone, st))
        _replies[rb.normalize_phone(body.phone)] = st
        return {"ok": True}

    @app.post("/reply/stop")
    async def reply_stop(body: ReplyIn, authorization: str = Header(None)):
        _auth(authorization)
        await _stop_reply(body.phone)
        return {"ok": True}

    @app.get("/reply/status")
    async def reply_status(phone: str, authorization: str = Header(None)):
        _auth(authorization)
        st = _replies.get(rb.normalize_phone(phone))
        if not st:
            return {"running": False, "sent": 0}
        return {"running": not st["stop"], "sent": st["sent"]}

    @app.post("/profile/update")
    async def profile_update(body: ProfileIn, authorization: str = Header(None)):
        _auth(authorization)
        changed = await account_conn.call(body.phone, _apply_profile,
                                          body.first_name, body.last_name, body.bio,
                                          timeout=60)
        return {"ok": True, "changed": bool(changed)}

    @app.post("/account/verify")
    async def account_verify(body: PhoneIn, authorization: str = Header(None)):
        _auth(authorization)
        dead = await account_conn.verify_session_dead(body.phone)
        return {"ok": True, "dead": bool(dead)}

    @app.post("/session/import")
    async def session_import(body: SessionImportIn, authorization: str = Header(None)):
        _auth(authorization)
        # v4: WRITE the portable session onto this worker's local session store
        # WITHOUT connecting. session.insert only writes the session file, so it
        # can NEVER cause AUTH_FROM_ANOTHER (no second live connection). The
        # account connects later only when a real job runs (coordinated by
        # account_conn / active_jobs — single connection at a time).
        if not body.auth:
            return {"ok": False, "error": "missing auth"}
        try:
            phone = rb.normalize_phone(body.phone)
            # make sure no warm connection is fighting the file (Feature 6)
            try:
                await account_conn.close(phone)
            except Exception:
                pass
            client = rb.open_client(phone)
            client.session.insert(
                auth=body.auth,
                guid=body.guid,
                user_agent=body.user_agent,
                phone_number=phone,
                private_key=body.private_key,
            )
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)[:200]}

    @app.post("/group/leave")
    async def group_leave(body: GroupLeaveIn, authorization: str = Header(None)):
        _auth(authorization)
        try:
            await account_conn.call(body.phone, rb.leave_group, body.group_guid,
                                    timeout=60)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)[:160]}

    # ----- generator engine (موتور مولد) -----
    @app.post("/gen/self")
    async def gen_self(body: GenSelfIn, authorization: str = Header(None)):
        _auth(authorization)
        guid = await account_conn.call(body.phone, rb.get_self_guid, timeout=30)
        return {"ok": True, "guid": guid}

    @app.post("/gen/create")
    async def gen_create(body: GenCreateIn, authorization: str = Header(None)):
        _auth(authorization)
        async def _do(client):
            guid = await rb.create_channel(client, body.title)
            username = ""
            try:
                username = await rb.assign_random_channel_username(client, guid)
            except Exception:
                username = ""
            self_guid = await rb.get_self_guid(client)
            return {"object_guid": guid, "username": username,
                    "creator_guid": self_guid}
        res = await account_conn.call(body.phone, _do, timeout=120)
        return {"ok": True, **res}

    @app.post("/gen/join")
    async def gen_join(body: GenJoinIn, authorization: str = Header(None)):
        _auth(authorization)
        async def _do(client):
            return await rb.join_channel_by_username(client, body.username)
        try:
            guid = await account_conn.call(body.phone, _do, timeout=90)
            return {"ok": True, "guid": guid}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)[:160]}

    @app.post("/gen/is_admin")
    async def gen_is_admin(body: GenAdminIn, authorization: str = Header(None)):
        _auth(authorization)
        try:
            ok = await account_conn.call(body.phone, rb.user_is_admin,
                                         body.object_guid, body.user_guid, timeout=60)
            return {"ok": True, "is_admin": bool(ok)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "is_admin": False, "error": repr(e)[:160]}

    @app.post("/gen/seed")
    async def gen_seed(body: GenSeedIn, authorization: str = Header(None)):
        _auth(authorization)
        async def _do(client):
            return await rb.seed_object_with_contacts(
                client, body.kind, body.object_guid, target=body.target,
                batch=body.batch, delay=body.delay, exclude=set(body.exclude or []))
        try:
            added = await account_conn.call(body.phone, _do, timeout=900)
            return {"ok": True, "added": added}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "added": 0, "error": repr(e)[:160]}

    @app.post("/broadcast/run")
    async def broadcast_run(body: BroadcastIn, authorization: str = Header(None)):
        _auth(authorization)
        async def _do(client):
            guid = await rb.create_channel(client, body.title)
            username = ""
            try:
                username = await rb.assign_random_channel_username(client, guid)
            except Exception:
                username = ""
            forwarded = False
            try:
                saved_guid, mid = await rb.find_marked_message(client, body.marker)
                if mid:
                    await rb.forward_message(client, saved_guid, guid, mid)
                    forwarded = True
            except Exception:
                forwarded = False
            added = 0
            try:
                added = await rb.seed_channel_with_contacts(
                    client, guid, target=body.member_target,
                    batch=config.CHANNEL_ADD_BATCH, delay=config.CHANNEL_ADD_DELAY)
            except Exception:
                added = 0
            return {"object_guid": guid, "username": username,
                    "forwarded": forwarded, "added": added}
        try:
            res = await account_conn.call(body.phone, _do, timeout=900)
            return {"ok": True, **res}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": repr(e)[:200]}

    @app.post("/pvexport/run")
    async def pvexport_run(body: PvExportIn, authorization: str = Header(None)):
        _auth(authorization)
        # download all PV photos -> return them base64 so the master builds the PDF
        import base64
        async def _do(client):
            out = []
            guids = await rb.get_chat_list_guids(client, only_users=True)
            for g in guids[:body.max_chats]:
                async for _mid, fi in rb.iter_chat_photos(client, g):
                    try:
                        blob = await rb.download_photo(client, fi)
                        if blob:
                            out.append(base64.b64encode(blob).decode())
                    except Exception:
                        continue
                    if len(out) >= body.max_photos:
                        return out
            return out
        try:
            photos = await account_conn.call(body.phone, _do, timeout=1800)
            return {"ok": True, "photos_b64": photos, "count": len(photos)}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "count": 0, "error": repr(e)[:200]}

    @app.post("/contacts/add")
    async def contacts_add(body: ContactsAddIn, authorization: str = Header(None)):
        _auth(authorization)
        # add a list of phone numbers to the account's contacts, with the same
        # "5 consecutive errors -> pause -> resume" protection used for sends.
        async def _do(client):
            added = 0        # number is on Rubika (real contact)
            not_user = 0     # added to address book but no Rubika account
            failed = 0
            guids = []
            attempt_fail = 0
            for raw in (body.numbers or []):
                ph = rb.normalize_phone(str(raw))
                if not ph:
                    continue
                try:
                    r = await asyncio.wait_for(
                        rb.add_contact(client, ph, body.default_first or "Friend"),
                        timeout=config.SEND_TIMEOUT)
                    attempt_fail = 0
                    if r.get("on_rubika"):
                        added += 1
                        if r.get("guid"):
                            guids.append(r["guid"])
                    else:
                        not_user += 1
                except Exception:
                    failed += 1
                    attempt_fail += 1
                    if attempt_fail >= config.MAX_ERRORS:
                        await asyncio.sleep(config.RESUME_WAIT)
                        attempt_fail = 0
                await asyncio.sleep(max(0.0, float(body.delay)))
            return {"added": added, "not_user": not_user,
                    "failed": failed, "guids": guids}
        try:
            res = await account_conn.call(body.phone, _do, timeout=7200)
            return {"ok": True, **res}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "added": 0, "not_user": 0, "failed": 0,
                    "guids": [], "error": repr(e)[:200]}

    @app.post("/send/to_list")
    async def send_to_list(body: SendListIn, authorization: str = Header(None)):
        _auth(authorization)
        # forward the marked message to a SPECIFIC list of guids (used by the
        # brain to message only the freshly-added contacts). Same 5-consecutive
        # -> pause -> resume protection.
        async def _do(client):
            mode = (body.mode or "marker").lower()
            saved_guid = mid = None
            if mode != "text":
                saved_guid, mid = await rb.find_marked_message(client, body.marker or "")
                if not mid:
                    return {"marker_found": False, "sent": 0, "fail": 0}
            guids = list(body.guids or [])
            # order like a normal send (chat-first -> online -> last-seen) when asked
            if getattr(body, "order", False) and guids:
                try:
                    ordered, _st = await rb.get_ordered_recipients(client)
                    rank = {g: i for i, g in enumerate(ordered)}
                    guids = sorted(guids, key=lambda g: rank.get(g, 10 ** 9))
                except Exception:  # noqa: BLE001
                    pass
            ok = 0
            fail = 0
            attempt_fail = 0
            for g in guids:
                try:
                    if mode == "text":
                        await asyncio.wait_for(
                            rb.send_text(client, g, body.text or ""),
                            timeout=body.send_timeout)
                    else:
                        await asyncio.wait_for(
                            rb.forward_message(client, saved_guid, g, mid),
                            timeout=body.send_timeout)
                    # step 5: optional second text (always text) to the SAME
                    # recipient right after the main send — mirrors the local
                    # run_send. Best-effort: a failure must NOT undo the send.
                    if getattr(body, "text2", ""):
                        try:
                            await asyncio.wait_for(
                                rb.send_text(client, g, body.text2),
                                timeout=body.send_timeout)
                        except Exception:  # noqa: BLE001
                            pass
                    ok += 1
                    attempt_fail = 0
                except Exception:
                    fail += 1
                    attempt_fail += 1
                    if attempt_fail >= body.max_errors:
                        await asyncio.sleep(config.RESUME_WAIT)
                        attempt_fail = 0
                await asyncio.sleep(max(0.0, float(body.delay)))
            return {"marker_found": True, "sent": ok, "fail": fail}
        try:
            res = await account_conn.call(body.phone, _do, timeout=7200)
            return {"ok": True, **res}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "sent": 0, "fail": 0, "error": repr(e)[:200]}

    @app.get("/extras/logs")
    async def extras_logs(authorization: str = Header(None)):
        _auth(authorization)
        global _extras_logs
        logs = _extras_logs
        _extras_logs = []
        return {"logs": logs}

    return app


async def _stop_automation(phone: str) -> int:
    """Stop a running automation for a phone; return how many it had sent."""
    st = _automations.pop(rb.normalize_phone(phone), None)
    if not st:
        return 0
    st["stop"] = True
    task = st.get("task")
    if task:
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:
            task.cancel()
    return st.get("sent", 0)


def _pick_text(texts: list, last_idx):
    """Random text index, avoiding the same one as last time (if possible)."""
    if not texts:
        return None, None
    if len(texts) == 1:
        return 0, texts[0]
    choices = [i for i in range(len(texts)) if i != last_idx]
    i = random.choice(choices)
    return i, texts[i]


async def _run_automation(phone: str, state: dict):
    """Worker-side automation loop — ONE connection per pass (Feature 6), the
    same open->work->close shape as the original source. A group is muted only
    after 3 failures in a row; if everything gets muted we reset to recover."""
    fails: dict = {}
    last_text: dict = {}
    try:
        while not state["stop"]:
            try:
                async with account_conn.connection(phone) as client:
                    try:
                        groups = await asyncio.wait_for(
                            rb.get_group_guids(client), timeout=60)
                    except Exception:
                        groups = []
                        account_conn.drop_connection(phone)
                    state["groups"] = len(groups)
                    for g in groups:
                        if state["stop"]:
                            break
                        guid = g["guid"]
                        if guid in state["skipped"]:
                            continue
                        idx, txt = _pick_text(state["texts"], last_text.get(guid))
                        if txt is None:
                            break
                        try:
                            await asyncio.wait_for(
                                rb.send_text(client, guid, txt),
                                timeout=config.SEND_TIMEOUT)
                            state["sent"] += 1
                            last_text[guid] = idx
                            fails[guid] = 0
                        except Exception:
                            # ANY send failure -> count against THIS group, mute
                            # after 3 strikes. Never declare the account dead and
                            # never open a second connection to "verify".
                            fails[guid] = fails.get(guid, 0) + 1
                            if fails[guid] >= 3:
                                state["skipped"].add(guid)
                        await asyncio.sleep(random.uniform(
                            config.AUTOMATION_GROUP_DELAY_MIN,
                            config.AUTOMATION_GROUP_DELAY_MAX))
                    if groups and all(g["guid"] in state["skipped"] for g in groups):
                        state["skipped"].clear()
                        fails.clear()
                        account_conn.drop_connection(phone)
            except Exception:
                # whole-pass error -> drop connection, continue next round.
                account_conn.drop_connection(phone)
            waited = 0
            while waited < state["interval"] and not state["stop"]:
                await asyncio.sleep(1)
                waited += 1
    except Exception:
        pass


async def _state_sleep(st: dict, seconds: float):
    waited = 0.0
    while waited < seconds and not st.get("stop"):
        await asyncio.sleep(1.0)
        waited += 1.0


async def _stop_linkdooni(phone: str) -> int:
    st = _linkdoonis.pop(rb.normalize_phone(phone), None)
    if not st:
        return 0
    st["stop"] = True
    task = st.get("task")
    if task:
        try:
            await asyncio.wait_for(task, timeout=10)
        except Exception:
            task.cancel()
    return st.get("sent", 0)


async def _run_linkdooni(phone: str, state: dict):
    """Worker-side linkdooni loop — ONE connection per pass. Sends a random
    configured text to ONLY the assigned group guids every interval. Logs are
    queued for the master to drain."""
    last_text: dict = {}
    try:
        while not state["stop"]:
            try:
                async with account_conn.connection(phone) as client:
                    for guid in list(state["guids"]):
                        if state["stop"]:
                            break
                        idx, txt = _pick_text(state["texts"], last_text.get(guid))
                        if txt is None:
                            break
                        try:
                            await asyncio.wait_for(
                                rb.send_text(client, guid, txt),
                                timeout=config.SEND_TIMEOUT)
                            state["sent"] += 1
                            last_text[guid] = idx
                        except Exception:
                            pass
                        await asyncio.sleep(random.uniform(
                            config.LINKDOONI_GROUP_DELAY_MIN,
                            config.LINKDOONI_GROUP_DELAY_MAX))
            except Exception:
                account_conn.drop_connection(phone)
            await _state_sleep(state, state["interval"])
    except Exception:
        pass


async def _stop_secretary(phone: str):
    st = _secretaries.pop(rb.normalize_phone(phone), None)
    if not st:
        return
    st["stop"] = True
    t = st.get("task")
    if t:
        try:
            await asyncio.wait_for(t, timeout=10)
        except Exception:
            t.cancel()


async def _stop_channelreport(phone: str):
    st = _channelreports.pop(rb.normalize_phone(phone), None)
    if not st:
        return
    st["stop"] = True
    t = st.get("task")
    if t:
        try:
            await asyncio.wait_for(t, timeout=10)
        except Exception:
            t.cancel()


async def _stop_reply(phone: str):
    st = _replies.pop(rb.normalize_phone(phone), None)
    if not st:
        return
    st["stop"] = True
    t = st.get("task")
    if t:
        try:
            await asyncio.wait_for(t, timeout=10)
        except Exception:
            t.cancel()


async def _run_secretary(phone: str, st: dict):
    """Worker-side PV secretary — ONE connection per pass. Logs queued for the
    master."""
    while not st["stop"]:
        try:
            async with account_conn.connection(phone) as client:
                result = await asyncio.wait_for(
                    rb.get_chats_updates(client, st.get("state") or ""), timeout=60)
                chats, new_state = rb.parse_chats_updates(result)
                if new_state:
                    st["state"] = new_state
                if not st["primed"]:
                    st["primed"] = True
                    chats = []
                if chats:
                    self_guid = st.get("self_guid")
                    if not self_guid:
                        self_guid = await asyncio.wait_for(
                            rb.get_self_guid(client), timeout=30)
                        st["self_guid"] = self_guid
                marker_ctx = None
                for chat in chats:
                    if st["stop"]:
                        break
                    if rb.chat_type(chat) != "user":
                        continue
                    guid = rb.chat_object_guid(chat)
                    if not guid:
                        continue
                    author = rb.message_author_guid(rb.chat_last_message(chat))
                    if self_guid and author and author == self_guid:
                        continue
                    if guid in st["replied_users"]:
                        continue
                    try:
                        if st["mode"] == "text":
                            if not st["text"]:
                                continue
                            await asyncio.wait_for(
                                rb.send_text(client, guid, st["text"]),
                                timeout=config.SEND_TIMEOUT)
                        else:
                            if marker_ctx is None:
                                marker_ctx = await asyncio.wait_for(
                                    rb.find_marked_message(client, st["marker"] or ""),
                                    timeout=60)
                            saved_guid, mid = marker_ctx
                            if not mid:
                                _qlog(_wcard("🤖 منشی — مارکر پیدا نشد", [
                                    f"👤 Account : {phone}", f"🕒 {_now()}"]))
                                break
                            await asyncio.wait_for(
                                rb.forward_to(client, saved_guid, guid, mid),
                                timeout=config.SEND_TIMEOUT)
                        st["replied_users"].add(guid)
                        st["sent"] += 1
                        _qlog(_wcard("🤖 منشی — جواب خودکار", [
                            f"👤 Account : {phone}",
                            f"🎯 To : {guid}",
                            f"✍️ Mode : {'متن دلخواه' if st['mode'] == 'text' else 'مارکر'}",
                            f"🕒 {_now()}"]))
                    except Exception as e:  # noqa: BLE001
                        if account_conn.is_auth_error(e):
                            raise
                        _qlog(_wcard("⚠️ منشی — خطا", [
                            f"👤 Account : {phone}", f"🎯 To : {guid}",
                            f"💥 {repr(e)[:140]}", f"🕒 {_now()}"]))
                    await asyncio.sleep(config.SECRETARY_REPLY_DELAY)
        except account_conn.InvalidAuthError:
            if await _handle_auth_error(phone):
                break
        except Exception as e:  # noqa: BLE001
            if account_conn.is_auth_error(e):
                if await _handle_auth_error(phone):
                    break
            else:
                _qlog(_wcard("⚠️ منشی — خطای حلقه", [
                f"👤 Account : {phone}", f"💥 {repr(e)[:140]}", f"🕒 {_now()}"]))
        await _state_sleep(st, st["interval"])


async def _run_channelreport(phone: str, st: dict):
    """Worker-side channel report — ONE connection per pass; queues the card."""
    while not st["stop"]:
        guid = st.get("channel_guid") or ""
        title = st.get("channel_title") or ""
        iv = st["interval"]
        if guid:
            try:
                async with account_conn.connection(phone) as client:
                    if not str(guid).startswith("c0"):
                        rguid, rtitle = await asyncio.wait_for(
                            rb.resolve_channel(client, guid), timeout=60)
                        if rguid:
                            guid = rguid
                            st["channel_guid"] = rguid
                            if rtitle and not title:
                                title = rtitle
                                st["channel_title"] = rtitle
                    info = await asyncio.wait_for(
                        rb.get_channel_info(client, guid), timeout=60)
                    members = rb.channel_member_count(info)
                    if not title:
                        title = rb.channel_title_of(info)
                    views, _mid = await asyncio.wait_for(
                        rb.get_last_post_views(client, guid), timeout=60)
                _qlog(_wcard("📊 گزارش کانال", [
                    f"👤 Account : {phone}",
                    f"🆔 Channel : {guid}",
                    (f"🏷 Title : {title}" if title else None),
                    f"👥 Members : {members}",
                    f"👁 Last post views : "
                    f"{views if views is not None else 'نامشخص'}",
                    f"🕒 {_now()}"]))
            except account_conn.InvalidAuthError:
                if await _handle_auth_error(phone):
                    break
            except Exception as e:  # noqa: BLE001
                if account_conn.is_auth_error(e):
                    if await _handle_auth_error(phone):
                        break
                else:
                    _qlog(_wcard("⚠️ گزارش کانال — خطا", [
                        f"👤 Account : {phone}", f"🆔 Channel : {guid}",
                        f"💥 {repr(e)[:140]}", f"🕒 {_now()}"]))
        await _state_sleep(st, iv)


async def _run_reply(phone: str, st: dict):
    """Worker-side group-reply responder — ONE connection per pass; queues the
    reply card for the master."""
    while not st["stop"]:
        try:
            text = st.get("text") or ""
            delay = st.get("delay")
            if delay is None:
                delay = config.REPLY_DELAY
            if not text:
                await _state_sleep(st, config.REPLY_POLL_INTERVAL)
                continue
            async with account_conn.connection(phone) as client:
                self_guid = st.get("self_guid")
                if not self_guid:
                    self_guid = await asyncio.wait_for(
                        rb.get_self_guid(client), timeout=30)
                    st["self_guid"] = self_guid
                result = await asyncio.wait_for(
                    rb.get_chats_updates(client, st.get("state") or ""), timeout=60)
                chats, new_state = rb.parse_chats_updates(result)
                if new_state:
                    st["state"] = new_state
                if not st["primed"]:
                    st["primed"] = True
                    chats = []
                done = st["done"]
                for chat in chats:
                    if st["stop"]:
                        break
                    if rb.chat_type(chat) != "group":
                        continue
                    gguid = rb.chat_object_guid(chat)
                    if not gguid:
                        continue
                    try:
                        msgs = await asyncio.wait_for(
                            rb.get_recent_messages(client, gguid, 20), timeout=60)
                    except Exception as e:  # noqa: BLE001
                        if account_conn.is_auth_error(e):
                            raise
                        msgs = []
                    for m in msgs:
                        if st["stop"]:
                            break
                        mid = rb._msg_id_of(m)
                        rtid = rb.message_reply_to_id(m)
                        if not mid or not rtid or mid in done:
                            continue
                        author = rb.message_author_guid(m)
                        if self_guid and author == self_guid:
                            done.add(mid)
                            continue
                        try:
                            parents = await asyncio.wait_for(
                                rb.get_messages_by_id(client, gguid, [rtid]),
                                timeout=60)
                        except Exception as e:  # noqa: BLE001
                            if account_conn.is_auth_error(e):
                                raise
                            parents = []
                        if not parents:
                            continue
                        if not (self_guid and rb.message_author_guid(parents[0]) == self_guid):
                            done.add(mid)
                            continue
                        await asyncio.sleep(max(0.0, float(delay)))
                        try:
                            await asyncio.wait_for(
                                rb.send_reply(client, gguid, text, mid),
                                timeout=config.SEND_TIMEOUT)
                            done.add(mid)
                            st["sent"] += 1
                            _qlog(_wcard("↩️ پاسخ‌گوی ریپلای", [
                                f"یک ریپلای جواب داده شد توسط [{phone}]",
                                f"👥 Group : {gguid}", f"🕒 {_now()}"]))
                        except Exception as e:  # noqa: BLE001
                            if account_conn.is_auth_error(e):
                                raise
                            _qlog(_wcard("⚠️ ریپلای — خطا", [
                                f"👤 Account : {phone}", f"👥 Group : {gguid}",
                                f"💥 {repr(e)[:140]}", f"🕒 {_now()}"]))
        except account_conn.InvalidAuthError:
            if await _handle_auth_error(phone):
                break
        except Exception as e:  # noqa: BLE001
            if account_conn.is_auth_error(e):
                if await _handle_auth_error(phone):
                    break
            else:
                _qlog(_wcard("⚠️ ریپلای — خطای حلقه", [
                    f"👤 Account : {phone}", f"💥 {repr(e)[:140]}", f"🕒 {_now()}"]))
        await _state_sleep(st, config.REPLY_POLL_INTERVAL)


async def _sleep_with_stop(job: dict, seconds: float, step: float = 2.0):
    """Sleep up to `seconds`, but bail out early if the job is stopped."""
    waited = 0.0
    while waited < seconds:
        if job.get("stopped"):
            return
        d = min(step, seconds - waited)
        await asyncio.sleep(d)
        waited += d


async def _run_send(client, job: dict, saved_guid, mid, recipients, body):
    """Worker send loop with auto-resume: on hitting max_errors, wait
    body.resume_wait and resume from the rest of the list, up to
    body.max_retries times. Manual stop ends immediately. Calls the UNCHANGED
    rb.forward_message for every recipient."""
    n = len(recipients)
    idx = 0
    dead_rounds = 0
    try:
        while True:
            attempt_fail = 0
            hit_max = False
            round_ok_start = job["ok"]
            while idx < n:
                if job["stopped"]:
                    job["reason"] = "manual_stop"
                    return
                guid = recipients[idx]
                idx += 1
                try:
                    await asyncio.wait_for(
                        rb.forward_message(client, saved_guid, guid, mid),
                        timeout=body.send_timeout,
                    )
                    # step 5: optional second text (always text) to the SAME
                    # recipient, right after the forward. Best-effort: a failure
                    # of the second text must NOT undo the successful forward.
                    if getattr(body, "text2", ""):
                        try:
                            await asyncio.wait_for(
                                rb.send_text(client, guid, body.text2),
                                timeout=body.send_timeout,
                            )
                        except Exception:  # noqa: BLE001
                            pass
                    job["ok"] += 1
                    attempt_fail = 0          # reset: count CONSECUTIVE errors only
                except Exception as e:  # noqa: BLE001
                    job["fail"] += 1
                    attempt_fail += 1
                    job["last_error"] = repr(e)[:200]
                    if attempt_fail >= body.max_errors:
                        hit_max = True
                        break
                await _sleep_with_stop(job, body.delay)

            if not hit_max:
                break  # finished the whole list
            # max_retries <= 0 means UNLIMITED resume (keep going until done)
            if body.max_retries > 0 and job["retry_count"] >= body.max_retries:
                job["reason"] = f"max_errors({body.max_errors})"
                break
            # wait, then reconnect a fresh client and resume from `idx`
            job["retry_count"] += 1
            if job["ok"] == round_ok_start:
                dead_rounds += 1
            else:
                dead_rounds = 0
            if (config.RESUME_MAX_DEAD_ROUNDS > 0
                    and dead_rounds >= config.RESUME_MAX_DEAD_ROUNDS):
                job["reason"] = f"blocked: {dead_rounds} dead rounds"
                break
            job["state"] = "waiting"
            await _sleep_with_stop(job, body.resume_wait)
            if job["stopped"]:
                job["reason"] = "manual_stop"
                break
            job["state"] = "sending"
            try:
                await client.disconnect()
            except Exception:
                pass
            client = rb.open_client(body.phone)
            await rb.connect_ready(client)
    except Exception as e:  # noqa: BLE001
        job["reason"] = f"fatal: {repr(e)[:200]}"
    finally:
        job["done"] = True
        try:
            await client.disconnect()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Entrypoint (called when MODE=worker).
# --------------------------------------------------------------------------- #
def run():
    problems = config.validate_worker()
    if problems:
        print("Missing worker settings in .env: " + ", ".join(problems))
        return
    if not _HAVE_FASTAPI:
        print("نصب نیست: fastapi/uvicorn. اجرا کن: pip install fastapi uvicorn httpx")
        return
    import uvicorn
    app = _build_app()
    host = config.WORKER_BIND_HOST or "0.0.0.0"
    print(f"Worker API listening on {host}:{config.WORKER_API_PORT}", flush=True)
    uvicorn.run(app, host=host, port=config.WORKER_API_PORT, log_level="info")


if __name__ == "__main__":
    run()
