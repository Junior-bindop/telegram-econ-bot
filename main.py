"""
Bot Telegram - Alertes annonces economiques 3 etoiles (USD, EUR, GBP, JPY)
Version "run-once", concue pour tourner via un cron GitHub Actions
(le workflow relance ce script toutes les 5 minutes).

A chaque execution :
1. Telecharge le calendrier Forex Factory
2. Filtre les evenements High impact (3 etoiles) sur USD/EUR/GBP/JPY
3. Envoie une alerte Telegram pour ceux qui commencent dans <= 10 minutes
   et n'ont pas deja ete notifies
4. Sauvegarde l'etat (evenements deja notifies) dans notified_events.json
   -> ce fichier est ensuite commit dans le depot par le workflow GitHub
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dateutil import parser as date_parser

# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

CURRENCIES = {"USD", "EUR", "GBP", "JPY"}
IMPACT_FILTER = {"High"}          # 3 etoiles = "High" chez Forex Factory

FLAGS = {
    "USD": "\U0001F1FA\U0001F1F8",   # 🇺🇸
    "EUR": "\U0001F1EA\U0001F1FA",   # 🇪🇺
    "GBP": "\U0001F1EC\U0001F1E7",   # 🇬🇧
    "JPY": "\U0001F1EF\U0001F1F5",   # 🇯🇵
}

# GitHub Actions ne garantit pas une precision a la minute pres pour les
# taches planifiees (delais possibles en cas de forte charge). On alerte
# donc des qu'il reste <= 10 min avant l'annonce (plutot que d'exiger
# precisement 5-10 min), pour ne jamais rater completement une alerte.
LEAD_TIME_MAX = 10

LOCAL_TZ_OFFSET_HOURS = 1   # Cameroun (UTC+1, pas de changement d'heure)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_FILE = Path(__file__).parent / "notified_events.json"
STATE_RETENTION_HOURS = 24  # purge les entrees plus vieilles que ca

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("econ-bot")


# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------

def send_telegram_message(text: str) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        # On journalise le corps de la reponse Telegram (souvent tres parlant :
        # "chat not found", "bot was blocked", "not enough rights to send text
        # messages", etc.)
        body = getattr(e.response, "text", "")
        log.error("Echec envoi Telegram : %s | Reponse: %s", e, body[:300])
        return False


# ----------------------------------------------------------------------
# CALENDRIER ECONOMIQUE
# ----------------------------------------------------------------------

def fetch_calendar() -> list:
    """Telecharge le calendrier, avec 3 tentatives en cas d'echec ou de
    blocage (Forex Factory renvoie parfois une page HTML "Request Denied"
    au lieu du JSON si trop de requetes viennent de la meme plage d'IP,
    ce qui peut arriver avec les IP partagees de GitHub Actions)."""
    raw_events = None
    max_attempts = 3

    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.get(
                CALENDAR_URL,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0 (compatible; econ-bot/1.0)"},
            )
            content_type = r.headers.get("content-type", "")
            preview = r.text[:150].strip()

            looks_like_html = preview.lower().startswith(("<!doctype", "<html"))
            if "json" not in content_type.lower() or looks_like_html:
                log.warning(
                    "Tentative %d/%d : reponse inattendue (probable blocage/limite "
                    "de requetes). Status=%s Content-Type=%s Extrait=%r",
                    attempt, max_attempts, r.status_code, content_type, preview,
                )
                if attempt < max_attempts:
                    time.sleep(15)
                    continue
                return []

            r.raise_for_status()
            raw_events = r.json()
            break

        except requests.RequestException as e:
            log.warning("Tentative %d/%d : echec telechargement calendrier : %s", attempt, max_attempts, e)
            if attempt < max_attempts:
                time.sleep(15)
            else:
                log.error("Abandon apres %d tentatives.", max_attempts)
                return []

    if raw_events is None:
        return []

    log.info("Calendrier telecharge : %d evenement(s) au total (toutes devises/impacts confondus).", len(raw_events))

    events = []
    for e in raw_events:
        try:
            if e.get("country") not in CURRENCIES:
                continue
            if e.get("impact") not in IMPACT_FILTER:
                continue
            event_time = date_parser.parse(e["date"])
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            events.append({
                "id": f'{e.get("title")}_{event_time.isoformat()}',
                "title": e.get("title"),
                "country": e.get("country"),
                "time": event_time,
                "forecast": e.get("forecast") or "\u2014",
                "previous": e.get("previous") or "\u2014",
            })
        except Exception as ex:
            log.warning("Evenement ignore (parsing) : %s", ex)
    return events


# ----------------------------------------------------------------------
# ETAT (evenements deja notifies)
# ----------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ----------------------------------------------------------------------
# LOGIQUE PRINCIPALE
# ----------------------------------------------------------------------

def format_message(event: dict, minutes_left: int) -> str:
    local_time = event["time"] + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)
    flag = FLAGS.get(event["country"], "")

    details_lines = []
    if event["forecast"] != "\u2014":
        details_lines.append(f"Prevision : {event['forecast']}")
    if event["previous"] != "\u2014":
        details_lines.append(f"Precedent : {event['previous']}")
    if not details_lines:
        details_lines.append("Pas de donnees previsionnelles disponibles")
    details = "\n".join(details_lines)

    return (
        f"\U0001F4E2 <b>ANNONCE ECONOMIQUE</b> \U0001F6A8\n"
        f"Sujet : {event['title']} {flag}\n"
        f"Importance : \U0001F31F\U0001F31F\U0001F31F\n"
        f"Details :\n{details}\n"
        f"Heure : {local_time.strftime('%H:%M')} (GMT+1) \u2014 dans ~{minutes_left} min"
    )


def run_once():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit(
            "BOT_TOKEN et CHAT_ID doivent etre definis (secrets GitHub Actions)."
        )

    now = datetime.now(timezone.utc)
    state = load_state()
    events = fetch_calendar()
    log.info("%d evenement(s) 3 etoiles dans le calendrier.", len(events))

    sent = 0
    for event in events:
        if event["id"] in state:
            continue
        minutes_left = (event["time"] - now).total_seconds() / 60
        if 0 < minutes_left <= LEAD_TIME_MAX:
            ok = send_telegram_message(format_message(event, round(minutes_left)))
            if ok:
                state[event["id"]] = event["time"].isoformat()
                sent += 1
                log.info("Alerte envoyee : %s (%s)", event["title"], event["country"])
            else:
                log.error(
                    "Alerte NON envoyee (sera retentee au prochain run) : %s (%s)",
                    event["title"], event["country"],
                )

    # Purge des vieilles entrees pour ne pas faire grossir le fichier indefiniment
    cutoff = now - timedelta(hours=STATE_RETENTION_HOURS)
    state = {
        eid: ts for eid, ts in state.items()
        if date_parser.parse(ts) > cutoff
    }

    save_state(state)
    log.info(
        "%d alerte(s) envoyee(s). Etat sauvegarde (%d evenement(s) suivis).",
        sent, len(state),
    )


if __name__ == "__main__":
    run_once()
