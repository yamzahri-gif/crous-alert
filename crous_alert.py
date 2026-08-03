#!/usr/bin/env python3
"""
Alerte logements CROUS - Toulouse et agglomération.

Scrape trouverunlogement.lescrous.fr, filtre sur Toulouse + communes
limitrophes, compare avec l'état précédent (seen.json) et envoie une
alerte (email + Telegram) pour chaque NOUVEAU logement détecté.

Licence des données : Etalab-2.0 (site gouvernemental, réutilisation libre).
"""

import hashlib
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

# Recherche filtrée par le site lui-même (bounds générés par sa propre
# géolocalisation en tapant "Toulouse"). Sert de SECONDE source en plus du
# scan complet ci-dessus : le site semble parfois incohérent entre sa liste
# "toute la France" et sa recherche filtrée par ville, donc on combine les
# deux pour réduire le risque de rater un logement.
TOULOUSE_BOUNDS = "1.3503956_43.668708_1.5153795_43.532654"
EXTRA_SEARCH_URLS = [
    f"{BASE_URL}/tools/42/search?bounds={TOULOUSE_BOUNDS}&locationName=Toulouse",
    f"{BASE_URL}/tools/47/search?bounds={TOULOUSE_BOUNDS}&locationName=Toulouse",
]

# Communes à surveiller (Toulouse + agglomération). Comparaison insensible
# à la casse et aux accents sur l'adresse affichée par le CROUS.
VILLES_CIBLES = [
    "toulouse",
    "rangueil",
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
]

STATE_FILE = Path(__file__).parent / "seen.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://trouverunlogement.lescrous.fr/",
    "Connection": "keep-alive",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)

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


def notify_logement(logement: dict, reason: str) -> None:
    """reason: 'nouveau' (jamais vu) ou 'modifié' (déjà vu, caractéristiques
    changées : prix, surface, type, équipements...)."""
    label = "Nouveau logement CROUS" if reason == "nouveau" else "Logement CROUS mis à jour"
    subject = f"🏠 {label} : {logement['titre']} ({logement['ville']})"
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
        f"🏠 <b>{label}</b>\n"
        f"<b>{logement['titre']}</b> — {logement['ville']}\n"
        f"{logement['adresse']}\n"
        f"💶 {logement['prix']} | 📐 {logement['surface']} | {logement['type']}\n"
        f"{logement['url']}"
    )
    send_telegram(tg_text)


# --- Scraping ----------------------------------------------------------

def fetch_page(path_or_url: str, page: int) -> BeautifulSoup:
    # Visite la page d'accueil une première fois pour obtenir les cookies de
    # session (certains sites bloquent les requêtes "à froid" sans cookies).
    if not SESSION.cookies:
        SESSION.get(BASE_URL, timeout=20)

    if path_or_url.startswith("http"):
        # URL complète déjà paramétrée (ex. avec bounds/locationName).
        url = path_or_url
        params = {"page": page} if page > 1 else {}
    else:
        url = f"{BASE_URL}{path_or_url}"
        params = {"page": page} if page > 1 else {}

    resp = SESSION.get(url, params=params, timeout=20)
    print(f"[http] GET {resp.url} -> statut {resp.status_code}, {len(resp.text)} octets reçus.")
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def get_total_pages(soup: BeautifulSoup) -> int:
    """Fallback approximatif : cherche un lien 'Dernière page' si présent.
    Non utilisé comme source de vérité (voir scrape_all qui boucle jusqu'à
    une page vide), mais gardé pour du logging informatif."""
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

        # Le conteneur doit englober TOUTE la carte (image, prix, titre,
        # adresse, surface...). On priorise le <li> englobant, car un <div>
        # ancestor peut être un simple wrapper interne du titre qui n'inclut
        # pas le prix (affiché ailleurs dans la carte).
        container = h3.find_parent("li") or h3.find_parent(["div", "article"]) or h3.parent
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

        # Empreinte du contenu complet de la carte (prix, surface, type,
        # équipements...) : permet de détecter un changement de
        # caractéristiques sur un logement déjà connu (même ID), et pas
        # seulement l'apparition d'un nouvel ID.
        signature = hashlib.md5(text_block.encode("utf-8")).hexdigest()

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
                "signature": signature,
            }
        )

    return results


