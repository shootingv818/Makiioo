#!/usr/bin/env python3
"""linktest — self-contained two-server link tester (Iran <-> abroad).

One file, stdlib only. Run the SAME `both` command on each server (each one
listens on every candidate port AND probes the peer), so the two servers test
each other at once:

    python3 linktest.py both <PEER_PUBLIC_IP>

Modes:
    both HOST   listen on all ports + probe HOST + stay up as a listener
    listen      only open the echo listeners (TCP+UDP)
    probe HOST  only probe the peer
    egress      which outbound ports this box is allowed to use
"""
import socket, ssl, sys, time, threading, subprocess

TCP = [22, 2222, 22022, 443, 8443, 2053, 2083, 2087, 2096,
       8080, 80, 53, 465, 587, 993, 3389, 8088, 9000, 10000]
UDP = [51820, 1194, 500, 4500, 53, 8472]
TOK = b"LINKTEST-OK"


# ---------------- listener (TCP + UDP echo) ----------------
def _tcp_srv(port, ok):
    try:
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port)); s.listen(32); ok.append(port)
    except Exception:
        return
    while True:
        try:
            c, a = s.accept()
        except Exception:
            return
        try:
            # send our token IMMEDIATELY (no waiting to recv first -> no
            # mutual-recv deadlock), so the prober confirms a real path fast.
            c.settimeout(8)
            c.sendall(TOK + (" tcp %d\n" % port).encode())
        except Exception:
            pass
        finally:
            try: c.close()
            except Exception: pass


def _udp_srv(port, ok):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port)); ok.append(port)
    except Exception:
        return
    while True:
        try:
            d, a = s.recvfrom(256)
            s.sendto(TOK + (" udp %d\n" % port).encode(), a)
        except Exception:
            return


def listen(block=True):
    tok, uok = [], []
    for p in TCP:
        threading.Thread(target=_tcp_srv, args=(p, tok), daemon=True).start()
    for p in UDP:
        threading.Thread(target=_udp_srv, args=(p, uok), daemon=True).start()
    time.sleep(0.6)
    skipped = [p for p in TCP if p not in tok]
    print("LISTENER  tcp=%s udp=%s" % (sorted(tok), sorted(uok)))
    if skipped:
        print("  (ports with a real service already, still reachable: %s)" % skipped)
    if block:
        print("  listening... (Ctrl-C to stop)")
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt:
            print("stopped.")


# ---------------- prober ----------------
def _tcp(host, port, timeout=4):
    """Return (connected, echoed, got_data, rtt_ms, firstbytes).
      echoed   = our listener's token came back (definitely reached our box)
      got_data = the peer sent SOMETHING (e.g. a real sshd/smtp banner) ->
                 still a usable reachable endpoint, just not our echo server
      neither  = connected but silent -> may be a middlebox faking the SYN."""
    t = time.time()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except Exception as e:
        return False, False, False, None, type(e).__name__
    rtt = (time.time() - t) * 1000
    echoed = got = False
    det = ""
    try:
        s.settimeout(3)
        b = b""
        try:
            b = s.recv(128)          # our listener sends a banner immediately
        except Exception:
            pass
        if not b:
            # no unprompted banner (e.g. HTTP waits for a request) -> poke it
            try:
                s.sendall(TOK + b"\n")
                b = s.recv(128)
            except Exception:
                pass
        echoed = TOK in b
        got = len(b) > 0
        det = b.decode("replace").strip()[:40]
    except Exception as e:
        det = type(e).__name__
    finally:
        try: s.close()
        except Exception: pass
    return True, echoed, got, round(rtt, 1), det


def _lat(host, port, n=4):
    xs = []
    for _ in range(n):
        c, e, g, rt, _d = _tcp(host, port, 4)
        if c and rt is not None: xs.append(rt)
        time.sleep(0.05)
    if not xs:
        return 0.0, None, None
    a = sum(xs) / len(xs)
    j = sum(abs(x - a) for x in xs) / len(xs)
    return len(xs) / n, round(a, 1), round(j, 1)


