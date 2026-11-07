#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xm_diagnose.py
==============
Klaert in einem Lauf, WARUM servapibi.xm.com.co nicht erreichbar ist.

Ergebnisstand 04.11.2026 (bereits verifiziert):
  Die DNS-Zone xm.com.co hat TTL 10 s und liefert vielen Resolvern SERVFAIL.
  Die A-Records EXISTIEREN aber:
      190.90.250.249
      179.1.5.120
  Es ist kein Firmenfilter. Ein fester IP-Bypass funktioniert.

Dieses Skript bestaetigt den Befund auf deinem Rechner und prueft zusaetzlich,
ob die IPs noch aktuell sind und ob HTTPS durchgeht.

Aufruf:
    python xm_diagnose.py
    python xm_diagnose.py --ip 190.90.250.249

Das Skript veraendert nichts.
"""

import argparse
import os
import socket
import sys

try:
    import requests
except ImportError:
    sys.exit("Fehlt: requests   ->   python -m pip install requests")


HOSTS = [
    "servapibi.xm.com.co",
    "xm.com.co",
    "www.xm.com.co",
    "sinergox.xm.com.co",
]

KNOWN_IPS = ["190.90.250.249", "179.1.5.120"]

DOH = [
    ("Cloudflare", "https://cloudflare-dns.com/dns-query"),
    ("Google",     "https://dns.google/resolve"),
]

STATUS_TEXT = {
    0: "NOERROR (aufgeloest)",
    1: "FORMERR",
    2: "SERVFAIL (Server verweigert Antwort)",
    3: "NXDOMAIN (existiert nicht)",
    5: "REFUSED",
}


def rule(n=74, ch="-"):
    print(ch * n)


def test_system_dns(host):
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET)
        return True, sorted({i[4][0] for i in infos})
    except socket.gaierror as exc:
        return False, "gaierror %s" % (exc.errno,)
    except Exception as exc:
        return False, "%s" % type(exc).__name__


def test_doh(url, host):
    try:
        r = requests.get(url, params={"name": host, "type": "A"},
                         headers={"accept": "application/dns-json"}, timeout=15)
        if r.status_code != 200:
            return None, "HTTP %s" % r.status_code
        data = r.json()
        status = data.get("Status")
        ips = [a.get("data") for a in (data.get("Answer") or [])
               if a.get("type") == 1]
        return status, ips
    except Exception as exc:
        return None, "%s" % type(exc).__name__


def install_ip_bypass(ip, host="servapibi.xm.com.co"):
    """Verbindungen zu host gehen an ip, SNI und Host-Header bleiben."""
    try:
        from urllib3.util import connection as u3conn
    except ImportError:
        return False
    original = u3conn.create_connection

    def patched(address, *args, **kwargs):
        h, port = address
        if h == host:
            address = (ip, port)
        return original(address, *args, **kwargs)

    u3conn.create_connection = patched
    return True


def post_hourly(ip=None):
    body = {"MetricId": "factorEmisionCO2e",
            "StartDate": "2025-06-15",
            "EndDate": "2025-06-15",
            "Entity": "Sistema"}
    if ip:
        install_ip_bypass(ip)
    try:
        r = requests.post("https://servapibi.xm.com.co/hourly",
                          json=body, timeout=25)
        return True, "HTTP %s, %d Bytes" % (r.status_code, len(r.content))
    except Exception as exc:
        msg = str(exc)
        if "NameResolution" in msg or "getaddrinfo" in msg or "Failed to resolve" in msg:
            return False, "DNS-Fehler (Bypass inaktiv)"
        if "ConnectTimeout" in msg or "timed out" in msg:
            return False, "Zeitueberschreitung (IP nicht erreichbar)"
        if "SSLError" in msg or "certificate" in msg.lower():
            return False, "TLS-Fehler: %s" % msg[:120]
        return False, "%s: %s" % (type(exc).__name__, msg[:120])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=None, help="IP fuer den POST-Test")
    args = ap.parse_args()

    print()
    print("=" * 74)
    print("XM-DIAGNOSE  -  servapibi.xm.com.co")
    print("=" * 74)

    # 0 Proxy
    print("\n[0] Proxy")
    rule()
    pv = {k: v for k, v in os.environ.items() if "proxy" in k.lower() and v}
    if pv:
        for k, v in pv.items():
            print("  %-22s %s" % (k, v))
    else:
        print("  keine Proxy-Variablen gesetzt")

    # 1 System-DNS
    print("\n[1] Windows-DNS")
    rule()
    sys_ok = {}
    for h in HOSTS:
        ok, res = test_system_dns(h)
        sys_ok[h] = ok
        print("  %-26s %s" % (h, ("OK  " + str(res)) if ok else ("FEHLER  " + str(res))))

    # 2 Public Resolver
    print("\n[2] Oeffentliche Resolver (DoH)")
    rule()
    public_ips = []
    for name, url in DOH:
        status, ips = test_doh(url, "servapibi.xm.com.co")
        if status is None:
            print("  %-12s nicht erreichbar (%s)" % (name, ips))
            continue
        print("  %-12s Status %s = %s" % (name, status,
              STATUS_TEXT.get(status, "unbekannt")))
        if ips:
            print("               %s" % ", ".join(str(i) for i in ips))
            public_ips = ips

    # 3 IPs direkt
    print("\n[3] Bekannte IPs (Stand 04.11.2026)")
    rule()
    for ip in KNOWN_IPS:
        print("  %-18s im Bypass hinterlegt" % ip)

    # 4 HTTPS
    print("\n[4] HTTPS-POST auf /hourly")
    rule()
    ok_g, msg = ("", "")
    try:
        r = requests.get("https://www.google.com", timeout=12)
        ok_g, msg = True, "HTTP %s" % r.status_code
    except Exception as exc:
        ok_g, msg = False, type(exc).__name__
    print("  %-30s %s" % ("www.google.com (Kontrolle)", msg))

    ip_to_use = args.ip or (public_ips[0] if public_ips else KNOWN_IPS[0])
    ok_x, msg = post_hourly(ip_to_use)
    print("  %-30s %s" % ("POST via %s" % ip_to_use, msg))

    # 5 Bewertung
    print("\n" + "=" * 74)
    print("BEWERTUNG")
    print("=" * 74)

    if ok_x:
        print("\n  ERFOLG: Die API antwortet.")
        print("  Weiter mit:")
        print("    python xm_co2_hourly.py year 2025 --ip %s" % ip_to_use)
        print("    python xm_co2_hourly.py patch 2025 --profiles <Datei>.xlsx --ip %s" % ip_to_use)
        print()
        return 0

    if not sys_ok.get("servapibi.xm.com.co"):
        print("\n  Befund: Windows-DNS loest servapibi nicht auf.")
        if public_ips:
            print("  Oeffentliche Resolver liefern dagegen: %s" % ", ".join(public_ips))
            print("  -> Die Records existieren. Der DNS ist das Problem, nicht die Domain.")
        else:
            print("  Oeffentliche Resolver liefern ebenfalls SERVFAIL.")
            print("  -> Die Zone antwortet unzuverlaessig, die Records existieren aber")
            print("     (bestaetigt 04.11.2026: 190.90.250.249, 179.1.5.120).")
        print("\n  Naechster Schritt:")
        print("    1) IP-Bypass im Abrufskript verwenden (Parameter --ip)")
        print("    2) Falls Zeitueberschreitung: andere IP probieren")
        for ip in KNOWN_IPS:
            print("         --ip %s" % ip)
        print("    3) Falls auch das scheitert: IPs sind veraltet. Aktuelle holen:")
        print("         https://viewdns.info/dnsrecord/?domain=servapibi.xm.com.co")
    else:
        print("\n  Windows-DNS loest servapibi auf, aber der POST scheitert.")
        print("  -> Port 443 blockiert oder Proxy verlangt Anmeldung.")

    if not ok_g:
        print("\n  HINWEIS: Auch www.google.com ist nicht erreichbar.")
        print("  Dann liegt ein allgemeines Netzproblem vor.")

    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
