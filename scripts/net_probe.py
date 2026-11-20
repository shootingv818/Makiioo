#!/usr/bin/env python3
"""
🌐 net_probe — heavy master↔worker link diagnostic (Iran ↔ abroad).
====================================================================

GOAL: find the most STABLE and FASTEST way to connect a server abroad (master)
to a server in Iran (worker), when the abroad→Iran direction is filtered.

This one file, run on BOTH servers, maps EVERY candidate method and port in
BOTH directions and then RECOMMENDS the best transport (reverse SSH tunnel for
simplicity/stability, or WireGuard/UDP for raw speed), ranked by real measured
latency + jitter + success ratio — not just "the port opened".

Pure standard library. No pip, no file to copy to the peer beyond THIS file.
Python 3.6+. Shell tools (ping/mtr/traceroute/ss/ip) are used only when present.

HOW TO USE (two servers, A and B):

  Step 1 — on server A (e.g. the master, which already runs sshd):
      python3 net_probe.py --listen
    leave it running. It opens TCP+UDP echo servers on every candidate port it
    can bind (ports already used by real services are skipped but still
    reachable — the probe will hit the real service).

  Step 2 — on server B (e.g. the Iran box):
      python3 net_probe.py --probe <A_PUBLIC_IP>
    it tests TCP token-echo, TLS, HTTP, UDP echo, samples latency/jitter, runs
    a traceroute, and prints a ranked verdict for the B→A direction.

  Step 3 — swap roles (B --listen, A --probe <B_IP>) to map the other way.
    (abroad→Iran is usually fully blocked; that result is itself the finding.)

Other modes:
  --egress     outbound reachability to public hosts on many ports (discovers
               which egress ports the local firewall allows, with no peer).
  --info       local facts: interfaces, egress IP, open ports, route, MTU.
  --verdict A.json B.json   merge two saved RESULT lines -> overall method pick.
  --selftest   offline self-checks (loopback TCP+UDP echo, ranking, parsers).

Save a probe's machine line for later merge:
  python3 net_probe.py --probe <IP> --json > b_to_a.json
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import subprocess
import sys
import threading
import time

# Candidate ports grouped by the method they usually carry. Comprehensive but
# bounded so a full sweep stays fast.
PORTS = {
    "ssh":   [22, 2222, 22022, 2022],
    "https": [443, 8443, 2053, 2083, 2087, 2096, 9443],
    "http":  [80, 8080, 8880, 2052, 2082, 2086, 2095],
    "dns":   [53],
    "mail":  [25, 465, 587, 993, 995],
    "rdp":   [3389],
    "vpn":   [1194, 1701, 500, 4500],          # openvpn/l2tp/ipsec (some UDP)
    "misc":  [3306, 5432, 6379, 9000, 10000, 8088],
}
UDP_PORTS = [51820, 1194, 500, 4500, 53, 8472]  # wireguard/openvpn/ipsec/dns/vxlan
ALL_TCP = sorted({p for v in PORTS.values() for p in v})

TOKEN = "NETPROBE-OK"
BANNER = b"NETPROBE listener ready\n"
CONNECT_TIMEOUT = 6.0
SAMPLES = 5                       # latency samples per confirmed TCP port


# --------------------------------------------------------------------------- #
# shell helpers (best-effort, never raise)
# --------------------------------------------------------------------------- #
def _run(cmd, timeout=15):
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
    return _run(f"command -v {cmd}", 5)[0] == 0


# --------------------------------------------------------------------------- #
# LISTENER (TCP + UDP echo on every bindable candidate port)
# --------------------------------------------------------------------------- #
def _tcp_server(port, results):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.listen(32)
        results[("tcp", port)] = "listening"
    except Exception as e:  # noqa: BLE001
        results[("tcp", port)] = f"skip ({type(e).__name__})"
        return
    while True:
        try:
            conn, addr = s.accept()
        except Exception:  # noqa: BLE001
            return
        threading.Thread(target=_tcp_handle, args=(conn, addr, port),
                         daemon=True).start()


def _tcp_handle(conn, addr, port):
    try:
        conn.settimeout(10)
        conn.sendall(BANNER)
        data = conn.recv(256)
        tok = (data or b"").strip().decode(errors="replace")
        conn.sendall(f"{TOKEN} tcp port={port} from={addr[0]} echo={tok}\n".encode())
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _udp_server(port, results):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        results[("udp", port)] = "listening"
    except Exception as e:  # noqa: BLE001
        results[("udp", port)] = f"skip ({type(e).__name__})"
        return
    while True:
        try:
            data, addr = s.recvfrom(512)
            s.sendto(f"{TOKEN} udp port={port}\n".encode(), addr)
        except Exception:  # noqa: BLE001
            return


def cmd_listen(tcp_ports, udp_ports):
    results = {}
    for p in tcp_ports:
        threading.Thread(target=_tcp_server, args=(p, results), daemon=True).start()
    for p in udp_ports:
        threading.Thread(target=_udp_server, args=(p, results), daemon=True).start()
    time.sleep(0.6)
    bound_tcp = [p for p in tcp_ports if results.get(("tcp", p)) == "listening"]
    bound_udp = [p for p in udp_ports if results.get(("udp", p)) == "listening"]
    skipped = [p for p in tcp_ports if results.get(("tcp", p), "").startswith("skip")]
    print("🌐 LISTENER up — leave running while the other server probes.\n")
    print(f"  TCP listening : {bound_tcp}")
    print(f"  UDP listening : {bound_udp}")
    if skipped:
        print(f"  TCP in-use/skipped (real service already there, still "
              f"reachable): {skipped}")
    if not bound_tcp and not bound_udp:
        print("\n⚠ Could not bind any port (need root for <1024). Use --ports.")
        return 1
    print("\n  On the OTHER server run:")
    print(f"    python3 net_probe.py --probe <THIS_IP>")
    print("\n  (Ctrl-C to stop.)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


# --------------------------------------------------------------------------- #
# PROBER
# --------------------------------------------------------------------------- #
def _tcp_once(host, port, timeout=CONNECT_TIMEOUT):
    """One connect; return (connected, echoed, rtt_ms, detail)."""
    t0 = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return False, False, None, f"{type(e).__name__}"
    rtt = (time.time() - t0) * 1000
    echoed, detail = False, ""
    try:
        s.settimeout(timeout)
        try:
            s.recv(256)
        except Exception:  # noqa: BLE001
            pass
        s.sendall((TOKEN + "\n").encode())
        back = s.recv(256).decode(errors="replace")
        if TOKEN in back:
            echoed, detail = True, back.strip()[:80]
        else:
            detail = f"no-echo ({back.strip()[:40]!r})"
    except Exception as e:  # noqa: BLE001
        detail = f"no-echo ({type(e).__name__})"
    finally:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    return True, echoed, round(rtt, 1), detail


def _sample_latency(host, port, n=SAMPLES):
    """Return (success_ratio, min, avg, max, jitter_ms) over n connects."""
    rtts, okc = [], 0
    for _ in range(n):
        connected, _echo, rtt, _d = _tcp_once(host, port, timeout=4)
        if connected and rtt is not None:
            okc += 1
            rtts.append(rtt)
        time.sleep(0.05)
    if not rtts:
        return 0.0, None, None, None, None
    rtts.sort()
    avg = sum(rtts) / len(rtts)
    jitter = (sum(abs(r - avg) for r in rtts) / len(rtts))
    return okc / n, round(rtts[0], 1), round(avg, 1), round(rtts[-1], 1), round(jitter, 1)


def _udp_once(host, port, timeout=4):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto((TOKEN + "\n").encode(), (host, port))
        data, _ = s.recvfrom(256)
        s.close()
        return TOKEN in data.decode(errors="replace")
    except Exception:  # noqa: BLE001
        return False


def _tls(host, port=443, timeout=CONNECT_TIMEOUT):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as ss:
                return True, ss.version() or "TLS"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _http(host, port=80, timeout=CONNECT_TIMEOUT):
    try:
        import http.client
        c = http.client.HTTPConnection(host, port, timeout=timeout)
        c.request("GET", "/", headers={"User-Agent": "netprobe"})
        r = c.getresponse()
        c.close()
        return True, f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


def _icmp(host):
    if not _have("ping"):
        return "(no ping)"
    rc, out = _run(f"ping -c 5 -W 2 {host}", 20)
    for line in out.splitlines():
        if "packet loss" in line:
            return line.strip()
    return (out.strip().splitlines() or ["(no output)"])[-1]


def _trace(host):
    for tool in ("mtr -rwzc 3", "traceroute -m 20 -w 2", "tracepath"):
        if _have(tool.split()[0]):
            return f"$ {tool} {host}\n" + _run(f"{tool} {host}", 45)[1].strip()
    return "(no mtr/traceroute/tracepath)"


def _rank(confirmed):
    """confirmed: list of dicts with port, avg, jitter, success. Rank by a
    stability+speed score (lower is better): avg + 2*jitter, penalise loss."""
    def score(c):
        avg = c["avg"] if c["avg"] is not None else 9e6
        jit = c["jitter"] if c["jitter"] is not None else 9e6
        loss = (1 - c["success"]) * 1000
        return avg + 2 * jit + loss
    return sorted(confirmed, key=score)


def cmd_probe(host, tcp_ports, udp_ports, do_trace, as_json):
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:  # noqa: BLE001
        print(f"❌ resolve failed for {host!r}: {e}")
        return 1
    if not as_json:
        print(f"🎯 Probing {host} ({ip})\n")
        print("  ICMP      : " + _icmp(host))

    confirmed, syn_only, closed = [], [], []
    if not as_json:
        print("\n  TCP (token echo = real end-to-end path; else middlebox):")
    for p in tcp_ports:
        connected, echoed, rtt, detail = _tcp_once(host, p)
        if echoed:
            sr, mn, avg, mx, jit = _sample_latency(host, p)
            c = {"port": p, "success": sr, "min": mn, "avg": avg, "max": mx,
                 "jitter": jit}
            confirmed.append(c)
            if not as_json:
                print(f"    ✅ echo   {p:<6} rtt~{avg}ms jitter {jit}ms "
                      f"loss {int((1-sr)*100)}%")
        elif connected:
            syn_only.append(p)
            if not as_json:
                print(f"    🟡 synonly {p:<6} {detail}")
        else:
            closed.append(p)
    tls_ok, tls_v = _tls(host, 443)
    http_ok, http_v = _http(host, 80)
    udp_open = [p for p in udp_ports if _udp_once(host, p)]
    if not as_json:
        print(f"\n  TLS:443   : {'✅ ' + str(tls_v) if tls_ok else '❌ ' + str(tls_v)}")
        print(f"  HTTP:80   : {'✅ ' + str(http_v) if http_ok else '❌ ' + str(http_v)}")
        print(f"  UDP echo  : {udp_open or 'none'}")
        if do_trace:
            print("\n  path:\n" + _trace(host))

    ranked = _rank(confirmed)
    result = {"target": host, "ip": ip,
              "tcp_echo": [c["port"] for c in confirmed],
              "tcp_synonly": syn_only, "udp_open": udp_open,
              "tls443": tls_ok, "http80": http_ok,
              "best": ranked[0] if ranked else None,
              "ranked": ranked}
    if as_json:
        print("RESULT " + json.dumps(result))
        return 0

    print("\n" + "=" * 62)
    print("VERDICT — this direction")
    if ranked:
        b = ranked[0]
        print(f"  ✅ Best TCP port  : {b['port']}  (~{b['avg']}ms, jitter "
              f"{b['jitter']}ms, loss {int((1-b['success'])*100)}%)")
        print("  Top candidates    : " +
              ", ".join(f"{c['port']}(~{c['avg']}ms)" for c in ranked[:4]))
    else:
        print("  ❌ No TCP port reachable end-to-end this direction.")
    if syn_only:
        print(f"  🟡 SYN-only (fake, middlebox): {syn_only} — treat as blocked.")
    if udp_open:
        print(f"  ✅ UDP open        : {udp_open} (good for WireGuard)")
    print("-" * 62)
    _recommend(result)
    print("=" * 62)
    print("RESULT " + json.dumps(result))
    return 0


def _recommend(result):
    ssh_ports = set(PORTS["ssh"])
    echo = result.get("tcp_echo", [])
    udp = result.get("udp_open", [])
    best = result.get("best")
    print("RECOMMENDED METHOD (connect abroad→Iran via an Iran→abroad tunnel):")
    if not echo and not udp:
        print("  ✗ This direction is fully blocked. Run the probe the OTHER way")
        print("    (Iran must reach the master) — the tunnel is built on the")
        print("    direction that shows ✅ echo.")
        return
    # WireGuard if any UDP open -> fastest.
    if udp:
        print(f"  ① FASTEST  : WireGuard over UDP {udp[0]} — lowest overhead,")
        print("              best throughput and roaming. Preferred if stable.")
    ssh_open = [p for p in echo if p in ssh_ports]
    tun_port = (ssh_open[0] if ssh_open else (best["port"] if best else echo[0]))
    print(f"  ② STABLE   : reverse SSH tunnel on TCP {tun_port}"
          + ("" if ssh_open else "  (run sshd on this port at the master)"))
    print(f"              initiated FROM Iran: ssh -N -R ... -p {tun_port} "
          "root@MASTER")
    if result.get("tls443") and 443 in echo:
        print("  ③ STEALTH  : wrap the tunnel in TLS on 443 (hardest to filter).")


# --------------------------------------------------------------------------- #
# EGRESS / INFO / VERDICT-merge
# --------------------------------------------------------------------------- #
def _public_ip():
    for host, path in (("api.ipify.org", "/"), ("ifconfig.me", "/ip"),
                       ("icanhazip.com", "/")):
        try:
            import http.client
            c = http.client.HTTPConnection(host, 80, timeout=5)
            c.request("GET", path, headers={"User-Agent": "curl/8"})
            body = c.getresponse().read().decode(errors="replace").strip()
            c.close()
            if body:
                return f"{body} (via {host})"
        except Exception:  # noqa: BLE001
            continue
    return "(unknown — outbound HTTP may be blocked)"


def cmd_egress():
    print("🌍 OUTBOUND REACHABILITY (which egress ports does this box allow?)\n")
    # hit a well-known reachable host on many ports to learn the local firewall.
    host = "1.1.1.1"
    hosts = {80: "example.com", 443: "cloudflare.com", 53: "1.1.1.1"}
    any_ok = False
    for p in sorted(set(ALL_TCP + [53])):
        tgt = hosts.get(p, host)
        t0 = time.time()
        try:
            socket.create_connection((tgt, p), timeout=4).close()
            print(f"  ✅ egress {p:<6} ok  (~{round((time.time()-t0)*1000)}ms, {tgt})")
            any_ok = True
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ egress {p:<6} {type(e).__name__}")
    print("\n  egress IP : " + _public_ip())
    print("  " + ("✅ Outbound works on the ✅ ports above."
                  if any_ok else "❌ No outbound — box is heavily firewalled."))
    return 0


def cmd_info():
    print("🖥  LOCAL INFO\n")
    print("  hostname : " + socket.gethostname())
    print("  egress IP: " + _public_ip())
    for label, cmd in (("interfaces", "ip -brief addr 2>/dev/null || ifconfig -a"),
                       ("route    ", "ip route 2>/dev/null || route -n"),
                       ("listening", "ss -tulnp 2>/dev/null || netstat -tulnp"),
                       ("mtu      ", "ip link 2>/dev/null | grep -i mtu")):
        out = _run(cmd)[1].strip()
        print(f"\n  {label}:\n    " + "\n    ".join(out.splitlines()[:30]))
    return 0


def cmd_verdict(fa, fb):
    def _load(f):
        try:
            txt = open(f).read()
            i = txt.find("{")
            return json.loads(txt[i:]) if i >= 0 else {}
        except Exception as e:  # noqa: BLE001
            print(f"  could not read {f}: {e}")
            return {}
    a, b = _load(fa), _load(fb)
    print("🧭 OVERALL — combining both directions\n")
    print(f"  A→ ({a.get('target','?')}) echo: {a.get('tcp_echo')} "
          f"udp: {a.get('udp_open')}")
    print(f"  B→ ({b.get('target','?')}) echo: {b.get('tcp_echo')} "
          f"udp: {b.get('udp_open')}")
    # the usable direction is whichever shows echo.
    usable = b if b.get("tcp_echo") or b.get("udp_open") else a
    other = a if usable is b else b
    print()
    if not (usable.get("tcp_echo") or usable.get("udp_open")):
        print("  ❌ Neither direction reachable. Need a relay (a 3rd box both")
        print("     can reach) or a domestic CDN/ir-hosted entry.")
        return 0
    print(f"  ✅ Build the tunnel in the direction that reaches "
          f"{usable.get('target')} (initiator = the other box).")
    _recommend(usable)
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

    res = {}
    threading.Thread(target=_tcp_server, args=(53535, res), daemon=True).start()
    threading.Thread(target=_udp_server, args=(53535, res), daemon=True).start()
    time.sleep(0.4)
    ok(res.get(("tcp", 53535)) == "listening", "tcp listener bound")
    ok(res.get(("udp", 53535)) == "listening", "udp listener bound")

    connected, echoed, rtt, _ = _tcp_once("127.0.0.1", 53535, timeout=3)
    ok(connected and echoed, "tcp token echo round-trip")
    ok(rtt is not None, "tcp rtt measured")
    ok(_udp_once("127.0.0.1", 53535), "udp token echo round-trip")

    sr, mn, avg, mx, jit = _sample_latency("127.0.0.1", 53535, n=4)
    ok(sr == 1.0 and avg is not None and jit is not None, "latency sampling")

    c2, e2, _, _ = _tcp_once("127.0.0.1", 59999, timeout=1)
    ok(not c2, "closed port unreachable")
    ok(not _udp_once("127.0.0.1", 59999), "closed udp no echo")

    ranked = _rank([{"port": 443, "success": 1.0, "min": 10, "avg": 10, "max": 11,
                     "jitter": 1},
                    {"port": 22, "success": 1.0, "min": 50, "avg": 60, "max": 90,
                     "jitter": 15},
                    {"port": 80, "success": 0.4, "min": 10, "avg": 12, "max": 14,
                     "jitter": 1}])
    ok(ranked[0]["port"] == 443, "ranking prefers low latency+jitter")
    ok(ranked[-1]["port"] == 80, "ranking penalises packet loss")

    ok(isinstance(_have("sh"), bool), "_have returns bool")
    print(f"\n  ==== {passed} passed, {failed} failed ====\n")
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description="heavy master↔worker link probe")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--listen", action="store_true")
    g.add_argument("--probe", metavar="HOST")
    g.add_argument("--info", action="store_true")
    g.add_argument("--egress", action="store_true")
    g.add_argument("--verdict", nargs=2, metavar=("A.json", "B.json"))
    g.add_argument("--selftest", action="store_true")
    ap.add_argument("--ports", nargs="+", type=int, default=ALL_TCP)
    ap.add_argument("--udp", nargs="+", type=int, default=UDP_PORTS)
    ap.add_argument("--trace", action="store_true")
    ap.add_argument("--json", action="store_true", help="machine output only")
    a = ap.parse_args()

    if a.listen:
        return cmd_listen(a.ports, a.udp)
    if a.probe:
        return cmd_probe(a.probe, a.ports, a.udp, a.trace, a.json)
    if a.info:
        return cmd_info()
    if a.egress:
        return cmd_egress()
    if a.verdict:
        return cmd_verdict(*a.verdict)
    if a.selftest:
        return cmd_selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
