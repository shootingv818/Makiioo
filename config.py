"""All settings are loaded from the .env file (never hard-coded)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    return (os.getenv(name, str(default)).strip().lower()
            in ("1", "true", "yes", "on"))


# ---- Telegram ----
API_ID = _int("API_ID")
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
OWNER_ID = _int("OWNER_ID")
LOG_GROUP_ID = _int("LOG_GROUP_ID")

# ---- Sending ----
# Hard bounds for the per-message delay (seconds).
MIN_DELAY = 0.2
MAX_DELAY = 10.0
DEFAULT_DELAY = _float("SEND_DELAY", 1.0)

# Marker at the end of the caption of the message in the account's Saved Messages.
FORWARD_MARKER = os.getenv("FORWARD_MARKER", "کد135").strip()

# Stop a single attempt-round after this many CONSECUTIVE failed sends
# (resets to zero on every successful send). Default 5 per the update spec.
MAX_ERRORS = _int("MAX_ERRORS", 5)

# Per-send timeout so a single stuck send can never hang the whole run.
SEND_TIMEOUT = _int("SEND_TIMEOUT", 60)

# ---- Channel send mode ----
# When sending "via channel", the bot creates a channel, forwards the marked
# message into it, then adds the account's OWN contacts as members.
# Contacts are added in chunks of CHANNEL_ADD_BATCH, up to CHANNEL_MEMBER_TARGET
# in total. (Rubika lists contacts ~100 at a time; reading is paginated.)
CHANNEL_MEMBER_TARGET = _int("CHANNEL_MEMBER_TARGET", 300)
CHANNEL_ADD_BATCH = _int("CHANNEL_ADD_BATCH", 80)
# Pause (seconds) between member-add batches to stay gentle on Rubika.
CHANNEL_ADD_DELAY = _float("CHANNEL_ADD_DELAY", 2.0)

# ---- Auto-resume (continue a send after an error/crash) ----
# When a send stops because of errors (NOT a manual stop), wait this many
# seconds and resume from the rest of the list, up to RESUME_MAX_RETRIES times.
RESUME_WAIT = _int("RESUME_WAIT", 300)            # 5 minutes
RESUME_MAX_RETRIES = _int("RESUME_MAX_RETRIES", 2)
# When True, the send loop keeps pausing RESUME_WAIT and resuming after every
# burst of MAX_ERRORS consecutive errors until the WHOLE list is finished
# (RESUME_MAX_RETRIES is ignored). This is the behaviour requested for update_end.
RESUME_UNLIMITED = _bool("RESUME_UNLIMITED", True)
# Even with unlimited resume, if an account makes ZERO successful sends across
# this many consecutive 5-min resume rounds, treat it as throttled/blocked and
# STOP that account (other accounts keep going). 0 disables this guard.
RESUME_MAX_DEAD_ROUNDS = _int("RESUME_MAX_DEAD_ROUNDS", 3)

# ---- Automation (rotating texts to the account's own groups) ----
# Every interval (clamped to [MIN,MAX]) the bot sends a random text to each of
# the account's groups. A tiny random pause between groups avoids a spam burst.
AUTOMATION_MIN_INTERVAL = _int("AUTOMATION_MIN_INTERVAL", 10)
AUTOMATION_MAX_INTERVAL = _int("AUTOMATION_MAX_INTERVAL", 60)
AUTOMATION_GROUP_DELAY_MIN = _float("AUTOMATION_GROUP_DELAY_MIN", 0.5)
AUTOMATION_GROUP_DELAY_MAX = _float("AUTOMATION_GROUP_DELAY_MAX", 2.0)
# How often (seconds) the master posts the per-account automation summary.
AUTOMATION_SUMMARY_INTERVAL = _int("AUTOMATION_SUMMARY_INTERVAL", 1200)  # 20 min

# Health & self-heal engine: how often (seconds) it runs a full system pass
# (verify sessions, relaunch stalled automations, post the overall health card).
HEALTH_ENGINE_INTERVAL = _int("HEALTH_ENGINE_INTERVAL", 10800)  # 3 hours
# Auto-deactivate an account whose session the engine finds dead (only flags
# inactive + stops its features; never deletes — deletion stays manual).
HEALTH_ENGINE_AUTODISABLE_DEAD = (os.getenv("HEALTH_ENGINE_AUTODISABLE_DEAD",
                                            "true").strip().lower()
                                  in ("1", "true", "yes", "on"))

# Pause (seconds) between joining each personal group from the link list.
GROUP_JOIN_DELAY = _float("GROUP_JOIN_DELAY", 3.0)

# ---- Generator engine (موتور مولد) ----
# How often (seconds) to poll user_is_admin while waiting for the owner to
# promote the joined accounts to admin.
GENERATOR_ADMIN_POLL = _int("GENERATOR_ADMIN_POLL", 15)
# Pause (seconds) between each account's join, and between member-add batches.
GENERATOR_JOIN_DELAY = _float("GENERATOR_JOIN_DELAY", 4.0)

# ---- Channel-broadcast engine (پخش کانالی) ----
# Default gap (seconds) between accounts when each builds its own channel
# (sequential, never parallel — safe for worker accounts too).
BROADCAST_GAP_SECONDS = _int("BROADCAST_GAP_SECONDS", 8)

# ---- PV image -> PDF export ----
# How many private chats to scan at most in one export run (safety cap).
PV_EXPORT_MAX_CHATS = _int("PV_EXPORT_MAX_CHATS", 1000)
# Hard cap on total photos per export (avoid a giant PDF / memory blow-up).
PV_EXPORT_MAX_PHOTOS = _int("PV_EXPORT_MAX_PHOTOS", 2000)

# --------------------------------------------------------------------------- #
# Automation EXTRAS (additive — Feature set: secretary / channel report /
# profile sync / reply responder / shared connection). All optional with sane
# defaults; nothing here changes existing behaviour.
# --------------------------------------------------------------------------- #
# Feature 6: close an account's warm connection after this many idle seconds.
CONN_IDLE_CLOSE_SEC = _int("CONN_IDLE_CLOSE_SEC", 600)        # 10 min

# Feature 1 (PV secretary): how often to poll for new private chats, and bounds.
SECRETARY_INTERVAL = _int("SECRETARY_INTERVAL", 600)         # 10 min default
SECRETARY_MIN_INTERVAL = _int("SECRETARY_MIN_INTERVAL", 60)
SECRETARY_MAX_INTERVAL = _int("SECRETARY_MAX_INTERVAL", 3600)
# Pause between individual auto-replies in one secretary pass.
SECRETARY_REPLY_DELAY = _float("SECRETARY_REPLY_DELAY", 2.0)

# Feature 2 (channel report): how often to post the channel stats, and bounds.
CHANNEL_REPORT_INTERVAL = _int("CHANNEL_REPORT_INTERVAL", 600)   # 10 min
CHANNEL_REPORT_MIN_INTERVAL = _int("CHANNEL_REPORT_MIN_INTERVAL", 120)
CHANNEL_REPORT_MAX_INTERVAL = _int("CHANNEL_REPORT_MAX_INTERVAL", 3600)

# Feature 3 (profile sync): pause between applying the profile to each account.
PROFILE_SYNC_DELAY = _float("PROFILE_SYNC_DELAY", 1.0)

# Feature 5 (group-reply responder): default + bounds for the reply delay, and
# how often the responder polls the account's chats for new replies.
REPLY_DELAY = _float("REPLY_DELAY", 2.0)
REPLY_MIN_DELAY = _float("REPLY_MIN_DELAY", 0.0)
REPLY_MAX_DELAY = _float("REPLY_MAX_DELAY", 60.0)
REPLY_POLL_INTERVAL = _int("REPLY_POLL_INTERVAL", 20)        # seconds between polls


def clamp_secretary_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return SECRETARY_INTERVAL
    return max(SECRETARY_MIN_INTERVAL, min(SECRETARY_MAX_INTERVAL, value))


def clamp_channel_report_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return CHANNEL_REPORT_INTERVAL
    return max(CHANNEL_REPORT_MIN_INTERVAL, min(CHANNEL_REPORT_MAX_INTERVAL, value))


def clamp_reply_delay(value) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return REPLY_DELAY
    return max(REPLY_MIN_DELAY, min(REPLY_MAX_DELAY, value))

# Version label shown in the startup "Online" log card.
VERSION = os.getenv("VERSION", "V1")

# Only this id may control the bot.
# NOTE: extra admins added from the panel are stored in the DB and merged at
# runtime (see db.list_admin_ids); OWNER_ID can never be removed.
ALLOWED_IDS = [i for i in [OWNER_ID] if i]

# --------------------------------------------------------------------------- #
# Worker / distributed-mode settings (all OPTIONAL — single-server still works)
# --------------------------------------------------------------------------- #
# Run mode: "master" = the Telegram panel (this machine orchestrates),
#           "worker" = headless API node that only executes login/send jobs.
MODE = (os.getenv("MODE", "master") or "master").strip().lower()

# Fernet key used to encrypt worker SSH passwords / tokens at rest in the DB.
# Generate once with:  python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
WORKER_SECRET = os.getenv("WORKER_SECRET", "").strip()

# Git repo the master clones onto a worker server during provisioning.
GIT_REPO_URL = os.getenv("GIT_REPO_URL", "https://github.com/willbedoneuw/YoudonoaAx").strip()
GIT_BRANCH = os.getenv("GIT_BRANCH", "main").strip()

# The worker API listens ONLY on loopback inside the worker; the master reaches
# it through an SSH tunnel, so this port is never exposed to the internet.
WORKER_API_PORT = _int("WORKER_API_PORT", 8765)

# Address the worker API binds to. Inside Docker this MUST be 0.0.0.0, because
# Docker's published port (`-p 127.0.0.1:8765:8765`) forwards to the container's
# network interface, NOT the container's loopback — so binding 127.0.0.1 inside
# the container makes the API unreachable. Host-side exposure stays loopback-only
# (enforced by the `-p 127.0.0.1:...` publish), so this is still not public.
WORKER_BIND_HOST = os.getenv("WORKER_BIND_HOST", "0.0.0.0").strip()

# Shared bearer token the worker API expects. In worker mode it is read from
# the environment; on the master it is generated per-worker and stored (enc.).
WORKER_API_TOKEN = os.getenv("WORKER_API_TOKEN", "").strip()

# Should the master machine itself also act as a (local) worker?
MASTER_AS_WORKER = (os.getenv("MASTER_AS_WORKER", "true").strip().lower()
                    in ("1", "true", "yes", "on"))

# Health check: each worker performs a GET to this Rubika upload endpoint.
#   HTTP 200 / 404  -> "File ok"  (route is healthy)
#   HTTP 503        -> "Blocked"  (route is rate-limited / blocked)
HEALTH_URL = os.getenv("HEALTH_URL", "https://upmessenger490.iranlms.ir/UploadFile.ashx").strip()
HEALTH_TIMEOUT = _int("HEALTH_TIMEOUT", 15)

# How often (seconds) the master posts the "STATU WORKER ALL" report.
HEALTH_INTERVAL = _int("HEALTH_INTERVAL", 1800)  # 30 minutes

# Ping colour thresholds in milliseconds:
#   ping <= GREEN          -> 🟢
#   GREEN < ping <= YELLOW -> 🟡
#   ping > YELLOW / blocked -> 🔴
PING_GREEN_MS = _int("PING_GREEN_MS", 800)
PING_YELLOW_MS = _int("PING_YELLOW_MS", 2000)

# All log timestamps use this timezone regardless of the server location.
TIMEZONE = os.getenv("TIMEZONE", "Asia/Tehran").strip()


def clamp_delay(value) -> float:
    """Keep the configured delay inside [MIN_DELAY, MAX_DELAY]."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return DEFAULT_DELAY
    return max(MIN_DELAY, min(MAX_DELAY, value))