def is_target_city(adresse: str) -> bool:
    adresse_norm = adresse.lower()
    return any(ville in adresse_norm for ville in VILLES_CIBLES)


def scrape_all() -> list[dict]:
    all_logements = []
    MAX_PAGES = 25  # garde-fou pour éviter une boucle infinie

    for path in SEARCH_PATHS:
        page = 1
        path_count = 0
        while page <= MAX_PAGES:
            try:
                soup = fetch_page(path, page)
            except requests.RequestException as e:
                print(f"[scrape] échec page {page} de {path}: {e}")
                break

            listings = parse_listings(soup)
            if not listings:
                # Page vide = on a dépassé la dernière page réelle.
                break

            all_logements.extend(listings)
            path_count += len(listings)
            page += 1

        print(f"[scrape] {path}: {path_count} logement(s) sur {page - 1} page(s).")

    # Seconde passe : recherche filtrée par le site lui-même sur "Toulouse"
    # (bounds réels générés par leur géolocalisation). Sert de filet de
    # sécurité en plus du scan complet ci-dessus, au cas où les deux listes
    # ne soient pas parfaitement synchronisées côté serveur du CROUS.
    for url in EXTRA_SEARCH_URLS:
        try:
            soup = fetch_page(url, 1)
        except requests.RequestException as e:
            print(f"[scrape] échec recherche filtrée {url}: {e}")
            continue
        listings = parse_listings(soup)
        print(f"[scrape] recherche filtrée Toulouse ({url}): {len(listings)} logement(s).")
        all_logements.extend(listings)

    # Dédoublonnage par ID (un même logement peut apparaître dans le scan
    # complet ET dans la recherche filtrée).
    dedup = {l["id"]: l for l in all_logements}
    all_logements = list(dedup.values())

    print(f"[diagnostic] {len(all_logements)} logement(s) au total (toutes villes confondues, avant filtrage).")
    return [l for l in all_logements if is_target_city(l["adresse"])]


# --- État / persistance -------------------------------------------------

def load_seen() -> dict:
    """Retourne un dict {id: signature}. Migre automatiquement l'ancien
    format (simple liste d'IDs) en attribuant une signature vide, ce qui
    déclenchera une notification 'mis à jour' une seule fois lors de la
    migration (sans casser le script)."""
    if not STATE_FILE.exists():
        return {}
    data = json.loads(STATE_FILE.read_text())
    if isinstance(data, list):
        return {logement_id: "" for logement_id in data}
    return data


def save_seen(seen: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(seen, ensure_ascii=False, indent=2, sort_keys=True)
    )


# --- Main ----------------------------------------------------------------

def main():
    print("Recherche des logements CROUS Toulouse + agglomération...")
    logements = scrape_all()
    print(f"{len(logements)} logement(s) trouvé(s) dans la zone ciblée.")

    seen = load_seen()  # {id: signature}

    nouveaux = []
    modifies = []
    for l in logements:
        ancienne_signature = seen.get(l["id"])
        if ancienne_signature is None:
            nouveaux.append(l)
        elif ancienne_signature != l["signature"]:
            modifies.append(l)

    if not nouveaux and not modifies:
        print("Aucun changement depuis la dernière vérification.")
    else:
        if nouveaux:
            print(f"{len(nouveaux)} nouveau(x) logement(s) ! Envoi des alertes...")
            for logement in nouveaux:
                print(f"  -> {logement['titre']} ({logement['adresse']})")
                notify_logement(logement, reason="nouveau")
        if modifies:
            print(f"{len(modifies)} logement(s) modifié(s) ! Envoi des alertes...")
            for logement in modifies:
                print(f"  -> {logement['titre']} ({logement['adresse']})")
                notify_logement(logement, reason="modifié")

    # Remplace l'état par ce qui est actuellement en ligne (et non une
    # fusion cumulative). Un logement qui disparaît puis réapparaît plus
    # tard (annulation, désistement...) doit redéclencher une alerte.
    nouvel_etat = {l["id"]: l["signature"] for l in logements}
    save_seen(nouvel_etat)


if __name__ == "__main__":
    sys.exit(main())
