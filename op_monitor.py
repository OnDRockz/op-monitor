#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One Piece TCG – Preorder- & Release-Monitor
===========================================
Prueft eine Liste von Shops auf NEUE Produkte (z. B. neue Vorbestellungen)
und neue News-/Release-Posts und schickt dir eine Benachrichtigung
(Discord und/oder Telegram).

Eigenschaften:
- Laeuft EINMAL pro Aufruf  -> gedacht fuer GitHub Actions oder Cron (alle 15-30 Min).
- Merkt sich bekannte Produkte/Posts in seen.json (kein Spam bei jedem Lauf).
- Card Collector (Alzey) hat Prioritaet: wird zuerst geprueft und im Alarm mit  markiert.
- Keine externen Pakete noetig  -> nur Python-Standardbibliothek.
- Probiert automatisch den Shopify-Feed (/products.json). Shops ohne Shopify
  werden einmal als "kein Feed" gemeldet -> die dann per changedetection.io beobachten.

Konfiguration ueber Umgebungsvariablen (mindestens EIN Kanal):
  DISCORD_WEBHOOK_URL   Discord Webhook-URL
  TELEGRAM_BOT_TOKEN    Telegram Bot-Token
  TELEGRAM_CHAT_ID      Telegram Chat-ID
"""

import json
import os
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET

# ==========================================================================
#  SHOP-LISTE  – hier kannst du jederzeit Shops ergaenzen (Zeile kopieren).
#  "priority": True  -> zuerst pruefen + im Alarm markieren.
# ==========================================================================
SHOPS = [
    {"name": "Card Collector (Alzey)", "url": "https://card-collector.net", "priority": True},

    # --- Deutschland ---
    {"name": "Card-Corner",         "url": "https://www.card-corner.de",       "priority": False},
    {"name": "Feenturm",            "url": "https://feenturm.de",              "priority": False},
    {"name": "GeeksHeaven",         "url": "https://geeksheaven.de",           "priority": False},
    {"name": "Crispy Cards",        "url": "https://crispycards.de",           "priority": False},
    {"name": "Zeno Cards",          "url": "https://zenocards.com",            "priority": False},
    {"name": "Games Island",        "url": "https://games-island.eu",          "priority": False},
    {"name": "Gate to the Games",   "url": "https://www.gate-to-the-games.de",  "priority": False},
    {"name": "Sapphire-Cards",      "url": "https://sapphire-cards.de",        "priority": False},
    {"name": "DunPop",              "url": "https://dunpop.de",                "priority": False},
    {"name": "Cardlantis (nur Info)","url": "https://cardlantis.de",           "priority": False},

    # --- Schweiz (Versand nach Basel, kein Zoll) ---
    {"name": "The Uncommon Shop (CH)", "url": "https://theuncommonshop.ch",    "priority": False},
    {"name": "Pikaversum (CH)",     "url": "https://pikaversum.ch",            "priority": False},
    {"name": "tcg-paradies (CH)",   "url": "https://tcg-paradies.ch",          "priority": False},
    {"name": "CardCollectors (CH)", "url": "https://cardcollectors.ch",        "priority": False},
    {"name": "Good Games Bern (CH)","url": "https://www.goodgamesbern.ch",     "priority": False},
]

# Nur Produkte melden, deren Titel eines dieser Woerter enthaelt (klein geschrieben).
# Leere Liste []  = ALLES melden. Fuer nur One Piece so lassen:
KEYWORDS = ["one piece"]

# Optional zusaetzlich nach Set-Codes filtern? Dann z. B.:
# KEYWORDS = ["one piece", "op-", "eb-", "prb", "st-", "dp-"]

STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
USER_AGENT = "Mozilla/5.0 (compatible; OP-Monitor/1.0)"
TIMEOUT = 25          # Sekunden pro Anfrage
MAX_PAGES = 20        # max. Feed-Seiten pro Shop (250 Produkte/Seite)
ATOM_NS = "{http://www.w3.org/2005/Atom}"


# ------------------------------------------------------------------ HTTP ---
def http_get(url):
    """Gibt (status_code, body_bytes) zurueck oder (None, None) bei Fehler."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.getcode(), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return None, None


# --------------------------------------------------------------- Shopify ---
def fetch_shopify_products(base_url):
    """
    Liest den Shopify-Feed /products.json (paginiert).
    Rueckgabe: Liste von Produkt-Dicts, oder None wenn der Shop kein Shopify ist.
    """
    products = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{base_url}/products.json?limit=250&page={page}"
        status, body = http_get(url)
        if status != 200 or not body:
            # Seite 1 nicht ladbar -> kein Shopify-Feed vorhanden
            return None if page == 1 else products
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return None if page == 1 else products
        if "products" not in data:
            return None if page == 1 else products
        batch = data["products"]
        if not batch:
            break
        products.extend(batch)
        time.sleep(0.4)  # hoeflich bleiben
    return products


def product_matches(title):
    if not KEYWORDS:
        return True
    t = (title or "").lower()
    return any(k in t for k in KEYWORDS)


def extract_product_info(base_url, p):
    handle = p.get("handle", "")
    variants = p.get("variants") or [{}]
    price = variants[0].get("price", "")
    return {
        "id": str(p.get("id", handle)),
        "title": p.get("title", "Unbenanntes Produkt"),
        "price": price,
        "url": f"{base_url}/products/{handle}" if handle else base_url,
    }