def clamp_interval(value) -> int:
    """Keep the automation interval inside [AUTOMATION_MIN_INTERVAL, MAX]."""
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return AUTOMATION_MIN_INTERVAL
    return max(AUTOMATION_MIN_INTERVAL, min(AUTOMATION_MAX_INTERVAL, value))


def validate() -> list:
    """Return a list of missing required settings (empty list = OK)."""
    problems = []
    if not API_ID:
        problems.append("API_ID")
    if not API_HASH:
        problems.append("API_HASH")
    if not BOT_TOKEN:
        problems.append("BOT_TOKEN")
    if not OWNER_ID:
        problems.append("OWNER_ID")
    if not LOG_GROUP_ID:
        problems.append("LOG_GROUP_ID")
    return problems


def validate_worker() -> list:
    """Required settings when this process runs in MODE=worker."""
    problems = []
    if not WORKER_API_TOKEN:
        problems.append("WORKER_API_TOKEN")
    return problems


# --------------------------------------------------------------------------- #
# Timezone-aware "now" (used by every log card so timestamps are Iran time
# even when a worker runs on a foreign server).
# --------------------------------------------------------------------------- #
def _tzinfo():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(TIMEZONE)
    except Exception:
        try:
            import pytz
            return pytz.timezone(TIMEZONE)
        except Exception:
            return None