def _udp(host, port, timeout=4):
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout); s.sendto(TOK + b"\n", (host, port))
        d, _ = s.recvfrom(128); s.close()
        return TOK in d
    except Exception:
        return False


def _tls(host):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((host, 443), timeout=5) as r:
            with ctx.wrap_socket(r, server_hostname=host) as s:
                return s.version() or "ok"
    except Exception:
        return None


def _ping(host):
    try:
        p = subprocess.run("ping -c 4 -W 2 " + host, shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=18)
        for ln in p.stdout.decode("replace").splitlines():
            if "packet loss" in ln: return ln.strip()
    except Exception:
        pass
    return "(no ping)"


def probe(host):
    try:
        ip = socket.gethostbyname(host)
    except Exception as e:
        print("resolve failed:", e); return
    print("\n==== PROBE -> %s (%s) ====" % (host, ip))
    print("ICMP :", _ping(host))
    usable, silent = [], []        # usable = echo OR a real service answered
    for p in TCP:
        c, e, g, rt, det = _tcp(host, p)
        if not c:
            continue
        if e or g:
            sr, a, j = _lat(host, p)
            kind = "echo" if e else "svc"     # svc = a real service answered
            usable.append((p, a, j, sr, kind))
            print("  OK  %-5s %-6d ~%sms  jitter %sms  loss %d%%%s" %
                  (kind, p, a, j, int((1 - sr) * 100),
                   ("  [" + det + "]" if (g and not e and det) else "")))
        else:
            silent.append(p)
            print("  ??    %-6d connected, no data (maybe middlebox)" % p)
    u = [p for p in UDP if _udp(host, p)]
    v = _tls(host)
    print("  UDP open :", u or "none")
    print("  TLS:443  :", v or "no")
    usable.sort(key=lambda x: (x[1] or 9e9) + 2 * (x[2] or 9e9) + (1 - x[3]) * 1000)
    print("---- verdict (this direction) ----")
    if usable:
        b = usable[0]
        print("  BEST tcp  : %d  (~%sms, jitter %sms, loss %d%%, %s)" %
              (b[0], b[1], b[2], int((1 - b[3]) * 100), b[4]))
        print("  usable    : " + ", ".join("%d(~%sms,%s)" % (p, a, k)
                                            for p, a, j, s, k in usable[:6]))
    else:
        print("  no TCP port reachable end-to-end this direction")
    if silent:
        print("  no-data ports (treat as blocked/middlebox): %s" % silent)
    if u:
        print("  UDP %d open -> WireGuard works (fastest option)" % u[0])
    print("  RESULT " + repr({"to": host,
                              "usable": [p for p, a, j, s, k in usable],
                              "silent": silent, "udp": u, "tls443": bool(v),
                              "best": usable[0][0] if usable else None}))


def egress():
    print("EGRESS (allowed outbound ports from this box):")
    for p in TCP:
        tgt = "cloudflare.com" if p in (443, 8443) else "example.com" if p in (80,) else "1.1.1.1"
        try:
            socket.create_connection((tgt, p), timeout=4).close()
            print("  OK   %-6d (%s)" % (p, tgt))
        except Exception as e:
            print("  no   %-6d %s" % (p, type(e).__name__))


def main():
    m = sys.argv[1] if len(sys.argv) > 1 else ""
    if m == "listen":
        listen(block=True)
    elif m == "probe" and len(sys.argv) > 2:
        probe(sys.argv[2])
    elif m == "both" and len(sys.argv) > 2:
        listen(block=False)        # start our listeners, don't block
        peer = sys.argv[2]
        # probe the peer a few times: it may only start listening AFTER we do,
        # so a couple of rounds makes the run-order not matter.
        for i in range(3):
            probe(peer)
            if i < 2:
                print("\n(re-probing in 15s so order doesn't matter...)\n")
                time.sleep(15)
        print("\nStaying up as a listener so the PEER can probe back. Ctrl-C to stop.")
        try:
            while True: time.sleep(3600)
        except KeyboardInterrupt:
            print("stopped.")
    elif m == "egress":
        egress()
    else:
        print("usage: python3 linktest.py both <PEER_IP> | listen | probe <IP> | egress")


if __name__ == "__main__":
    main()