# ------------------------------------------------------ Shopify Blog/News ---
def fetch_atom_posts(base_url):
    """
    Versucht den Shopify-News-Feed /blogs/news.atom zu lesen (fuer Release-Events).
    Rueckgabe: Liste von Post-Dicts oder None.
    """
    url = f"{base_url}/blogs/news.atom"
    status, body = http_get(url)
    if status != 200 or not body:
        return None
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None
    posts = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        pid = entry.findtext(f"{ATOM_NS}id") or ""
        title = entry.findtext(f"{ATOM_NS}title") or "Neuer Beitrag"
        link_el = entry.find(f"{ATOM_NS}link")
        link = link_el.get("href") if link_el is not None else base_url
        posts.append({"id": pid, "title": title, "url": link})
    return posts


# ---------------------------------------------------------------- State ----
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"products": {}, "posts": {}, "initialized": False}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        s.setdefault("products", {})
        s.setdefault("posts", {})
        s.setdefault("initialized", True)
        return s
    except Exception:
        return {"products": {}, "posts": {}, "initialized": False}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# --------------------------------------------------------- Notifications ---
def _chunks(text, size):
    lines, out, cur = text.split("\n"), [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > size:
            out.append(cur)
            cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    if cur:
        out.append(cur)
    return out


def notify(text):
    sent = False
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if webhook:
        for chunk in _chunks(text, 1900):
            payload = json.dumps({"content": chunk}).encode("utf-8")
            req = urllib.request.Request(
                webhook, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                urllib.request.urlopen(req, timeout=TIMEOUT)
                sent = True
                time.sleep(0.6)
            except Exception as e:
                print("Discord-Fehler:", e)

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        api = f"https://api.telegram.org/bot{token}/sendMessage"
        for chunk in _chunks(text, 3900):
            payload = json.dumps({"chat_id": chat, "text": chunk}).encode("utf-8")
            req = urllib.request.Request(
                api, data=payload,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            try:
                urllib.request.urlopen(req, timeout=TIMEOUT)
                sent = True
                time.sleep(0.4)
            except Exception as e:
                print("Telegram-Fehler:", e)

    if not sent:
        print("!! Kein Benachrichtigungskanal gesetzt oder Versand fehlgeschlagen.")
        print(text)


# ------------------------------------------------------------------ Main ---
def main():
    state = load_state()
    first_run = not state.get("initialized")

    # Prioritaets-Shops zuerst
    shops = sorted(SHOPS, key=lambda s: not s.get("priority"))

    new_items = []     # (priority, shop_name, title, price, url)
    new_posts = []     # (shop_name, title, url)
    feed_ok, no_feed = [], []

    for shop in shops:
        name, base = shop["name"], shop["url"].rstrip("/")
        prio = shop.get("priority", False)

        # ---- Produkte ----
        products = fetch_shopify_products(base)
        if products is None:
            no_feed.append(name)
        else:
            feed_ok.append(name)
            current = {}
            for p in products:
                if product_matches(p.get("title", "")):
                    info = extract_product_info(base, p)
                    current[info["id"]] = info
            known = set(state["products"].get(name, []))
            if name not in state["products"]:
                # neuer Shop in der Liste -> Baseline, nicht melden
                state["products"][name] = list(current.keys())
            else:
                for pid, info in current.items():
                    if pid not in known:
                        new_items.append((prio, name, info["title"], info["price"], info["url"]))
                state["products"][name] = list(current.keys())

        # ---- News/Events (Shopify-Blog) ----
        posts = fetch_atom_posts(base)
        if posts is not None:
            current_posts = {p["id"]: p for p in posts}
            known_posts = set(state["posts"].get(name, []))
            if name not in state["posts"]:
                state["posts"][name] = list(current_posts.keys())
            else:
                for pid, p in current_posts.items():
                    if pid not in known_posts:
                        new_posts.append((name, p["title"], p["url"]))
                state["posts"][name] = list(current_posts.keys())

        time.sleep(0.5)

    state["initialized"] = True
    save_state(state)

    # ---- Erst-Lauf: nur Baseline-Bestaetigung, kein Spam ----
    if first_run:
        msg = [" One Piece Monitor ist aktiv.",
               f"Baseline gespeichert fuer {len(feed_ok)} Shops mit Feed.",
               "Ab jetzt bekommst du nur noch NEUE Artikel/News gemeldet."]
        if no_feed:
            msg.append("")
            msg.append("Ohne Produkt-Feed (bitte per changedetection.io beobachten):")
            msg.append(" - " + ", ".join(no_feed))
        notify("\n".join(msg))
        print("Baseline gesetzt.")
        return

    # ---- Meldung bauen ----
    if not new_items and not new_posts:
        print("Keine neuen Artikel oder Posts.")
        return

    lines = []
    if new_items:
        # Prioritaet zuerst, dann nach Shop
        new_items.sort(key=lambda x: (not x[0], x[1]))
        lines.append(" NEUE Artikel / Vorbestellungen:")
        last_shop = None
        for prio, shop_name, title, price, url in new_items:
            header = shop_name + ("   [PRIO]" if prio else "")
            if shop_name != last_shop:
                lines.append("")
                lines.append(header)
                last_shop = shop_name
            price_str = f" – {price} EUR" if price else ""
            lines.append(f" • {title}{price_str}\n   {url}")

    if new_posts:
        lines.append("")
        lines.append(" News / Release-Events:")
        for shop_name, title, url in new_posts:
            lines.append(f" • {shop_name}: {title}\n   {url}")

    notify("\n".join(lines))
    print(f"Gemeldet: {len(new_items)} Artikel, {len(new_posts)} Posts.")


if __name__ == "__main__":
    main()