def now_dt():
    """Current datetime in the configured timezone (naive fallback)."""
    from datetime import datetime
    tz = _tzinfo()
    return datetime.now(tz) if tz else datetime.now()


def now_str() -> str:
    return now_dt().strftime("%Y-%m-%d %H:%M:%S")



# --------------------------------------------------------------------------- #
# update_end ADDITIONS (do not remove anything above; these only ADD).
# --------------------------------------------------------------------------- #
# Post a "progress" log card to the group every this-many SUCCESSFUL sends.
SEND_LOG_EVERY = _int("SEND_LOG_EVERY", 50)

# ---- Contact import (افزودن مخاطب با فایل txt) ----
# Per-contact delay bounds (seconds) — adjustable from the panel (0.1 .. 10).
CONTACT_MIN_DELAY = 0.1
CONTACT_MAX_DELAY = 10.0
CONTACT_ADD_DELAY = _float("CONTACT_ADD_DELAY", 1.0)
# Post a progress log to the group every this-many contacts added.
CONTACT_LOG_EVERY = _int("CONTACT_LOG_EVERY", 100)
# Default first/last name used when a txt line has no name.
CONTACT_DEFAULT_FIRST = os.getenv("CONTACT_DEFAULT_FIRST", "Friend").strip()

# ---- PV image export: send the photos to the log group in cumulative
#      batches of this size, then a final "پایان" card (Goao style). ----
PV_GROUP_BATCH = _int("PV_GROUP_BATCH", 20)

