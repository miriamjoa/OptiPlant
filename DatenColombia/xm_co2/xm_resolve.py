#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xm_resolve.py
=============
Loest servapibi.xm.com.co auf, obwohl die normale DNS-Aufloesung scheitert.

Hintergrund (verifiziert 04.11.2026):
  Die Zone xm.com.co wird von nuqui.internexa.net und gorgona.internexa.net
  bedient. Die A-Records fuer servapibi haben TTL 10 Sekunden und wechseln
  zwischen Rechenzentren. Waehrend dieser Wechsel antworten die Nameserver
  vielen Resolvern mit SERVFAIL - auch Cloudflare und Google.

  Die Records EXISTIEREN aber. Man muss nur direkt bei den autoritativen
  Nameservern fragen, statt einen Cache zu fragen.

Strategien in dieser Reihenfolge:
  1. Normaler System-Resolver
  2. DNS-over-HTTPS (Cloudflare, Google)
  3. Direkte UDP-Abfrage bei den autoritativen Nameservern
  4. Bekannte IPs als letzter Ausweg

Aufruf:
    python xm_resolve.py
    python xm_resolve.py --json
"""

import argparse
import json
import random
import socket
import struct
import sys

try:
    import requests
except ImportError:
    requests = None


DOMAIN = "servapibi.xm.com.co"
AUTHORITATIVE_NS = ["gorgona.internexa.net", "nuqui.internexa.net"]
DOH_ENDPOINTS = [
    ("Cloudflare", "https://cloudflare-dns.com/dns-query"),
    ("Google", "https://dns.google/resolve"),
]
# Letzter Ausweg. Aendern sich wegen TTL 10 s laufend.
FALLBACK_IPS = ["191.97.49.119", "179.1.12.119"]


# ---------------------------------------------------------------- DNS Pakete
def _build_query(name, qtype=1, timeout=8):
    tid = random.randint(0, 65535)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(struct.pack("B", len(p)) + p.encode()
                     for p in name.rstrip(".").split(".")) + b"\x00"
    return header + qname + struct.pack(">HH", qtype, 1), tid


def _skip_name(data, i):
    while True:
        if data[i] & 0xC0 == 0xC0:
            return i + 2
        if data[i] == 0:
            return i + 1
        i += data[i] + 1


def _parse_response(data, qtype=1):
    """Gibt (rcode, [records]) zurueck."""
    rcode = data[3] & 0x0F
    ancount = struct.unpack(">H", data[6:8])[0]
    i = _skip_name(data, 12) + 4          # Frage ueberspringen
    out = []
    for _ in range(ancount):
        i = _skip_name(data, i)
        rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        rdata = data[i:i + rdlen]
        i += rdlen
        if rtype == qtype == 1 and rdlen == 4:
            out.append(".".join(str(b) for b in rdata))
        elif rtype == 2:                   # NS
            out.append(("NS", rdata))
    return rcode, out


def query_udp(server_ip, name, qtype=1, timeout=8):
    packet, tid = _build_query(name, qtype)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(packet, (server_ip, 53))
        data, _ = s.recvfrom(4096)
        return _parse_response(data, qtype)
    finally:
        s.close()


# ---------------------------------------------------------------- Strategien
def strategy_system():
    try:
        infos = socket.getaddrinfo(DOMAIN, 443, socket.AF_INET)
        ips = sorted({i[4][0] for i in infos})
        return ips, "System-Resolver"
    except Exception as exc:
        return [], "System-Resolver: %s" % type(exc).__name__


def strategy_doh():
    if requests is None:
        return [], "DoH: requests fehlt"
    notes = []
    for name, url in DOH_ENDPOINTS:
        try:
            r = requests.get(url, params={"name": DOMAIN, "type": "A"},
                             headers={"accept": "application/dns-json"},
                             timeout=12)
            data = r.json()
            status = data.get("Status")
            ips = [a.get("data") for a in (data.get("Answer") or [])
                   if a.get("type") == 1]
            if ips:
                return ips, "DoH %s" % name
            notes.append("%s Status %s" % (name, status))
        except Exception as exc:
            notes.append("%s %s" % (name, type(exc).__name__))
    return [], "DoH: " + ", ".join(notes)


def strategy_authoritative():
    """Fragt die Nameserver von internexa direkt."""
    ns_ips = []
    # Nameserver-Adressen beschaffen: erst System, dann DoH
    for ns in AUTHORITATIVE_NS:
        try:
            ns_ips.append((ns, socket.gethostbyname(ns)))
            continue
        except Exception:
            pass
        if requests is not None:
            for _name, url in DOH_ENDPOINTS:
                try:
                    r = requests.get(url, params={"name": ns, "type": "A"},
                                     headers={"accept": "application/dns-json"},
                                     timeout=10)
                    ans = [a.get("data") for a in (r.json().get("Answer") or [])
                           if a.get("type") == 1]
                    if ans:
                        ns_ips.append((ns, ans[0]))
                        break
                except Exception:
                    continue

    if not ns_ips:
        return [], "autoritativ: Nameserver-Adressen nicht ermittelbar"

    collected = []
    tried = []
    for ns_name, ns_ip in ns_ips:
        try:
            rcode, records = query_udp(ns_ip, DOMAIN, qtype=1)
            ips = [r for r in records if isinstance(r, str)]
            tried.append("%s(%s) rcode=%d" % (ns_name, ns_ip, rcode))
            if ips:
                collected.extend(ips)
        except Exception as exc:
            tried.append("%s(%s) %s" % (ns_name, ns_ip, type(exc).__name__))

    if collected:
        return sorted(set(collected)), "autoritativ: " + "; ".join(tried)
    return [], "autoritativ: " + "; ".join(tried)


def resolve(verbose=True):
    """Liefert (ips, quelle)."""
    for fn in (strategy_system, strategy_doh, strategy_authoritative):
        ips, note = fn()
        if verbose:
            mark = "OK " if ips else "-- "
            print("  %s%-58s %s" % (mark, fn.__name__.replace("strategy_", ""),
                                    note))
        if ips:
            return ips, note
    if verbose:
        print("  --%-58s %s" % ("fallback", ", ".join(FALLBACK_IPS)))
    return list(FALLBACK_IPS), "Fallback-Liste (unbestaetigt)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="nur JSON ausgeben")
    args = ap.parse_args()

    if not args.json:
        print()
        print("Aufloesung von %s" % DOMAIN)
        print("-" * 74)

    ips, source = resolve(verbose=not args.json)

    if args.json:
        print(json.dumps({"domain": DOMAIN, "ips": ips, "source": source}))
    else:
        print("-" * 74)
        if ips:
            print("Ergebnis: %s" % ", ".join(ips))
            print("Quelle  : %s" % source)
            print("\nNutzung:")
            print("  python xm_co2_hourly.py test --start 2025-06-15 --ip %s" % ips[0])
        else:
            print("Keine IP ermittelt.")
        print()
    return 0 if ips else 1


if __name__ == "__main__":
    sys.exit(main())
