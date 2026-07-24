#!/usr/bin/env python3
"""
Alerte logements CROUS - Toulouse et agglomération.

Scrape trouverunlogement.lescrous.fr, filtre sur Toulouse + communes
limitrophes, compare avec l'état précédent (seen.json) et envoie une
alerte (email + Telegram) pour chaque NOUVEAU logement détecté.

Licence des données : Etalab-2.0 (site gouvernemental, réutilisation libre).
"""

import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# --- Configuration ---------------------------------------------------

BASE_URL = "https://trouverunlogement.lescrous.fr"
# tools/42 = campagne en cours (phase complémentaire 2025-2026).
# tools/47 = campagne 2026-2027, à activer quand elle ouvrira.
SEARCH_PATHS = ["/tools/42/search", "/tools/47/search"]

# Communes à surveiller (Toulouse + agglomération). Comparaison insensible
# à la casse et aux accents sur l'adresse affichée par le CROUS.
VILLES_CIBLES = [
    "toulouse",
    "blagnac",
    "colomiers",
    "balma",
    "tournefeuille",
    "cugnaux",
    "ramonville",
    "labege",
    "l'union",
    "union",
    "saint-orens",
    "castanet",
    "muret",
    "rangueil",
    "Toulouse",
    "Blagnac",
    "Colomiers",
    "Balma",
    "Tournefeuille",
    "Cugnaux",
    "Ramonville",
    "Castanet",
    "Muret",
    "Rangueil",
    
]

STATE_FILE = Path(__file__).parent / "seen.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# --- Notification -----------------------------------------------------

def send_email(subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    dest = os.environ.get("ALERT_EMAIL_TO", user)

    if not user or not password:
        print("[email] GMAIL_USER / GMAIL_APP_PASSWORD manquants, email ignoré.")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = dest

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(user, password)
        server.sendmail(user, [dest], msg.as_string())
    print("[email] envoyé.")


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants, ignoré.")
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[telegram] échec: {resp.status_code} {resp.text}")
    else:
        print("[telegram] envoyé.")


def notify_new_logement(logement: dict) -> None:
    subject = f"🏠 Nouveau logement CROUS : {logement['titre']} ({logement['ville']})"
    body = (
        f"{logement['titre']}\n"
        f"{logement['adresse']}\n"
        f"Prix : {logement['prix']}\n"
        f"Surface : {logement['surface']}\n"
        f"Type : {logement['type']}\n\n"
        f"Lien : {logement['url']}"
    )
    send_email(subject, body)

    tg_text = (
        f"🏠 <b>Nouveau logement CROUS</b>\n"
        f"<b>{logement['titre']}</b> — {logement['ville']}\n"
        f"{logement['adresse']}\n"
        f"💶 {logement['prix']} | 📐 {logement['surface']} | {logement['type']}\n"
        f"{logement['url']}"
    )
    send_telegram(tg_text)


# --- Scraping ----------------------------------------------------------

def fetch_page(path: str, page: int) -> BeautifulSoup:
    url = f"{BASE_URL}{path}"
    params = {"page": page} if page > 1 else {}
    resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def get_total_pages(soup: BeautifulSoup) -> int:
    """Cherche le lien 'Dernière page' pour connaître le nombre total de pages."""
    last_link = soup.find("a", string=re.compile("Dernière page", re.I))
    if last_link and last_link.get("href"):
        m = re.search(r"page=(\d+)", last_link["href"])
        if m:
            return int(m.group(1))
    return 1


def parse_listings(soup: BeautifulSoup) -> list[dict]:
    """Extrait chaque logement affiché sur une page de résultats."""
    results = []

    # Chaque logement est un <h3> avec un <a> vers /tools/{id}/accommodations/{id}
    for h3 in soup.find_all(["h3", "h2"]):
        link = h3.find("a", href=re.compile(r"/accommodations/\d+"))
        if not link:
            continue

        titre = link.get_text(strip=True)
        url = link["href"]
        if url.startswith("/"):
            url = BASE_URL + url

        # Le conteneur parent contient l'adresse, prix, surface, type
        container = h3.find_parent(["li", "div", "article"]) or h3.parent
        text_block = container.get_text("\n", strip=True) if container else ""
        lines = [l for l in text_block.split("\n") if l.strip()]

        adresse = ""
        # L'adresse suit généralement le titre dans le bloc
        idx_titre = next((i for i, l in enumerate(lines) if titre in l), None)
        if idx_titre is not None and idx_titre + 1 < len(lines):
            adresse = lines[idx_titre + 1]

        prix_match = re.search(r"(\d+[,.]?\d*)\s*€", text_block)
        prix = prix_match.group(0) if prix_match else "?"

        surface_match = re.search(r"(\d+[,.]?\d*)\s*m²", text_block)
        surface = surface_match.group(0) if surface_match else "?"

        type_match = re.search(r"\b(Individuel|Couple|Colocation)\b", text_block)
        type_log = type_match.group(0) if type_match else "?"

        m_id = re.search(r"/accommodations/(\d+)", url)
        logement_id = m_id.group(1) if m_id else url

        results.append(
            {
                "id": logement_id,
                "titre": titre,
                "adresse": adresse,
                "ville": adresse,  # affiné plus bas
                "prix": prix,
                "surface": surface,
                "type": type_log,
                "url": url,
            }
        )

    return results


def is_target_city(adresse: str) -> bool:
    adresse_norm = adresse.lower()
    return any(ville in adresse_norm for ville in VILLES_CIBLES)


def scrape_all() -> list[dict]:
    all_logements = []
    for path in SEARCH_PATHS:
        try:
            first_page = fetch_page(path, 1)
        except requests.RequestException as e:
            print(f"[scrape] échec sur {path}: {e}")
            continue

        total_pages = get_total_pages(first_page)
        print(f"[scrape] {path}: {total_pages} page(s) au total.")

        all_logements.extend(parse_listings(first_page))
        for page in range(2, total_pages + 1):
            try:
                soup = fetch_page(path, page)
            except requests.RequestException as e:
                print(f"[scrape] échec page {page} de {path}: {e}")
                continue
            all_logements.extend(parse_listings(soup))

    return [l for l in all_logements if is_target_city(l["adresse"])]


# --- État / persistance -------------------------------------------------

def load_seen() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2))


# --- Main ----------------------------------------------------------------

def main():
    print("Recherche des logements CROUS Toulouse + agglomération...")
    logements = scrape_all()
    print(f"{len(logements)} logement(s) trouvé(s) dans la zone ciblée.")

    seen = load_seen()
    nouveaux = [l for l in logements if l["id"] not in seen]

    if not nouveaux:
        print("Aucun nouveau logement depuis la dernière vérification.")
    else:
        print(f"{len(nouveaux)} nouveau(x) logement(s) ! Envoi des alertes...")
        for logement in nouveaux:
            print(f"  -> {logement['titre']} ({logement['adresse']})")
            notify_new_logement(logement)

    # Met à jour l'état avec tout ce qui est actuellement en ligne
    current_ids = {l["id"] for l in logements}
    save_seen(seen | current_ids)


if __name__ == "__main__":
    sys.exit(main())