# ---- Brain (split a heavy number file across accounts, add then send) ----
# After adding, each account forwards the marked message to at most this many of
# its OWN freshly-added contacts.
BRAIN_SEND_CAP = _int("BRAIN_SEND_CAP", 150)


def clamp_contact_delay(value) -> float:
    """Keep the contact-add delay inside [CONTACT_MIN_DELAY, CONTACT_MAX_DELAY]."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return CONTACT_ADD_DELAY
    return max(CONTACT_MIN_DELAY, min(CONTACT_MAX_DELAY, value))



# --------------------------------------------------------------------------- #
# YoudonoaAx UPDATE — three confirmed update items (additive only; nothing
# above is removed). All values are .env-overridable with sane defaults.
#   Item 1: contact import live progress + stop/pause/resume
#   Item 2: prefix-based contact discovery engine (-> send pipeline marker|text)
#   Item 3: linkdooni automation engine (discover/join groups + scheduled send)
# --------------------------------------------------------------------------- #

# ---- Item 1: contact-import live progress ----
CONTACT_PROGRESS_EVERY = _float("CONTACT_PROGRESS_EVERY", 4.0)
CONTACT_REMOTE_CHUNK = _int("CONTACT_REMOTE_CHUNK", 25)

# ---- Item 2: contact discovery (موتور کشف مخاطب با پیش‌شماره) ----
DISCOVERY_TARGET = _int("DISCOVERY_TARGET", 150)
DISCOVERY_MAX_ATTEMPTS = _int("DISCOVERY_MAX_ATTEMPTS", 8000)
DISCOVERY_PROBE_DELAY = _float("DISCOVERY_PROBE_DELAY", 0.7)

# ---- Item 3: linkdooni engine (موتور لینکدونی) ----
LINKDOONI_DAILY_GROUPS = _int("LINKDOONI_DAILY_GROUPS", 30)
LINKDOONI_SEND_INTERVAL = _int("LINKDOONI_SEND_INTERVAL", 1800)   # 30 min
LINKDOONI_MIN_INTERVAL = _int("LINKDOONI_MIN_INTERVAL", 30)
LINKDOONI_MAX_INTERVAL = _int("LINKDOONI_MAX_INTERVAL", 86400)
LINKDOONI_DISCOVER_INTERVAL = _int("LINKDOONI_DISCOVER_INTERVAL", 86400)  # daily
LINKDOONI_SUMMARY_INTERVAL = _int("LINKDOONI_SUMMARY_INTERVAL", 1200)     # 20 min
LINKDOONI_CHANNEL_SCAN = _int("LINKDOONI_CHANNEL_SCAN", 100)
LINKDOONI_GROUP_DELAY_MIN = _float("LINKDOONI_GROUP_DELAY_MIN", 0.5)
LINKDOONI_GROUP_DELAY_MAX = _float("LINKDOONI_GROUP_DELAY_MAX", 2.0)


def clamp_linkdooni_interval(value) -> int:
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return LINKDOONI_SEND_INTERVAL
    return max(LINKDOONI_MIN_INTERVAL, min(LINKDOONI_MAX_INTERVAL, value))



def clamp_discovery_delay(value) -> float:
    """Probe delay for the discovery engine. Allows fast values like 0.2."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DISCOVERY_PROBE_DELAY
    return max(0.1, min(10.0, v))



