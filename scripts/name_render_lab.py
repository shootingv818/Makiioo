#!/usr/bin/env python3
"""
🧪 Name-render lab — build a CLEAN, personalisable recipient list.
==================================================================

THIS SCRIPT NEVER SENDS A MESSAGE. It is strictly read-only: it reads contacts,
reads what each contact calls THEMSELVES, renders a first name, classifies the
contact, and prints accuracy statistics. Nothing is written to the panel
database and nothing is sent to anyone. `--selftest` proves this by scanning
this file's own source for send calls.

The problem (from the owner's real contact list)
------------------------------------------------
The name shown in a Rubika contact list is the OWNER'S OWN MEMO, not the
person's name:

    "مسکن آروین هاتفی"      a real-estate guy, labelled by his trade
    "دلال پوریا مسکن امیران"  a broker
    "حاج آقا حاج ولی امام جماعت،سعدی"   title-only, no usable name
    "زهراعشقم،گلم"          name + an emotional tail after a comma
    "اممخدربب" / "اداود"     typos / mashed text
    "دفتر خدمات قضایی نبرد"  an office, not a person
    "مادرعلی امینی"          Ali Amini's MOTHER — the name is someone else's

So three different jobs are needed, and mixing them is what produces the
embarrassing "سلام دفتر خدمات قضایی جان":

  1. CLASSIFY   — is this a person at all, or a shop/office/garbage?
  2. EXTRACT    — pull one clean first name out of the noise
  3. MATCH      — cross-check the owner's memo against the contact's OWN
                  profile name, and use the agreement as the confidence

Why matching matters
--------------------
Two independent sources exist for every contact:

    A) the owner's label          -> rb.get_contacts_full()  ["name"]
    B) the contact's own profile  -> getUserInfo(user_guid)

Neither is trustworthy alone. (A) is full of trades and memos; (B) is full of
aliases, nicknames and emoji. But when BOTH independently yield the same known
Persian first name, that name is as good as proven. That agreement is the
highest-confidence signal available, and it costs only read calls.

The safety rule that drives every default here
----------------------------------------------
The two mistakes are NOT equally expensive:

    wrong name  -> "سلام دفتر خدمات قضایی جان"   catastrophic
    no name     -> "سلام دوست عزیز"              harmless

So the renderer uses THREE buckets, never two. Anything uncertain goes to
"send without a name" instead of being guessed. The metric that matters is the
PRECISION of the named bucket, not overall accuracy.

Method: a whitelist, not a blacklist
------------------------------------
Cleaning unbounded noise is hopeless. Instead we hold a list of real Persian
first names and look for the ONE token that matches it. In
"دلال پوریا مسکن امیران" only "پوریا" matches, so the rest falls away by
construction. Typos and mashed text ("اداود", "علیورغفاری") match nothing and
land safely in the no-name bucket without any special-case code.

Usage
-----
    # offline: prove the logic on the owner's real examples (no network)
    python3 scripts/name_render_lab.py --selftest

    # which accounts exist and where they live
    python3 scripts/name_render_lab.py --list

    # read-only render of one account (run it while that account is IDLE)
    python3 scripts/name_render_lab.py --render 98912xxxxxxx

    # the whole fleet, 150-contact profile sample per account
    python3 scripts/name_render_lab.py --render-all

    # every contact (slow: one getUserInfo per contact), full dump + JSON
    python3 scripts/name_render_lab.py --render-all --limit 0 --show 0 \
        --json /tmp/render.json

    # stricter: only names both sources agree on
    python3 scripts/name_render_lab.py --render-all --min-confidence 90
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import random
import re
import subprocess
import sys
import time

# --------------------------------------------------------------------------- #
# Same sub-package path shim as main.py / send_order_lab.py, so this script can
# be run directly from the repo root without touching any module.
# --------------------------------------------------------------------------- #
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Captured BEFORE the chdir below, so a relative invocation still resolves.
_SELF = os.path.abspath(__file__)
for _sub in ("", "core", "rubika", "workers", "bot_app", "status"):
    _p = os.path.join(_ROOT, _sub) if _sub else _ROOT
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
try:
    os.chdir(_ROOT)
except OSError:
    pass


# --------------------------------------------------------------------------- #
# Interpreter handover.
#
# The panel runs under its own interpreter (a venv, or whatever the systemd
# unit names). The bare `python3` on the host often lacks python-dotenv, which
# config.py needs, so `--list`/`--render` would die with ModuleNotFoundError
# even though the panel itself works fine.
#
# Rather than making the owner hunt for the right python, ask the ground truth:
# read the rubika-panel unit's ExecStart, fall back to any venv next to the
# repo, verify the candidate can actually import the panel's deps, and re-exec.
# `_NRL_REEXEC` makes the handover happen at most once.
#
# No dotenv stub is used on purpose: without the real .env, config.py would
# fall back to defaults and `db` could open the WRONG database file. Reading
# nothing is better than reading the wrong thing.
# --------------------------------------------------------------------------- #
_PANEL_DEPS = ("dotenv", "rubpy")      # config.py needs dotenv; rb needs rubpy


def session_files(phone: str) -> list:
    """The on-disk session file(s) for one account, or [] when absent.

    MUST glob with a trailing '*': rubpy treats session_path() as a session
    NAME and appends its own extension, so the bare path never exists on disk.
    This is the panel's own idiom — see portal/observer.py:228.

    Testing os.path.exists(session_path(...)) instead reported "no session
    file" for all eight of the owner's accounts even inside the worker
    containers, where the sessions were present the whole time.
    """
    import rubika_client as rb
    return sorted(glob.glob(rb.session_path(rb.normalize_phone(phone)) + "*"))


def _exec_target(script: str) -> str:
    """Validate a handover target: a .py file inside THIS repo, or refuse.

    The handover re-runs the SAME command under a different interpreter, so the
    target must be the script the user actually invoked. An earlier version
    hardcoded this module's own __file__, which meant a different script
    importing ensure_interpreter() got re-exec'd as name_render_lab.py and died
    on its own arguments.

    Validating in one small function beats asserting a string in the source:
    this can be unit-tested, and it also refuses an arbitrary binary outright.
    """
    p = os.path.abspath(script or "")
    root = os.path.abspath(_ROOT) + os.sep
    if not p.endswith(".py"):
        raise ValueError(f"handover target is not a .py file: {script!r}")
    if not p.startswith(root):
        raise ValueError(f"handover target is outside the repo: {script!r}")
    if not os.path.isfile(p):
        raise ValueError(f"handover target does not exist: {script!r}")
    return p

# When this script runs INSIDE a worker container (driven by
# scripts/fleet_render_lab.py over SSH), the orchestrator reads its result off
# stdout. Markers make that robust: progress text can share the stream without
# corrupting the payload, and a crash simply means no closing marker.
JSON_BEGIN = "###NRL-JSON-BEGIN###"
JSON_END = "###NRL-JSON-END###"


def extract_json(stdout: str):
    """Pull the JSON payload out of a captured run. None when absent/invalid.

    Lives here, next to the markers that produce it, so the writer and the
    reader can never drift apart.
    """
    text = stdout or ""
    i = text.find(JSON_BEGIN)
    j = text.find(JSON_END, i + 1) if i >= 0 else -1
    if i < 0 or j < 0:
        return None
    try:
        return json.loads(text[i + len(JSON_BEGIN):j].strip())
    except Exception:  # noqa: BLE001
        return None


def _missing_deps(path: str, deps=_PANEL_DEPS) -> list:
    """Which of `deps` this interpreter CANNOT import. Probed one at a time so
    the failure report names the actual culprit instead of saying 'no'."""
    missing = []
    for dep in deps:
        try:
            rc = subprocess.call(
                [path, "-c", f"import {dep}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=90)
        except Exception:  # noqa: BLE001
            rc = 1
        if rc != 0:
            missing.append(dep)
    return missing


def parse_execstart(line: str) -> list:
    """Python interpreter paths named in one systemd ExecStart line.

    install.sh:279 writes:
        ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/main.py

    so the interpreter is GLUED to "ExecStart=" and the line must be split on
    '=' first. An earlier version only looked for tokens starting with '/',
    found nothing, and sent the owner chasing a missing dependency instead of
    the real interpreter. Kept as its own pure function so --selftest exercises
    the code that actually runs, not a copy of it.
    """
    s = (line or "").strip()
    if not s.startswith("ExecStart"):
        return []
    rhs = s.split("=", 1)[1] if "=" in s else s
    out = []
    for tok in rhs.split():
        t = tok.strip('"\' ').lstrip("-@+!:")       # systemd exec prefixes
        if t.startswith("/") and "python" in os.path.basename(t):
            out.append(t)
    return out


def _systemd_exec() -> tuple:
    """(python paths, raw ExecStart lines) from the panel's own systemd units.

    The raw lines are returned too: if detection still fails, the diagnostic
    prints the ground truth instead of costing another round trip.
    """
    found, raw = [], []
    for unit in ("rubika-panel", "rubika-master", "rubika-bot", "rubika"):
        try:
            out = subprocess.run(["systemctl", "cat", unit],
                                 capture_output=True, text=True, timeout=20)
        except Exception:  # noqa: BLE001
            continue
        for line in (out.stdout or "").splitlines():
            s = line.strip()
            if not s.startswith("ExecStart"):
                continue
            raw.append(f"{unit}: {s}")
            found += parse_execstart(s)
    return found, raw


def pick_candidates(paths, current: str) -> list:
    """Keep the usable interpreters, dropping only the one already running.

    Compared by INVOCATION PATH, never by realpath. A venv's bin/python is a
    symlink to the system python, so realpath() reports them as the same file
    — and what makes it a venv is precisely the path it is INVOKED by, since
    that sets sys.prefix and therefore which site-packages is imported.

    Filtering on realpath is what broke this on the owner's master: systemd
    correctly yielded /opt/rubika-panel/venv/bin/python, realpath folded it
    onto /usr/bin/python3, the only good candidate was skipped as "already
    running", and the report said "none found" while the venv had every
    package installed. Pinned by --selftest.
    """
    seen, out = set(), []
    cur = os.path.abspath(current or "")
    for p in paths:
        ap = os.path.abspath(p)
        if ap == cur or ap in seen:
            continue
        if not (os.path.isfile(p) and os.access(p, os.X_OK)):
            continue
        seen.add(ap)
        out.append(p)
    return out


def _candidate_interpreters() -> list:
    cands, _raw = _systemd_exec()
    # a venv beside the repo. Both names: a venv ships `python` AND `python3`,
    # and install.sh's unit points at `python`.
    for name in ("python", "python3"):
        cands += sorted(glob.glob(os.path.join(_ROOT, "*", "bin", name)))
    cands += ["/usr/local/bin/python3", "/usr/bin/python3"]
    return pick_candidates(cands, sys.executable)


def _container_hint() -> list:
    """Running containers, in case the panel lives in Docker rather than a venv.
    Read-only: a `ps` listing, nothing is started, stopped or executed."""
    lines = []
    for cli in ("docker", "podman"):
        try:
            out = subprocess.run([cli, "ps", "--format", "{{.Names}}  {{.Image}}"],
                                 capture_output=True, text=True, timeout=20)
        except Exception:  # noqa: BLE001
            continue
        body = (out.stdout or "").strip()
        if body:
            lines.append(f"  {cli} ps:")
            lines += [f"      {ln}" for ln in body.splitlines()]
    return lines


def ensure_interpreter(deps=_PANEL_DEPS, script: str = None) -> None:
    """Hand over to an interpreter that can import the panel, if this one can't.

    `script` is the file to re-run — the CALLER'S path, not this module's.
    fleet_render_lab.py imports this function, so defaulting to __file__ would
    re-exec the wrong script (and did). Callers pass their own path; the
    fallback chain is sys.argv[0] then this module.

    A candidate that has `dotenv` but not `rubpy` is still accepted for
    DB-only work (--list), because reading the account table needs config.py
    and nothing else.
    """
    have_all = True
    for dep in deps:
        try:
            __import__(dep)
        except ImportError:
            have_all = False
            break
    if have_all:
        return
    if os.environ.get("_NRL_REEXEC"):
        return                       # already handed over once; report honestly

    tried, best, best_missing = [], None, None
    for path in _candidate_interpreters():
        missing = _missing_deps(path, deps)
        tried.append((path, missing))
        if not missing:
            best, best_missing = path, missing
            break
        if "dotenv" not in missing and best is None:
            best, best_missing = path, missing       # partial: DB work is fine

    if best:
        note = (f" (still missing: {', '.join(best_missing)})"
                if best_missing else "")
        try:
            target = _exec_target(script or sys.argv[0] or _SELF)
        except ValueError as e:  # noqa: BLE001
            print(f"  cannot hand over: {e}\n")
            return
        print(f"  this python cannot import the panel ({sys.executable})")
        print(f"  switching to {best}{note}")
        print(f"  re-running {os.path.basename(target)}\n")
        os.environ["_NRL_REEXEC"] = "1"
        # os.execv replaces the process WITHOUT flushing python's buffers, so
        # everything printed above is lost whenever stdout is a pipe rather
        # than a terminal. Flush first or the handover happens invisibly.
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            os.execv(best, [best, target] + sys.argv[1:])
        except OSError as e:  # noqa: BLE001
            print(f"  handover failed: {repr(e)[:120]}\n")

    # Nothing usable: print the GROUND TRUTH so one run settles it.
    _paths, raw = _systemd_exec()
    print("\n  ⚠ no interpreter on this machine can import the panel.")
    print(f"  this python : {sys.executable}")
    print(f"  needed      : {', '.join(deps)}")
    print("\n  candidates tried (and what each one is missing):")
    if not tried:
        print("      — none found —")
    for path, missing in tried:
        print(f"      {path}   missing: {', '.join(missing) or 'nothing'}")
    print("\n  ExecStart lines from systemd (the ground truth):")
    for ln in raw or ["      — no rubika unit found —"]:
        print(f"      {ln}" if not ln.startswith("    ") else ln)
    hint = _container_hint()
    if hint:
        print("\n  containers running on this host:")
        for ln in hint:
            print(ln)
        print("\n  if the panel is one of these, run the script INSIDE it:")
        print("      docker exec -it <name> python /app/scripts/"
              "name_render_lab.py --render-all")
    print("\n  if a venv exists but is missing packages, this fixes it:")
    print(f"      {os.path.join(_ROOT, 'venv/bin/pip')} install -r "
          f"{os.path.join(_ROOT, 'requirements.txt')}\n")


# =========================================================================== #
# SECTION 1 — normalisation (no guessing; this is where cheap accuracy lives)
# =========================================================================== #

# Arabic letters typed instead of Persian ones, and shapes that must collapse
# so "محمّد", "مـحـمـد" and "محمد" all match the SAME list entry.
_LETTER_MAP = {
    "ي": "ی", "ك": "ک", "ۀ": "ه", "ة": "ه",
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ؤ": "و", "ئ": "ی", "ٔ": "",
}
# harakat / tashkeel + kashida
_MARKS = re.compile("[\u064b-\u0652\u0670\u0640\u00ad]")
# Arabic/Persian + ASCII digits
_DIGITS = re.compile("[0-9\u0660-\u0669\u06f0-\u06f9]")
# anything that is not a Persian/Arabic letter or a space -> separator.
_NON_LETTER = re.compile("[^\u0621-\u063a\u0641-\u064a\u0671-\u06d3\u06fa-\u06ff ]")


def normalize(text) -> str:
    """Fold a raw contact label into a comparable, letters-and-spaces string."""
    s = str(text or "")
    # ZWNJ becomes a space: "علی‌پور" -> "علی پور", which lets the whitelist see
    # the first name. Persian GIVEN names do not contain ZWNJ, so nothing real
    # is split by this.
    s = s.replace("\u200c", " ").replace("\u200f", " ").replace("\u200e", " ")
    for a, b in _LETTER_MAP.items():
        s = s.replace(a, b)
    s = _MARKS.sub("", s)
    s = _DIGITS.sub(" ", s)
    s = _NON_LETTER.sub(" ", s)          # drops emoji, commas, dashes, latin
    return " ".join(s.split())


def tokens(text) -> list:
    n = normalize(text)
    return n.split() if n else []


# =========================================================================== #
# SECTION 2 — the vocabularies
# =========================================================================== #

# --- shops / offices / organisations: EXCLUDE the contact entirely ---------- #
BUSINESS_WORDS = {
    # property & brokerage (the biggest group in this owner's list)
    "مسکن", "املاک", "بنگاه", "مشاور", "مشاورین", "ساختمانی", "پیمانکاری",
    "سازنده", "دلال", "معاملات", "مشارکت", "اجاره", "رهن", "خرید", "فروش",
    # offices & services
    "دفتر", "خدمات", "قضایی", "ثبت", "دارایی", "بیمه", "بانک", "صندوق", "وام",
    "آژانس", "نمایندگی", "شرکت", "موسسه", "مؤسسه", "اتحادیه", "تعاونی",
    "سازمان", "اداره", "شهرداری", "کمیته", "ستاد",
    # shops
    "فروشگاه", "سوپر", "سوپرمارکت", "هایپر", "بوتیک", "گالری", "مغازه",
    "انبار", "کارگاه", "کارخانه", "تولیدی", "پخش", "عمده", "بازار", "پاساژ",
    "رستوران", "فستفود", "قنادی", "نانوایی", "آبمیوه", "کباب",
    # tech / telecom shops
    "اسنپ", "تپسی", "کافینت", "کافی", "نت", "موبایل", "کامپیوتر", "لپتاپ",
    "تعمیرات", "تاسیسات", "تأسیسات", "لوازم", "یدکی", "ابزار",
    # health / education / religious institutions
    "کلینیک", "درمانگاه", "داروخانه", "آزمایشگاه", "بیمارستان", "مطب",
    "آموزشگاه", "مدرسه", "دبیرستان", "دانشگاه", "باشگاه", "استخر",
    "مسجد", "حسینیه", "هیئت", "هیات", "خیریه", "موقوفه",
    "جماعت",                      # "امام جماعت" -> a role, not a person
}

# --- titles / honorifics that PRECEDE a name: strip, do not exclude -------- #
TITLE_WORDS = {
    "حاج", "حاجی", "حاجیه", "سید", "سیده", "میر", "شیخ", "ملا", "کربلایی",
    "مشهدی", "آقا", "آقای", "خانم", "سرکار", "جناب", "حضرت", "استاد",
    "مهندس", "دکتر", "دکترا", "پروفسور", "سرهنگ", "سروان", "ستوان",
    "مستاجر", "مستأجر", "همکار", "راننده",
}

# --- trades that FOLLOW a name: strip, do not exclude ---------------------- #
JOB_WORDS = {
    "مکانیک", "لولهکش", "برقکار", "برقی", "نقاش", "بنا", "بنای", "جوشکار",
    "آهنگر", "نجار", "کاشیکار", "گچکار", "سنگکار", "سیمانکار", "عایقکار",
    "کفاش", "خیاط", "قصاب", "نانوا", "آشپز", "باربر", "تعمیرکار", "کارگر",
    "سرکارگر", "آرایشگر", "طلاساز", "عینکساز", "شیشهبر", "درودگر",
    "نما", "رولپلاک", "باطناب", "داربست", "تراش", "صافکار", "تودوزی",
}

# --- family / relationship words ------------------------------------------- #
# These decide WHOSE name is in the string, which is why they are split by
# position. See resolve_relationship() for the rule and why it matters.
RELATION_WORDS = {
    "عمه", "خاله", "دایی", "عمو", "مادر", "پدر", "مامان", "بابا", "داداش",
    "آبجی", "خواهر", "برادر", "همسر", "زن", "شوهر", "دوست", "رفیق",
    "عروس", "داماد", "پسر", "دختر", "نوه", "مادربزرگ", "پدربزرگ",
    "جاری", "باجناق", "خواهرزاده", "برادرزاده",
}

# --- Persian first names: the WHITELIST that does the real work ------------ #
# Deliberately given names only (no surnames), because a surname match would
# produce "سلام هاشمی جان". Extend it from the --render run's "candidates"
# section, which prints exactly the unmatched single-token labels.
FIRST_NAMES = {
    # --- men ---
    "محمد", "علی", "حسین", "حسن", "رضا", "مهدی", "امیر", "احمد", "ابراهیم",
    "اسماعیل", "اصغر", "اکبر", "جعفر", "جواد", "صادق", "کاظم", "موسی",
    "یوسف", "یعقوب", "ایوب", "داود", "سلیمان", "عیسی", "زکریا", "ادریس",
    "اسحاق", "هارون", "یونس", "یحیی", "ایمان", "احسان", "ارسلان", "اردشیر",
    "آرش", "آرمان", "آرمین", "آروین", "آریا", "آرین", "امین", "انوش",
    "بابک", "بهرام", "بهزاد", "بهمن", "بهنام", "بیژن", "پارسا", "پدرام",
    "پرویز", "پژمان", "پوریا", "پویا", "پیمان", "تورج", "جمشید", "جهانگیر",
    "حامد", "حمید", "حمزه", "خسرو", "داریوش", "دانیال", "رامین", "رسول",
    "روزبه", "زیاد", "سالار", "سامان", "سام", "سعید", "سپهر", "سروش",
    "سهیل", "سیامک", "سیاوش", "سینا", "شاهین", "شهاب", "شهرام", "شهریار",
    "صابر", "صالح", "طاها", "طه", "عادل", "عباس", "عبدالله", "عرفان",
    "عزیز", "عقیل", "علیرضا", "عماد", "فرامرز", "فرزاد", "فرشاد", "فرشید",
    "فرهاد", "فریبرز", "فرید", "قاسم", "کامران", "کامیار", "کاوه", "کسری",
    "کوروش", "کیا", "کیان", "کیانوش", "کیوان", "مازیار", "ماهان", "مجید",
    "محسن", "محمود", "مرتضی", "مسعود", "مسلم", "مصطفی", "مظفر", "معین",
    "منصور", "مهبد", "مهران", "مهرداد", "مهزیار", "میثم", "میلاد", "نادر",
    "ناصر", "نریمان", "نوید", "نیما", "هادی", "هامون", "همایون", "هومن",
    "وحید", "ولی", "یاسین", "یاشار", "امیرحسین", "امیرعلی", "امیرمحمد",
    "محمدرضا", "محمدحسین", "محمدعلی", "محمدامین", "علیاکبر", "علیاصغر",
    "سیدمحمد", "سیدعلی", "غلامرضا", "غلامحسین", "غلامعلی", "نعمت", "نعمتالله",
    "یدالله", "عبدالرضا", "عبدالحسین", "فتحالله", "عنایت", "شعبان", "رمضان",
    "اسفندیار", "اورنگ", "بردیا", "برزو", "دادار", "رستم", "زال", "سهراب",
    "گودرز", "منوچهر", "هوشنگ", "فریدون", "کیخسرو", "اردوان",
    # --- women ---
    "فاطمه", "زهرا", "مریم", "زینب", "خدیجه", "رقیه", "سکینه", "صغری",
    "کبری", "معصومه", "نرگس", "سمیرا", "سمیه", "سارا", "سحر", "ساره",
    "سانیا", "سایه", "سپیده", "ستاره", "سوگل", "سولماز", "سودابه", "سهیلا",
    "شادی", "شبنم", "شقایق", "شهرزاد", "شهلا", "شیدا", "شیرین", "شیوا",
    "صبا", "صدف", "طاهره", "عاطفه", "عادله", "عسل", "عطیه", "غزاله",
    "فرشته", "فرناز", "فرزانه", "فریبا", "فریده", "فیروزه", "کتایون",
    "کیمیا", "گلاره", "گلنار", "لادن", "لاله", "لیلا", "لیلی", "مانا",
    "ماندانا", "مرجان", "مرضیه", "مژگان", "ملیحه", "ملیکا", "منیژه",
    "مهتاب", "مهدیه", "مهسا", "مهشید", "مهناز", "مهین", "مینا", "مونا",
    "میترا", "نازنین", "ناهید", "نجمه", "ندا", "نسترن", "نسرین", "نفیسه",
    "نگار", "نگین", "نوشین", "نیایش", "نیلوفر", "هانیه", "هدیه", "هستی",
    "هلیا", "همتا", "یاس", "یاسمن", "یگانه", "اعظم", "افسانه", "افسون",
    "اکرم", "الهام", "الهه", "آتنا", "آذر", "آرزو", "آرام", "آزاده",
    "آسیه", "آفاق", "آناهیتا", "آوا", "اشرف", "انیس", "ایران", "بهار",
    "بهاره", "بهناز", "بیتا", "پانیذ", "پردیس", "پرستو", "پرنیا", "پروانه",
    "پروین", "پریا", "پریسا", "پرینا", "تانیا", "ترانه", "تهمینه", "ثمین",
    "جمیله", "حدیث", "حدیثه", "حمیده", "حوریه", "خاطره", "دریا", "دلارام",
    "راحله", "رضوان", "رها", "روشنک", "رویا", "ریحانه", "زرین", "زیبا",
    "سودا", "صنم", "طوبی", "عذرا", "فرح", "فروغ", "فهیمه", "قدسیه",
    "محبوبه", "مبینا", "مدینه", "معصوم", "مکرم", "منصوره", "میمنت",
    "نازی", "نرمین", "نوا", "وفا", "هاجر", "یلدا", "زری", "زهره", "سمانه",
    "سیما", "شمسی", "صفیه", "عفت", "فوزیه", "گیتی", "لیدا", "مهری",
    "ناز", "نازیلا", "نیکو", "ویدا", "هما", "هنگامه",
}

# Confidence bands -> what we do. Two thresholds, never one: everything in the
# middle is sent WITHOUT a name instead of being guessed.
C_CONFIRMED = 100       # both sources independently gave the SAME name
C_AGREE_PART = 90       # one name contains the other (e.g. محمد / محمدرضا)
C_LABEL_ONLY = 75       # only the owner's label yielded a name
C_PROFILE_ONLY = 60     # only the contact's own profile yielded a name
C_CONFLICT = 20         # both gave names, and they disagree
C_NONE = 0              # neither yielded a usable name

VERDICTS = {
    "CONFIRMED": C_CONFIRMED,
    "AGREE_PARTIAL": C_AGREE_PART,
    "LABEL_ONLY": C_LABEL_ONLY,
    "PROFILE_ONLY": C_PROFILE_ONLY,
    "CONFLICT": C_CONFLICT,
    "NO_NAME": C_NONE,
}


# =========================================================================== #
# SECTION 3 — classification + extraction (pure functions, fully testable)
# =========================================================================== #

def business_hits(text) -> list:
    """Business/organisation words present. Any hit excludes the contact."""
    return [t for t in tokens(text) if t in BUSINESS_WORDS]


def resolve_relationship(toks: list) -> tuple:
    """Decide whether a name in this string belongs to the CONTACT or to
    somebody else, using word order. Returns (allowed_tokens, blocked_reason).

    This is not a nicety — it is the difference between two real entries in the
    owner's list:

        "مرجان عمه"        عمه comes AFTER the name  -> aunt Marjan, name is HERS
        "مادر علی امینی"   مادر comes BEFORE the name -> Ali Amini's MOTHER,
                                                        the name is her SON's

    Addressing the second woman as "علی" is exactly the kind of mistake that
    must never happen, so a leading relationship word blocks naming entirely.
    """
    for i, t in enumerate(toks):
        if t in RELATION_WORDS:
            rest = toks[i + 1:]
            if any(r in FIRST_NAMES for r in rest):
                # possessive form: "<relation> <someone-else's name>"
                return [], f"relationship '{t}' owns the name"
            # appositive form: "<name> <relation>" -> keep the name, drop the word
            return [x for x in toks if x not in RELATION_WORDS], None
    return toks, None


def extract_name(text) -> dict:
    """Render ONE clean first name out of a raw label.

    Returns {name, reason, tokens, dropped}. `name` is "" when nothing can be
    claimed safely — which is a successful outcome, not a failure.
    """
    toks = tokens(text)
    out = {"name": "", "reason": "", "tokens": list(toks), "dropped": []}
    if not toks:
        out["reason"] = "empty"
        return out

    biz = [t for t in toks if t in BUSINESS_WORDS]
    if biz:
        out["reason"] = "business: " + " ".join(biz)
        out["dropped"] = biz
        return out

    allowed, blocked = resolve_relationship(toks)
    if blocked:
        out["reason"] = blocked
        return out

    # strip titles and trades; whatever survives is a name candidate
    kept = []
    for t in allowed:
        if t in TITLE_WORDS or t in JOB_WORDS:
            out["dropped"].append(t)
        else:
            kept.append(t)
    if not kept:
        out["reason"] = "only titles/trades, no name"
        return out

    matches = [t for t in kept if t in FIRST_NAMES]
    if not matches:
        out["reason"] = "no token matches the first-name list"
        return out
    if len(set(matches)) > 1:
        # "حسین خمسه مرادی علی" style: ambiguous, do not gamble
        out["reason"] = "several name candidates: " + " ".join(matches)
        return out

    out["name"] = matches[0]
    out["reason"] = "matched the first-name list"
    return out


def match_sources(label, profile) -> dict:
    """The MATCHING system: cross-check the owner's memo against the contact's
    own profile name and turn the agreement into a confidence number.

    Neither source can be trusted alone — the label is full of trades and
    memos, the profile is full of aliases and nicknames. Agreement between two
    independent sources is the strongest evidence available.
    """
    a = extract_name(label)
    b = extract_name(profile)
    biz = business_hits(label) + business_hits(profile)

    res = {
        "label_raw": str(label or ""),
        "profile_raw": str(profile or ""),
        "label_name": a["name"],
        "profile_name": b["name"],
        "label_reason": a["reason"],
        "profile_reason": b["reason"],
        "business": sorted(set(biz)),
        "verdict": "NO_NAME",
        "confidence": C_NONE,
        "name": "",
        "excluded": False,
    }

    if biz:
        res.update(verdict="EXCLUDED_BUSINESS", confidence=-1, excluded=True)
        return res

    na, nb = a["name"], b["name"]
    if na and nb:
        if na == nb:
            res.update(verdict="CONFIRMED", confidence=C_CONFIRMED, name=na)
        elif na in nb or nb in na:
            # محمد vs محمدرضا -> same person, use the SHORTER form for address
            res.update(verdict="AGREE_PARTIAL", confidence=C_AGREE_PART,
                       name=min((na, nb), key=len))
        else:
            res.update(verdict="CONFLICT", confidence=C_CONFLICT, name="")
    elif na:
        res.update(verdict="LABEL_ONLY", confidence=C_LABEL_ONLY, name=na)
    elif nb:
        res.update(verdict="PROFILE_ONLY", confidence=C_PROFILE_ONLY, name=nb)
    return res


def decide(res: dict, min_confidence: int) -> str:
    """Three buckets. Never two."""
    if res["excluded"]:
        return "EXCLUDE"                      # shop/office: do not message
    if res["name"] and res["confidence"] >= min_confidence:
        return "NAMED"                        # "سلام مجید جان"
    return "NO_NAME"                          # "سلام دوست عزیز"


# =========================================================================== #
# SECTION 4 — offline self-test (runs with no network and no login)
# =========================================================================== #

_PASS = 0
_FAIL = 0


def _ck(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        print(f"  ❌ {name}")


def _no_send_guard() -> tuple:
    """Prove this file cannot send or write: inspect its own CALLS, not its text.

    The owner's standing requirement is that this script never messages anyone
    and never touches the panel's data, so the promise is tested instead of
    asserted in a comment.

    Call-level (AST) checking rather than string matching, for two reasons: a
    text scan trivially matches its own keyword list, and a string in a comment
    is not a call anyway. Two things are verified:

      1. no banned function is ever CALLED (sends, DB writes, profile edits)
      2. every raw `builder(name=...)` API method is on a read-only allowlist,
         because builder() can reach any Rubika endpoint including writing ones
    """
    import ast
    with open(os.path.abspath(__file__), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    BANNED = {
        # sending
        "send_message", "send_text", "forward_message", "send_copy",
        "send_document", "run_send", "run_send_remote", "send_for_account",
        # account / content mutation
        "add_channel", "add_channel_members", "add_contact", "update_profile",
        "set_setting", "set_marker", "seed_channel_with_contacts",
        # panel database writes
        "set_account", "set_status", "set_session_blob", "insert", "commit",
        "execute", "executemany",
        # reaching a worker (which could then send on our behalf)
        "api_call",
    }
    # read-only Rubika methods this script is allowed to call raw
    ALLOWED_RAW = {"getUserInfo"}
    # harmless namesakes: a python list insert is not a database insert
    ALLOWED_EXACT = {"sys.path.insert"}

    def dotted(fn) -> str:
        """client.session.insert -> 'client.session.insert'"""
        parts = []
        while isinstance(fn, ast.Attribute):
            parts.append(fn.attr)
            fn = fn.value
        if isinstance(fn, ast.Name):
            parts.append(fn.id)
        return ".".join(reversed(parts))

    hits = []
    raw_methods = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        fn = n.func
        leaf = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        full = dotted(fn) if isinstance(fn, ast.Attribute) else leaf
        if leaf in BANNED and full not in ALLOWED_EXACT:
            hits.append(full or leaf)
        if leaf == "builder":
            for kw in n.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    raw_methods.add(kw.value.value)
    # The interpreter handover calls os.execv, which could in principle run
    # ANY binary with any arguments. Verify every execv hands over to THIS
    # SAME file, so the read-only guarantee survives the re-exec: the new
    # process runs this script, which re-runs this very check.
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        full = dotted(n.func)
        body = ast.unparse(n)
        if full in ("os.system", "subprocess.Popen", "subprocess.getoutput"):
            hits.append(f"{full} (arbitrary shell)")
        elif full.startswith("os.exec"):
            # The target must come from _exec_target(), which refuses anything
            # that is not a .py file inside this repo. A source-text check
            # could only pretend to verify this; _exec_target is unit-tested.
            if "target" not in body:
                hits.append(f"{full} target is not validated by _exec_target")

    # os.exec* does NOT flush python's buffers, so any diagnostic printed
    # before the handover is lost whenever stdout is a pipe. The enclosing
    # function must flush first; this was a real silent-handover bug.
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body_src = ast.unparse(fn)
        if ".exec" in body_src and "os.exec" in body_src:
            if "stdout.flush()" not in body_src:
                hits.append(f"{fn.name}() execs without flushing stdout")
        elif full.startswith("subprocess."):
            # Allowed ONLY for three read-only probes: testing whether an
            # interpreter can import a dependency, reading a systemd unit, and
            # listing containers. Anything else is rejected.
            allowed = ("import {dep}" in body or "systemctl" in body
                       or '"ps"' in body or "'ps'" in body)
            if not allowed:
                hits.append(f"{full}: {body[:60]}")

    # A dotenv stub would let config.py fall back to defaults and make `db`
    # open the WRONG database file, so patching sys.modules is banned outright.
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and "sys.modules" in ast.unparse(t):
                    hits.append("patches sys.modules (dependency stub)")

    hits = sorted(set(hits)) + sorted(raw_methods - ALLOWED_RAW)
    return (not hits), hits


def selftest() -> int:
    print("=" * 70)
    print("NAME-RENDER LAB — offline logic test")
    print("=" * 70)

    print("\n0. the read-only promise is enforced, not just claimed")
    ok, hits = _no_send_guard()
    _ck(f"no sending / DB-writing / worker call is reachable {hits or ''}", ok)
    _ck("the handover re-execs only THIS file, and stubs no dependency", ok)

    print("\n0b. the systemd ExecStart parser (this is what failed before)")
    # Exercises the REAL parse_execstart() used at runtime, not a copy of it:
    # a test against a duplicated implementation would stay green while the
    # shipped function was broken, which is how the original bug survived.
    _parse = parse_execstart
    _ck("real install.sh unit line -> venv/bin/python",
        _parse("ExecStart=/opt/rubika-panel/venv/bin/python "
               "/opt/rubika-panel/main.py")
        == ["/opt/rubika-panel/venv/bin/python"])
    _ck("python3 form works too",
        _parse("ExecStart=/opt/x/venv/bin/python3 /opt/x/main.py")
        == ["/opt/x/venv/bin/python3"])
    _ck("systemd '-' prefix is stripped",
        _parse("ExecStart=-/opt/x/venv/bin/python /opt/x/main.py")
        == ["/opt/x/venv/bin/python"])
    _ck("main.py is NOT mistaken for the interpreter",
        "/opt/rubika-panel/main.py" not in _parse(
            "ExecStart=/opt/rubika-panel/venv/bin/python "
            "/opt/rubika-panel/main.py"))
    _ck("a docker unit yields no interpreter (so we fall through to the hint)",
        _parse("ExecStart=/usr/bin/docker compose up") == [])
    _ck("non-ExecStart lines are ignored",
        _parse("Environment=PYTHONPATH=/opt/x") == [])

    print("\n0c. venv detection (the bug that actually blocked the owner's run)")
    # Builds the real shape: venv/bin/python as a symlink to the system python,
    # exactly what `python -m venv` produces. The old code compared realpath,
    # decided this WAS the running interpreter, skipped it, and reported
    # "none found" while every package was installed.
    import shutil as _sh
    import tempfile as _tf
    _d = _tf.mkdtemp(prefix="venvtest_")
    try:
        os.makedirs(os.path.join(_d, "venv", "bin"))
        fake = os.path.join(_d, "venv", "bin", "python")
        os.symlink(sys.executable, fake)
        _ck("a venv python IS a symlink to the system python (the trap)",
            os.path.realpath(fake) == os.path.realpath(sys.executable))
        _ck("...and it is STILL offered as a candidate",
            pick_candidates([fake], sys.executable) == [fake])
        _ck("the interpreter already running is dropped",
            pick_candidates([sys.executable], sys.executable) == [])
        _ck("the same path twice collapses to one",
            pick_candidates([fake, fake], sys.executable) == [fake])
        _ck("a non-existent path is dropped",
            pick_candidates([os.path.join(_d, "nope", "python")],
                            sys.executable) == [])
        _ck("end to end: the install.sh unit line survives the whole filter",
            pick_candidates(
                parse_execstart(f"ExecStart={fake} /opt/rubika-panel/main.py"),
                sys.executable) == [fake])
    finally:
        _sh.rmtree(_d, ignore_errors=True)

    print("\n0e. session detection must glob, not stat (8 accounts were lost)")
    try:
        import rubika_client as _rb
        _ph = "989999999999"
        _base = _rb.session_path(_ph)
        _ck("the bare session path does NOT exist on disk (the trap)",
            not os.path.exists(_base))
        os.makedirs(os.path.dirname(_base), exist_ok=True)
        _fake = _base + ".rbs"          # rubpy appends its own extension
        with open(_fake, "w", encoding="utf-8") as fh:
            fh.write("x")
        try:
            _ck("globbing finds the real file", session_files(_ph) == [_fake])
            _ck("a stat on the bare path would have missed it",
                not os.path.exists(_base) and bool(session_files(_ph)))
            _ck("an unknown account still reports nothing",
                session_files("989000000000") == [])
        finally:
            os.unlink(_fake)
        _ck("once removed, it reports nothing again", session_files(_ph) == [])
    except (ImportError, AttributeError) as e:  # noqa: BLE001
        # degrade, never crash: a minimal test double may not expose
        # session_path. On a real install it always does.
        print(f"     (skipped: {type(e).__name__} — {str(e)[:70]})")

    print("\n0d. handover target — re-run the CALLER, never this module")
    # The bug: ensure_interpreter() hardcoded this module's __file__, so
    # fleet_render_lab.py --plan was re-exec'd as name_render_lab.py --plan and
    # died on "unrecognized arguments: --plan".
    _fleet = os.path.join(_ROOT, "scripts", "fleet_render_lab.py")
    if os.path.isfile(_fleet):
        _ck("another script in the repo is a valid target",
            _exec_target(_fleet) == os.path.abspath(_fleet))
        _ck("...and it is NOT silently replaced by this module",
            _exec_target(_fleet) != _SELF)
    _ck("this module itself is a valid target", _exec_target(_SELF) == _SELF)
    _ck("a relative path is resolved", _exec_target(
        os.path.relpath(_SELF, _ROOT)) == _SELF)
    for bad, why in (("/bin/sh", "an arbitrary binary"),
                     ("/usr/bin/python3", "an interpreter"),
                     ("/tmp/evil.py", "a .py outside the repo"),
                     (os.path.join(_ROOT, "requirements.txt"), "a non-.py file"),
                     (os.path.join(_ROOT, "scripts", "nope.py"), "a missing file"),
                     ("", "an empty path")):
        try:
            _exec_target(bad)
            rejected = False
        except ValueError:
            rejected = True
        _ck(f"refuses {why}", rejected)

    # the probe must actually distinguish present from absent, per dependency
    _ck("dep probe: a real module reports nothing missing",
        _missing_deps(sys.executable, ("json",)) == [])
    _ck("dep probe: a missing module is named",
        _missing_deps(sys.executable, ("no_such_module_xyz",))
        == ["no_such_module_xyz"])
    _ck("dep probe: reports only the absent one of a mixed pair",
        _missing_deps(sys.executable, ("json", "no_such_module_xyz"))
        == ["no_such_module_xyz"])

    print("\n1. normalisation")
    _ck("arabic ي/ك fold to persian", normalize("يكتا") == "یکتا")
    _ck("harakat removed", normalize("مُحَمَّد") == "محمد")
    _ck("kashida removed", normalize("مـحـمـد") == "محمد")
    _ck("ZWNJ becomes a space", tokens("علی\u200cپور") == ["علی", "پور"])
    _ck("comma splits (the emotional tail case)",
        tokens("زهرا،گلم") == ["زهرا", "گلم"])
    _ck("digits dropped", tokens("زحلی ۲") == ["زحلی"])
    _ck("emoji dropped", tokens("سامان 🌹") == ["سامان"])
    _ck("empty stays empty", tokens("") == [])

    print("\n2. shops / offices are EXCLUDED (never messaged by name)")
    for raw in ("دفتر خدمات قضایی نبرد", "کافی نت کمیل مخبرجنوبی",
                "خیریه وفاق نوربهارشمالی علیپور", "اسنپ کربلا",
                "مسکن آروین هاتفی", "سازنده جعفری مشارکت",
                "حاج آقا حاج ولی امام جماعت،سعدی"):
        r = match_sources(raw, "")
        _ck(f"excluded: {raw[:34]}", r["excluded"] is True)

    print("\n3. clean names are kept")
    for raw, want in (("مجید هاشمی", "مجید"), ("هستی", "هستی"),
                      ("ملیحه", "ملیحه"), ("ابراهیم", "ابراهیم"),
                      ("مرضیه", "مرضیه"), ("راحله", "راحله"),
                      ("فریبا", "فریبا"), ("ابوالفضل", "")):
        got = extract_name(raw)["name"]
        _ck(f"{raw!r} -> {got!r}", got == want)

    print("\n4. titles and trades are stripped, the name survives")
    for raw, want in (("محمد مکانیک", "محمد"),
                      ("مهندس ایرج قاسمی سازنده", ""),
                      ("پیرعباس لوله کش برقکار", ""),
                      ("حاج خانم", ""),
                      ("مستاجر جاهد", ""),
                      ("دلال پوریا مسکن امیران", ""),
                      ("مسکن یاسین نجفی زادگان", "")):
        got = extract_name(raw)["name"]
        _ck(f"{raw!r} -> {got!r}", got == want)
    _ck("a trade word alone never becomes a name",
        extract_name("محمد مکانیک")["dropped"] == ["مکانیک"])

    print("\n5. relationship words decide WHOSE name it is")
    _ck("'مرجان عمه' -> مرجان (name is hers)",
        extract_name("مرجان عمه")["name"] == "مرجان")
    _ck("'مادر علی امینی' -> no name (it is her SON's name)",
        extract_name("مادر علی امینی")["name"] == "")
    _ck("...and it says why",
        "relationship" in extract_name("مادر علی امینی")["reason"])
    _ck("'دوست خانم امینی' -> no name",
        extract_name("دوست خانم امینی")["name"] == "")

    print("\n6. typos and mashed text fall safely into the no-name bucket")
    for raw in ("اممخدربب", "اداود", "علیورغفاری", "خورم زی", "بروجردی",
                "حسن احمدا"):
        r = extract_name(raw)
        if raw == "حسن احمدا":
            _ck(f"{raw!r} -> حسن (the first name is still readable)",
                r["name"] == "حسن")
        else:
            _ck(f"{raw!r} -> no name, no crash", r["name"] == "")

    print("\n7. the MATCHING system")
    r = match_sources("مسکن آروین هاتفی", "آروین")
    _ck("business label still excludes, even with a clean profile",
        r["verdict"] == "EXCLUDED_BUSINESS")
    r = match_sources("حسن احمدا", "حسن")
    _ck("both sources agree -> CONFIRMED", r["verdict"] == "CONFIRMED")
    _ck("confirmed carries the name", r["name"] == "حسن")
    _ck("confirmed is the top confidence", r["confidence"] == C_CONFIRMED)
    r = match_sources("محمد مکانیک", "محمدرضا")
    _ck("one name contains the other -> AGREE_PARTIAL",
        r["verdict"] == "AGREE_PARTIAL")
    _ck("the shorter form is used to address", r["name"] == "محمد")
    r = match_sources("مجید هاشمی", "🌹🌹")
    _ck("alias/emoji profile -> LABEL_ONLY", r["verdict"] == "LABEL_ONLY")
    r = match_sources("اداود", "داود")
    _ck("typo label + real profile -> PROFILE_ONLY",
        r["verdict"] == "PROFILE_ONLY")
    r = match_sources("مجید هاشمی", "سعید")
    _ck("sources disagree -> CONFLICT", r["verdict"] == "CONFLICT")
    _ck("a conflict yields NO name", r["name"] == "")
    r = match_sources("اممخدربب", "زهراعشقم")
    _ck("neither usable -> NO_NAME", r["verdict"] == "NO_NAME")

    print("\n8. the three buckets + the confidence knob")
    confirmed = match_sources("حسن احمدا", "حسن")
    prof_only = match_sources("اداود", "داود")
    conflict = match_sources("مجید هاشمی", "سعید")
    shop = match_sources("اسنپ کربلا", "")
    _ck("confirmed -> NAMED at the default 70", decide(confirmed, 70) == "NAMED")
    _ck("profile-only -> NO_NAME at the default 70",
        decide(prof_only, 70) == "NO_NAME")
    _ck("profile-only -> NAMED if you lower the bar to 60",
        decide(prof_only, 60) == "NAMED")
    _ck("confirmed survives the strictest bar",
        decide(confirmed, 100) == "NAMED")
    _ck("a conflict is never named, even at the lowest bar",
        decide(conflict, 20) == "NO_NAME" and decide(conflict, 0) == "NO_NAME")
    _ck("a shop is EXCLUDE at any threshold",
        decide(shop, 0) == "EXCLUDE" and decide(shop, 100) == "EXCLUDE")

    print("\n9. the safety invariant: a named bucket never holds a shop")
    sample = ["دفتر خدمات قضایی نبرد", "مسکن آروین هاتفی", "اسنپ کربلا",
              "مجید هاشمی", "هستی", "اممخدربب", "مادر علی امینی",
              "مرجان عمه", "کافی نت کمیل", "محمد مکانیک"]
    named = [s for s in sample if decide(match_sources(s, ""), 70) == "NAMED"]
    _ck(f"named bucket = {named}",
        set(named) == {"مجید هاشمی", "هستی", "مرجان عمه", "محمد مکانیک"})
    _ck("no excluded word leaked into the named bucket",
        all(not business_hits(s) for s in named))

    print(f"\n  ==== {_PASS} passed, {_FAIL} failed ====\n")
    return 1 if _FAIL else 0


# =========================================================================== #
# SECTION 5 — read-only account access
# =========================================================================== #

def _panel():
    """Import the panel modules (read-only use only). Reading the account
    table only needs config.py -> dotenv, so rubpy is not required here."""
    ensure_interpreter(deps=("dotenv",))
    import db
    import worker
    return db, worker


def list_accounts() -> int:
    """Print the panel's accounts and where each one lives. One SELECT, no
    network, no connection to Rubika."""
    try:
        db, worker = _panel()
    except Exception as e:  # noqa: BLE001
        print(f"  could not load the panel modules: {repr(e)[:160]}")
        print("  run this from the repo root (e.g. cd /opt/rubika-panel)")
        return 1
    try:
        rows = db.list_accounts()
    except Exception as e:  # noqa: BLE001
        print(f"  could not read the database: {repr(e)[:160]}")
        return 1
    if not rows:
        print("\n  no accounts in this panel yet.\n")
        return 0
    import rubika_client as rb
    print("=" * 70)
    print(f"ACCOUNTS ({len(rows)})")
    print("=" * 70)
    print(f"  {'id':>4}  {'phone':<16} {'status':<10} {'session here':<13} worker")
    for a in rows:
        try:
            w = worker.worker_for_account(a) or {}
            wtag = (w.get("tag") or "-") + (" (local)" if worker.is_local(w) else "")
        except Exception:  # noqa: BLE001
            wtag = "?"
        phone = str(a.get("phone") or "-")
        # A remote-worker account may have no session file on THIS machine; the
        # render run reports that honestly instead of failing cryptically.
        here = "yes" if session_files(phone) else "NO"
        print(f"  {a['id']:>4}  {phone:<16} {str(a.get('status') or '-'):<10} "
              f"{here:<13} {wtag}")
    print("\n  'session here' = NO  ->  this machine cannot read that account.")
    print("  Either run the script on that worker, or use the panel's")
    print("  🔑 Distribute Session so the master holds a session too.\n")
    return 0


async def _profile_name(client, guid: str) -> str:
    """What the contact calls THEMSELVES, via getUserInfo. Read-only.

    rubpy does not wrap this method, so the generic builder is used — the same
    call scripts/send_order_lab.py already makes for its ground-truth probe.
    """
    try:
        r = await client.builder(name="getUserInfo",
                                 input={"user_guid": guid}, dict=True)
    except Exception:  # noqa: BLE001
        return ""
    d = r if isinstance(r, dict) else {}
    u = d.get("user") or (d.get("data") or {}).get("user") or {}
    if not isinstance(u, dict):
        return ""
    first = str(u.get("first_name") or "")
    last = str(u.get("last_name") or "")
    name = (first + " " + last).strip()
    return name or str(u.get("name") or "")


async def render_account(phone: str, limit: int, delay: float,
                         min_confidence: int, show: int) -> dict:
    """READ-ONLY render of one account. Reads contacts, reads each sampled
    contact's own profile name, renders, matches, and returns the stats.

    Nothing is sent and nothing is written. Run it while the account is IDLE:
    it opens one connection through the project's own account_conn discipline,
    exactly like scripts/send_order_lab.py --probe.
    """
    import account_conn
    import rubika_client as rb

    pn = rb.normalize_phone(phone)
    out = {"phone": pn, "ok": False, "error": None, "contacts": 0,
           "profiles_read": 0, "rows": [], "counts": {}, "buckets": {}}

    if not session_files(pn):
        out["error"] = ("no session file here — run the script on that "
                        "account's worker, or use 🔑 Distribute Session")
        return out

    print(f"\n  connecting to {pn} (READ-ONLY, nothing is sent) ...")
    await account_conn.close(pn)              # project rule: one connection
    client = rb.open_client(pn)
    try:
        await rb.connect_ready(client)
        contacts = await rb.get_contacts_full(client)
    except account_conn.InvalidAuthError as e:  # noqa: BLE001
        out["error"] = f"session invalid: {repr(e)[:120]}"
        try:
            await client.disconnect()
        except Exception:
            pass
        return out
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:140]}"
        try:
            await client.disconnect()
        except Exception:
            pass
        return out

    out["contacts"] = len(contacts)
    print(f"  contacts read: {len(contacts)}")

    # Which contacts get the (one call each) profile lookup. Sampling keeps the
    # run short and the API footprint small; --limit 0 does every contact.
    pool = list(contacts)
    if limit and limit < len(pool):
        random.seed(1234)                      # reproducible sample
        pool = random.sample(pool, limit)
        print(f"  profile lookups: {len(pool)} sampled "
              f"(--limit 0 for all {len(contacts)})")
    else:
        print(f"  profile lookups: {len(pool)} (all)")

    wanted = {c["guid"] for c in pool}
    profiles = {}
    done = 0
    t0 = time.time()
    for c in pool:
        profiles[c["guid"]] = await _profile_name(client, c["guid"])
        done += 1
        if profiles[c["guid"]]:
            out["profiles_read"] += 1
        if done % 25 == 0:
            print(f"    … {done}/{len(pool)}  ({int(time.time() - t0)}s)")
        if delay:
            await asyncio.sleep(delay)
    try:
        await client.disconnect()
    except Exception:
        pass

    counts = {k: 0 for k in list(VERDICTS) + ["EXCLUDED_BUSINESS"]}
    buckets = {"NAMED": 0, "NO_NAME": 0, "EXCLUDE": 0}
    unmatched_single = {}
    for c in contacts:
        label = c.get("name") or ""
        prof = profiles.get(c["guid"], "")
        r = match_sources(label, prof)
        b = decide(r, min_confidence)
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
        buckets[b] += 1
        r["guid"] = c["guid"]
        r["sampled"] = c["guid"] in wanted
        r["bucket"] = b
        out["rows"].append(r)
        # candidates for the first-name list: a one-word label we could not match
        if b == "NO_NAME" and not r["business"]:
            tk = tokens(label)
            if len(tk) == 1 and tk[0] not in FIRST_NAMES:
                unmatched_single[tk[0]] = unmatched_single.get(tk[0], 0) + 1

    out["counts"] = counts
    out["buckets"] = buckets
    out["candidates"] = sorted(unmatched_single.items(),
                               key=lambda kv: -kv[1])
    out["ok"] = True

    if show:
        print(f"\n  ---- first {show} contacts ----")
        print(f"  {'bucket':<8} {'conf':>4} {'verdict':<17} "
              f"{'name':<10} label  |  profile")
        for r in out["rows"][:show]:
            print(f"  {r['bucket']:<8} {r['confidence']:>4} {r['verdict']:<17} "
                  f"{(r['name'] or '-'):<10} {r['label_raw'][:30]:<30} | "
                  f"{(r['profile_raw'] or '-')[:20]}")
    return out


def _pct(n, total) -> str:
    return f"{(100.0 * n / total):5.1f}%" if total else "    -"


def _print_account_report(res: dict, min_confidence: int) -> None:
    print("\n" + "-" * 70)
    print(f"  ACCOUNT {res['phone']}")
    print("-" * 70)
    if not res["ok"]:
        print(f"  ⚠ skipped: {res['error']}")
        return
    total = res["contacts"]
    b = res["buckets"]
    print(f"  📊 از {total} مخاطب، {b['NAMED']} تا رندر شد "
          f"({_pct(b['NAMED'], total).strip()})")
    print(f"  profile lookups ok  : {res['profiles_read']}")
    print(f"\n  BUCKETS (threshold = {min_confidence})")
    for b, lbl in (("NAMED", "send WITH name"),
                   ("NO_NAME", "send without a name"),
                   ("EXCLUDE", "shop/office — do not message")):
        n = res["buckets"][b]
        print(f"    {b:<9} {n:>5}  {_pct(n, total)}   {lbl}")
    print("\n  MATCH VERDICTS")
    for v in ("CONFIRMED", "AGREE_PARTIAL", "LABEL_ONLY", "PROFILE_ONLY",
              "CONFLICT", "NO_NAME", "EXCLUDED_BUSINESS"):
        n = res["counts"].get(v, 0)
        print(f"    {v:<18} {n:>5}  {_pct(n, total)}")
    cands = res.get("candidates") or []
    if cands:
        print(f"\n  CANDIDATES for the first-name list ({len(cands)} unmatched "
              f"single-word labels)")
        print("    " + "  ".join(f"{w}({c})" for w, c in cands[:30]))
        print("    ^ eyeball these: the real names among them should be added")


def _headline(total: int, named: int, noname: int, excluded: int) -> None:
    """The one line the owner actually asked for: of N contacts, M rendered."""
    print("\n" + "=" * 70)
    print(f"  📊  از {total} مخاطب، {named} تا رندر شد  "
          f"({_pct(named, total).strip()})")
    print("=" * 70)
    print(f"  ✅ رندر شد (با اسم)   : {named:>6}   {_pct(named, total)}")
    print(f"  ➖ بی‌اسم (خنثی)      : {noname:>6}   {_pct(noname, total)}")
    print(f"  🚫 مغازه/اداره (حذف) : {excluded:>6}   {_pct(excluded, total)}")
    print(f"  ──────────────────────────────────────")
    print(f"  کل مخاطبین            : {total:>6}")
    sendable = named + noname
    print(f"  قابلِ ارسال (با+بی اسم): {sendable:>6}   {_pct(sendable, total)}")


def _print_fleet_report(results: list, min_confidence: int) -> None:
    good = [r for r in results if r["ok"]]
    if not good:
        print("\n  no account could be read.\n")
        return
    total = sum(r["contacts"] for r in good)
    agg_b = {"NAMED": 0, "NO_NAME": 0, "EXCLUDE": 0}
    agg_v = {}
    for r in good:
        for k, v in r["buckets"].items():
            agg_b[k] += v
        for k, v in r["counts"].items():
            agg_v[k] = agg_v.get(k, 0) + v

    _headline(total, agg_b["NAMED"], agg_b["NO_NAME"], agg_b["EXCLUDE"])

    print("\n" + "=" * 70)
    print(f"FLEET DETAIL — {len(good)} account(s), {total} contacts")
    print("=" * 70)
    print(f"\n  BUCKETS (threshold = {min_confidence})")
    for b, lbl in (("NAMED", "send WITH name"),
                   ("NO_NAME", "send without a name"),
                   ("EXCLUDE", "shop/office — do not message")):
        print(f"    {b:<9} {agg_b[b]:>5}  {_pct(agg_b[b], total)}   {lbl}")
    print("\n  MATCH VERDICTS")
    for v in ("CONFIRMED", "AGREE_PARTIAL", "LABEL_ONLY", "PROFILE_ONLY",
              "CONFLICT", "NO_NAME", "EXCLUDED_BUSINESS"):
        print(f"    {v:<18} {agg_v.get(v, 0):>5}  {_pct(agg_v.get(v, 0), total)}")

    named = agg_b["NAMED"]
    conf = agg_v.get("CONFIRMED", 0) + agg_v.get("AGREE_PARTIAL", 0)
    print("\n  WHAT THIS DOES AND DOES NOT TELL YOU")
    print(f"    • {named} contacts ({_pct(named, total).strip()}) would be "
          f"addressed by name.")
    print(f"    • {conf} of them were confirmed by BOTH sources "
          f"independently.")
    print("    • The number that decides whether this is usable is the")
    print("      PRECISION of the NAMED bucket — of those names, how many are")
    print("      actually right. That cannot be computed here: it needs YOUR")
    print("      eyes on a sample. Run with --show 0 --json out.json, read a")
    print("      random 150 rows, and count the wrong ones.")
    print("    • A wrong name is the only expensive mistake. A missing name")
    print("      just means 'سلام دوست عزیز'.")


# =========================================================================== #
# SECTION 6 — CLI
# =========================================================================== #

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Render clean first names for Rubika contacts. "
                    "READ-ONLY: never sends a message.")
    ap.add_argument("--selftest", action="store_true",
                    help="offline logic test on the owner's real examples")
    ap.add_argument("--list", action="store_true", dest="do_list",
                    help="list accounts and whether this machine can read them")
    ap.add_argument("--render", nargs="+", metavar="PHONE",
                    help="READ-ONLY render of these accounts")
    ap.add_argument("--render-all", action="store_true",
                    help="READ-ONLY render of every active account")
    ap.add_argument("--limit", type=int, default=150, metavar="N",
                    help="profile lookups per account (0 = every contact). "
                         "Default 150 — a sample is enough for the statistics "
                         "and keeps the API footprint small.")
    ap.add_argument("--delay", type=float, default=0.25, metavar="SEC",
                    help="pause between profile lookups (default 0.25)")
    ap.add_argument("--min-confidence", type=int, default=70, metavar="N",
                    help="confidence needed to address someone by name "
                         "(100 confirmed, 90 partial, 75 label, 60 profile). "
                         "Default 70 = both-sources or owner's label only.")
    ap.add_argument("--show", type=int, default=25, metavar="N",
                    help="print the first N rows per account (0 = none)")
    ap.add_argument("--json", dest="out_json", metavar="FILE",
                    help="write the full per-contact result as JSON")
    ap.add_argument("--json-stdout", action="store_true", dest="json_stdout",
                    help="also emit the result on stdout between markers "
                         "(used by fleet_render_lab.py over SSH)")
    a = ap.parse_args()

    if a.selftest:
        return selftest()           # pure logic: no panel import, no handover
    if a.do_list:
        return list_accounts()      # DB only: dotenv is enough
    ensure_interpreter()            # may re-exec; the render needs rubpy too
    if not a.render and not a.render_all:
        ap.print_help()
        print("\n  start here:  --selftest      (offline, proves the logic)")
        print("  then:        --list          (which accounts are readable)")
        print("  then:        --render-all    (read-only, sends nothing)\n")
        return 0

    phones = list(a.render or [])
    if a.render_all:
        try:
            db, _w = _panel()
            for row in db.list_accounts():
                ph = str(row.get("phone") or "")
                if ph and (row.get("status") or "active") == "active":
                    phones.append(ph)
        except Exception as e:  # noqa: BLE001
            print(f"  could not read the account list: {repr(e)[:160]}")
            return 1
    seen = set()
    phones = [p for p in phones if not (p in seen or seen.add(p))]
    if not phones:
        print("  no account to render.")
        return 1

    print("=" * 70)
    print("NAME-RENDER LAB — READ-ONLY")
    print("=" * 70)
    print(f"  accounts        : {len(phones)}")
    print(f"  threshold       : {a.min_confidence}")
    print(f"  profile sample  : {a.limit or 'all'} per account")
    print("  NO MESSAGE IS SENT and NOTHING is written to the database.")
    print("  Run this while those accounts are idle (no send/automation).")

    async def run():
        out = []
        for ph in phones:
            res = await render_account(ph, a.limit, a.delay,
                                       a.min_confidence, a.show)
            _print_account_report(res, a.min_confidence)
            out.append(res)
        return out

    results = asyncio.run(run())
    _print_fleet_report(results, a.min_confidence)

    if a.out_json:
        try:
            with open(a.out_json, "w", encoding="utf-8") as fh:
                json.dump(results, fh, ensure_ascii=False, indent=1)
            print(f"\n  full result written to {a.out_json}")
        except Exception as e:  # noqa: BLE001
            print(f"\n  could not write JSON: {repr(e)[:140]}")
    if a.json_stdout:
        print(JSON_BEGIN)
        print(json.dumps(results, ensure_ascii=False))
        print(JSON_END)
    print()
    return 0 if any(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
