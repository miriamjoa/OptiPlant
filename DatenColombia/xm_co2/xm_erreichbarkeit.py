#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xm_erreichbarkeit.py
====================
Prueft systematisch, WELCHER XM-Host aus deinem Netz ueberhaupt antwortet
und mit welchem HTTP-Status. Aendert nichts, liest nur.

Warum:
  servapibi.xm.com.co  - TCP 443 scheiterte an allen bekannten IPs
  sinergox.xm.com.co   - antwortet (HTTP 405 auf POST /hourly)

405 heisst "Method Not Allowed": Der Server LEBT, akzeptiert aber POST auf
diesem Pfad nicht. Das ist ein Routing-, kein Netzproblem. Also lohnt es sich,
die anderen Pfade und Methoden durchzuprobieren.

Aufruf:
    python xm_erreichbarkeit.py
"""

import socket
import ssl
import sys

try:
    import requests
except ImportError:
    sys.exit("Fehlt: requests   ->   python -m pip install requests")


servapibi_IPS = [
    "190.90.250.249",
    "179.1.5.120",
    "191.97.49.119",
    "179.1.12.119",
]

HOSTS = [
    "sinergox.xm.com.co",
    "www.xm.com.co",
    "xm.com.co",
]

BODY = {"MetricId": "factorEmisionCO2e",
        "StartDate": "2025-06-15",
        "EndDate": "2025-06-15",
        "Entity": "Sistema"}


def rule(n=76, ch="-"):
    print(ch * n)


def tcp_test(ip, port=443, timeout=6):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception as exc:
        return "%s" % type(exc).__name__
    finally:
        s.close()


def http_probe(url, method="GET", body=None, timeout=20):
    """Gibt (status, server_header, erste 100 Zeichen) zurueck."""
    try:
        if method == "GET":
            r = requests.get(url, timeout=timeout, allow_redirects=False)
        else:
            r = requests.post(url, json=body, timeout=timeout,
                              allow_redirects=False)
        snippet = r.text[:100].replace("\n", " ").replace("\r", "")
        return r.status_code, r.headers.get("server", "-"), snippet
    except Exception as exc:
        return None, "-", "%s: %s" % (type(exc).__name__, str(exc)[:90])


def main():
    print()
    print("=" * 76)
    print("XM-ERREICHBARKEIT  -  welcher Host antwortet aus deinem Netz?")
    print("=" * 76)

    # --- 1) TCP auf alle bekannten servapibi-IPs ---
    print("\n[1] TCP Port 443 auf allen bekannten servapibi-IPs")
    rule()
    alive = []
    for ip in servapibi_IPS:
        res = tcp_test(ip)
        if res is True:
            alive.append(ip)
            print("  %-18s OFFEN" % ip)
        else:
            print("  %-18s geschlossen (%s)" % (ip, res))
    if not alive:
        print("\n  -> Keine der IPs nimmt Verbindungen an.")
        print("     Das spricht fuer Geo-Sperre oder veraltete Records.")

    # --- 2) HTTP auf den erreichbaren Hosts ---
    print("\n[2] HTTP-Antworten auf erreichbaren Hosts")
    rule()
    print("  %-26s %-6s %-6s %s" % ("Host / Pfad", "Meth.", "Status", "Server"))
    rule()
    results = {}
    for host in HOSTS:
        for path, method in (("/", "GET"), ("/hourly", "GET"), ("/hourly", "POST")):
            url = "https://%s%s" % (host, path)
            st, srv, snip = http_probe(url, method, BODY if method == "POST" else None)
            results[(host, path, method)] = (st, srv, snip)
            label = "%s%s" % (host, path)
            print("  %-26s %-6s %-6s %s" % (label, method,
                                            st if st else "---", srv))

    # --- 3) servapibi ueber Bypass, falls eine IP offen ist ---
    if alive:
        print("\n[3] servapibi.xm.com.co ueber offene IP")
        rule()
        from urllib3.util import connection as u3conn
        original = u3conn.create_connection
        target = alive[0]

        def patched(address, *args, **kwargs):
            h, port = address
            if h == "servapibi.xm.com.co":
                address = (target, port)
            return original(address, *args, **kwargs)

        u3conn.create_connection = patched
        st, srv, snip = http_probe("https://servapibi.xm.com.co/hourly",
                                   "POST", BODY)
        print("  POST via %s -> Status %s | Server %s" % (target, st, srv))
        if snip:
            print("  Antwort: %s" % snip)
        u3conn.create_connection = original

    # --- 4) Bewertung ---
    print("\n" + "=" * 76)
    print("BEWERTUNG")
    print("=" * 76)

    sg_post = results.get(("sinergox.xm.com.co", "/hourly", "POST"), (None,))[0]
    sg_get = results.get(("sinergox.xm.com.co", "/", "GET"), (None,))[0]

    if alive:
        print("\n  servapibi ist erreichbar. Weiter mit:")
        print("    python xm_co2_hourly.py test --start 2025-06-15 --ip %s" % alive[0])
    else:
        print("\n  servapibi: TCP 443 auf keiner IP. Aus deinem Netz nicht erreichbar.")

    if sg_post in (200, 201):
        print("\n  sinergox /hourly POST liefert %s - API dort nutzbar!" % sg_post)
        print("  Dann Host im Skript umstellen (sag Bescheid).")
    elif sg_post == 405:
        print("\n  sinergox /hourly POST = 405 Method Not Allowed.")
        print("  Der Server lebt, aber dieser Pfad akzeptiert kein POST.")
        print("  -> sinergox ist die Web-Oberflaeche, nicht die API.")
    elif sg_post == 404:
        print("\n  sinergox /hourly = 404. Pfad existiert dort nicht.")
    elif sg_post is None:
        print("\n  sinergox nicht erreichbar.")

    print("\n  Bitte die komplette Tabelle oben zurueckmelden.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