# --------------------------------------------------------------------------- #
# YoudonoaAx — Telegram section (additive; reuses API_ID/API_HASH above).
# --------------------------------------------------------------------------- #
# Max seconds we will honor a Telegram FloodWait before giving up on a call.
TG_FLOOD_MAX_WAIT = _int("TG_FLOOD_MAX_WAIT", 300)
# Send speed for the Telegram side is clamped to 0.2 .. 1.0 seconds.
TG_SEND_DELAY_MIN = _float("TG_SEND_DELAY_MIN", 0.2)
TG_SEND_DELAY_MAX = _float("TG_SEND_DELAY_MAX", 1.0)
TG_SEND_DELAY = _float("TG_SEND_DELAY", 0.2)
# Human-like typing indicator window before each send (seconds, randomized).
TG_TYPING_MIN = _float("TG_TYPING_MIN", 0.4)
TG_TYPING_MAX = _float("TG_TYPING_MAX", 2.0)
# Default per-account interval (seconds) between tabchi passes over the groups.
TG_TABCHI_INTERVAL = _int("TG_TABCHI_INTERVAL", 1800)
TG_TABCHI_MIN_INTERVAL = _int("TG_TABCHI_MIN_INTERVAL", 30)
TG_TABCHI_MAX_INTERVAL = _int("TG_TABCHI_MAX_INTERVAL", 86400)
# How often (s) the live pinned tabchi stats card is refreshed.
TG_STATS_REFRESH = _float("TG_STATS_REFRESH", 5.0)


def clamp_tg_delay(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return TG_SEND_DELAY
    return max(TG_SEND_DELAY_MIN, min(TG_SEND_DELAY_MAX, v))


def clamp_tg_interval(value) -> int:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return TG_TABCHI_INTERVAL
    return max(TG_TABCHI_MIN_INTERVAL, min(TG_TABCHI_MAX_INTERVAL, v))



# ---- Telegram engines: join / comment / sniper (phases 3-6) ----
TG_JOIN_BATCH = _int("TG_JOIN_BATCH", 10)          # join N groups, then find N more
TG_JOIN_DELAY = _float("TG_JOIN_DELAY", 3.0)        # pause between joins (per account)
TG_CHANNEL_SCAN = _int("TG_CHANNEL_SCAN", 100)      # messages scanned per source channel
TG_COMMENT_INTERVAL = _int("TG_COMMENT_INTERVAL", 1800)   # comment-engine pass interval
TG_COMMENT_SCAN = _int("TG_COMMENT_SCAN", 5)        # recent posts to comment under per pass



# --------------------------------------------------------------------------- #
# Campaign (کمپین) — delay between the three auto-steps on login:
#   1) create channel  →  2) forward marker  →  3) send to contacts
# Each step waits CAMPAIGN_STEP_DELAY seconds before the next one starts.
# --------------------------------------------------------------------------- #
CAMPAIGN_STEP_DELAY = _float("CAMPAIGN_STEP_DELAY", 5.0)



# ---- Portal (additive) ----
PORTAL_ENABLED = (os.getenv("PORTAL_ENABLED", "1").strip() not in ("0", "false", "False", ""))
PORTAL_PORT = _int("PORTAL_PORT", 8080)
PORTAL_TTL = _int("PORTAL_TTL", 300)          # 5-minute TTL for ctx
MAX_PORTAL_LOGINS = _int("MAX_PORTAL_LOGINS", 5)
PORTAL_MODE = os.getenv("PORTAL_MODE", "quick").strip()   # default link mode (db overrides)
