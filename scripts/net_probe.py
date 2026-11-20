#!/usr/bin/env python3
"""
🌐 net_probe — master↔worker connectivity diagnostic (Iran/abroad).
===================================================================

Problem: a master abroad cannot open a connection TO a worker in Iran, while
the reverse (Iran -> abroad) usually works. This script MEASURES exactly which
direction is open, on which ports, with a real end-to-end payload (not just a
half-open SYN that a middlebox might answer), and then RECOMMENDS a working
method (direct, flip-direction, reverse SSH tunnel, or an alternate port).

Pure standard library — no pip installs. Works on a minimal Linux box
(Python 3.6+). Shell tools (ping/traceroute/ss) are used only if present and
their absence never breaks the run.

Roles (run on BOTH servers):

  1) On the LISTENER side, open test servers on several candidate ports:
        python3 net_probe.py --listen
     (ports default: 22022 2222 443 8443 8080 80 2096 9000 — change with
      --ports). Leave it running while the other side probes.

  2) On the PROBER side, probe the listener's public IP:
        python3 net_probe.py --probe <OTHER_PUBLIC_IP>
     It reports, per port, whether a FULL round-trip token echo succeeded.

  3) Local facts (interfaces, egress IP, open ports, route, MTU):
        python3 net_probe.py --info

  4) General outbound reachability (is the internet reachable at all?):
        python3 net_probe.py --egress

Run --probe FROM abroad TO Iran, and also FROM Iran TO abroad. Comparing the
two tells you which direction to build the tunnel in.

  --selftest   run offline self-checks (loopback listen+probe, parsers).
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time

DEFAULT_PORTS = [22022, 2222, 443, 8443, 8080, 80, 2096, 9000]
TOKEN = "NETPROBE-OK"
BANNER = b"NETPROBE listener ready\n"
CONNECT_TIMEOUT = 6.0


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=12):
    """Run a shell command, return (rc, out) — never raises."""
    try:
        p = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace")
    except FileNotFoundError:
        return 127, "(command not found)"
    except subprocess.TimeoutExpired:
        return 124, "(timed out)"
    except Exception as e:  # noqa: BLE001
        return 1, f"(error: {e})"


def _have(cmd):
    rc, _ = _run(f"command -v {cmd}", timeout=5)
    return rc == 0


# --------------------------------------------------------------------------- #
# LISTENER — bind a tiny echo server on each candidate port.
# --------------------------------------------------------------------------- #
def _serve_one(port, results):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.listen(16)
        results[port] = "listening"
    except Exception as e:  # noqa: BLE001
        results[port] = f"bind failed: {e}"
        return
    while True:
        try:
            conn, addr = s.accept()
        except Exception:  # noqa: BLE001
            break
        threading.Thread(target=_handle, args=(conn, addr, port),
                         daemon=True).start()


def _handle(conn, addr, port):
    try:
        conn.settimeout(10)
        conn.sendall(BANNER)
        data = conn.recv(256)
        # echo back whatever token the prober sent, tagged with the port, so the
        # prober can confirm a REAL end-to-end path to THIS server (not a
        # transparent-proxy SYN-ACK).
        tok = (data or b"").strip().decode(errors="replace")
        reply = f"{TOKEN} port={port} from={addr[0]} echo={tok}\n".encode()
        conn.sendall(reply)
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def cmd_listen(ports):
    results = {}
    threads = []
    for p in ports:
        t = threading.Thread(target=_serve_one, args=(p, results), daemon=True)
        t.start()
        threads.append(t)
    time.sleep(0.5)
    print("🌐 LISTENER up. Leave this running while the other side probes.\n")
    for p in ports:
        state = results.get(p, "starting")
        mark = "✅" if state == "listening" else "❌"
        print(f"  {mark} port {p:<6} {state}")
    bound = [p for p in ports if results.get(p) == "listening"]
    if not bound:
        print("\n⚠ No port could be bound. Try running as root for <1024, or "
              "pick high ports with --ports.")
        return 1
    print(f"\n  Listening on: {', '.join(str(p) for p in bound)}")
    print("  On the OTHER server run:")
    print(f"    python3 net_probe.py --probe <THIS_SERVER_PUBLIC_IP> "
          f"--ports {' '.join(str(p) for p in bound)}")
    print("\n  (Ctrl-C to stop.)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


# --------------------------------------------------------------------------- #
# PROBER — connect to each port, do a token round-trip, time it.
# --------------------------------------------------------------------------- #
def _probe_tcp(host, port, timeout=CONNECT_TIMEOUT):
    r = {"port": port, "tcp": False, "echo": False, "rtt_ms": None,
         "detail": ""}
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        r["detail"] = f"connect failed: {type(e).__name__}: {e}"
        return r
    r["tcp"] = True
    r["rtt_ms"] = round((time.time() - t0) * 1000, 1)
    try:
        s.settimeout(timeout)
        # read banner (listener sends one), then send our token, read echo.
        try:
            s.recv(256)
        except Exception:  # noqa: BLE001
            pass
        s.sendall((TOKEN + "\n").encode())
        back = s.recv(256).decode(errors="replace")
        if TOKEN in back:
            r["echo"] = True
            r["detail"] = back.strip()[:120]
        else:
            r["detail"] = f"connected, but no token echo (got {back.strip()[:60]!r})"
    except Exception as e:  # noqa: BLE001
        r["detail"] = f"connected, no echo: {type(e).__name__}: {e}"
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    return r


def _probe_tls(host, port=443, timeout=CONNECT_TIMEOUT):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as ss:
                return True, ss.version() or "TLS ok"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def _icmp(host):
    if not _have("ping"):
        return "(ping not available)"
    rc, out = _run(f"ping -c 4 -W 2 {host}", timeout=15)
    for line in out.splitlines():
        if "packet loss" in line or "packets transmitted" in line:
            return line.strip()
    return out.strip().splitlines()[-1] if out.strip() else "(no output)"


def _trace(host):
    for tool in ("mtr -rwc 3", "traceroute -m 20 -w 2", "tracepath"):
        name = tool.split()[0]
        if _have(name):
            rc, out = _run(f"{tool} {host}", timeout=40)
            return f"$ {tool} {host}\n{out.strip()}"
    return "(no traceroute/mtr/tracepath available)"


def cmd_probe(host, ports, do_trace=False):
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:  # noqa: BLE001
        print(f"❌ DNS/resolve failed for {host!r}: {e}")
        return 1
    print(f"🎯 Probing {host} ({ip})\n")
    print("  ICMP ping : " + _icmp(host))
    ok_ports, syn_only = [], []
    print("\n  TCP ports (full token round-trip = real end-to-end path):")
    for p in ports:
        r = _probe_tcp(host, p)
        if r["echo"]:
            mark = "✅ echo"
            ok_ports.append(p)
        elif r["tcp"]:
            mark = "🟡 tcp-only"
            syn_only.append(p)
        else:
            mark = "❌"
        rtt = f"{r['rtt_ms']}ms" if r["rtt_ms"] is not None else "-"
        print(f"    {mark:<12} port {p:<6} {rtt:<8} {r['detail'][:80]}")
    tls_ok, tls_info = _probe_tls(host, 443)
    print(f"\n  TLS:443   : {'✅' if tls_ok else '❌'} {tls_info}")
    if do_trace:
        print("\n  path:\n" + _trace(host))

    print("\n" + "=" * 60)
    print("VERDICT (this direction):")
    if ok_ports:
        print(f"  ✅ REACHABLE end-to-end on: {ok_ports}")
        print("     This direction works — initiate the connection THIS way.")
    elif syn_only:
        print(f"  🟡 TCP opened but NO token echo on: {syn_only}")
        print("     A middlebox/proxy likely answered the SYN. NOT a usable")
        print("     path to the real server. Treat as blocked.")
    else:
        print("  ❌ No port reachable end-to-end in this direction.")
        print("     Build the tunnel in the OPPOSITE direction instead.")
    print("=" * 60)
    # machine-readable line for pairing both sides
    print("RESULT " + json.dumps({"target": host, "ip": ip,
                                   "echo_ports": ok_ports,
                                   "syn_only": syn_only,
                                   "tls443": tls_ok}))
    return 0


# --------------------------------------------------------------------------- #
# INFO / EGRESS
# --------------------------------------------------------------------------- #
def _public_ip():
    # try a few stdlib-only methods; all best-effort, short timeout.
    for host, port, path in (("api.ipify.org", 80, "/"),
                             ("ifconfig.me", 80, "/ip"),
                             ("icanhazip.com", 80, "/")):
        try:
            import http.client
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.request("GET", path, headers={"User-Agent": "curl/8"})
            r = c.getresponse()
            body = r.read().decode(errors="replace").strip()
            c.close()
            if body:
                return f"{body} (via {host})"
        except Exception:  # noqa: BLE001
            continue
    return "(could not determine — outbound HTTP may be blocked)"


def cmd_info():
    print("🖥  LOCAL INFO\n")
    print("  hostname : " + socket.gethostname())
    try:
        print("  local IPs: " + ", ".join(sorted(set(
            ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None)))))
    except Exception as e:  # noqa: BLE001
        print(f"  local IPs: (lookup failed: {e})")
    print("  egress IP: " + _public_ip())
    for label, cmd in (("interfaces", "ip -brief addr 2>/dev/null || ifconfig -a"),
                       ("route    ", "ip route 2>/dev/null || route -n"),
                       ("listening", "ss -tlnp 2>/dev/null || netstat -tlnp"),
                       ("mtu      ", "ip link 2>/dev/null | grep -i mtu")):
        rc, out = _run(cmd)
        out = out.strip()
        print(f"\n  {label}:\n    " + "\n    ".join(out.splitlines()[:25]))
    return 0


def cmd_egress():
    print("🌍 OUTBOUND REACHABILITY (can THIS box reach the internet at all?)\n")
    targets = [("1.1.1.1", 443, "Cloudflare"), ("8.8.8.8", 443, "Google DNS"),
               ("9.9.9.9", 443, "Quad9"), ("1.1.1.1", 53, "DNS/53"),
               ("github.com", 443, "GitHub")]
    any_ok = False
    for host, port, name in targets:
        t0 = time.time()
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.close()
            dt = round((time.time() - t0) * 1000, 1)
            print(f"  ✅ {name:<12} {host}:{port}  {dt}ms")
            any_ok = True
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {name:<12} {host}:{port}  {type(e).__name__}")
    print("\n  egress IP: " + _public_ip())
    print("\n  " + ("✅ Outbound internet works from here."
                    if any_ok else
                    "❌ No outbound reached — this box may be fully firewalled."))
    return 0


# --------------------------------------------------------------------------- #
# selftest (offline)
# --------------------------------------------------------------------------- #
def cmd_selftest():
    passed = failed = 0

    def ok(c, m):
        nonlocal passed, failed
        if c:
            passed += 1
        else:
            failed += 1
            print(f"  ✗ {m}")

    # loopback listener + probe on an ephemeral high port -> full echo path
    port = 53535
    res = {}
    threading.Thread(target=_serve_one, args=(port, res), daemon=True).start()
    time.sleep(0.4)
    ok(res.get(port) == "listening", "listener bound on loopback")
    r = _probe_tcp("127.0.0.1", port, timeout=3)
    ok(r["tcp"], "probe TCP connect ok")
    ok(r["echo"], "probe token round-trip ok")
    ok(r["rtt_ms"] is not None, "probe measured an rtt")

    # a closed port -> connect fails cleanly
    r2 = _probe_tcp("127.0.0.1", 59999, timeout=1)
    ok(not r2["tcp"], "closed port reported unreachable")

    # helpers never raise
    ok(isinstance(_have("sh"), bool), "_have returns bool")
    ok(isinstance(_run("echo hi")[1], str), "_run returns text")

    print(f"\n  ==== {passed} passed, {failed} failed ====\n")
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description="master↔worker connectivity probe")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--listen", action="store_true", help="run echo listeners")
    g.add_argument("--probe", metavar="HOST", help="probe the other server")
    g.add_argument("--info", action="store_true", help="local network facts")
    g.add_argument("--egress", action="store_true", help="outbound reachability")
    g.add_argument("--selftest", action="store_true", help="offline self-checks")
    ap.add_argument("--ports", nargs="+", type=int, default=DEFAULT_PORTS)
    ap.add_argument("--trace", action="store_true", help="also run traceroute")
    a = ap.parse_args()

    if a.listen:
        return cmd_listen(a.ports)
    if a.probe:
        return cmd_probe(a.probe, a.ports, do_trace=a.trace)
    if a.info:
        return cmd_info()
    if a.egress:
        return cmd_egress()
    if a.selftest:
        return cmd_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
