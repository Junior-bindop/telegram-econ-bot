"""
Bot Telegram - Alertes annonces economiques 3 etoiles (personnalisable)
Version "run-once", concue pour tourner via un cron GitHub Actions
(le workflow relance ce script toutes les 5 minutes).

A chaque execution :
1. Verifie les nouvelles commandes recues depuis Telegram (/prochaine, /devises, /aide)
2. Telecharge le calendrier Forex Factory (evenements High impact / 3 etoiles)
3. Envoie une alerte Telegram pour ceux qui commencent dans <= 10 minutes
   et n'ont pas deja ete notifies, pour les devises suivies
4. Sauvegarde l'etat (evenements notifies + config) -> commit automatique
   par le workflow GitHub
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

DEFAULT_CURRENCIES = ["USD", "EUR", "GBP", "JPY"]
IMPACT_FILTER = {"High"}          # 3 etoiles = "High" chez Forex Factory

FLAGS = {
    "USD": "\U0001F1FA\U0001F1F8",
    "EUR": "\U0001F1EA\U0001F1FA",
    "GBP": "\U0001F1EC\U0001F1E7",
    "JPY": "\U0001F1EF\U0001F1F5",
    "AUD": "\U0001F1E6\U0001F1FA",
    "CAD": "\U0001F1E8\U0001F1E6",
    "CHF": "\U0001F1E8\U0001F1ED",
    "CNY": "\U0001F1E8\U0001F1F3",
    "NZD": "\U0001F1F3\U0001F1FF",
}

# GitHub Actions ne garantit pas une precision a la minute pres pour les
# taches planifiees. On alerte donc des qu'il reste <= 10 min avant
# l'annonce (plutot que d'exiger precisement 5-10 min).
LEAD_TIME_MAX = 10

LOCAL_TZ_OFFSET_HOURS = 1   # Cameroun (UTC+1, pas de changement d'heure)

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_FILE = Path(__file__).parent / "notified_events.json"
CONFIG_FILE = Path(__file__).parent / "bot_config.json"
STATE_RETENTION_HOURS = 24

COMMANDS_HELP = (
    "\U0001F4CB <b>Commandes disponibles</b>\n"
    "/prochaine - la prochaine annonce 3\u2b50 a venir\n"
    "/devises - voir les devises actuellement suivies\n"
    "/devises USD,EUR,GBP,JPY - changer les devises suivies\n"
    "/aide - ce message"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("econ-bot")


# ----------------------------------------------------------------------
# TELEGRAM
# ----------------------------------------------------------------------

def send_telegram_message(text: str, chat_id: str = None) -> bool:
    target = chat_id or CHAT_ID
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": target, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        return True
    except requests.RequestException as e:
        body = getattr(e.response, "text", "")
        log.error("Echec envoi Telegram : %s | Reponse: %s", e, body[:300])
        return False


def get_telegram_updates(offset) -> list:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("result", [])
    except requests.RequestException as e:
        log.error("Echec recuperation des messages Telegram : %s", e)
        return []


# ----------------------------------------------------------------------
# CALENDRIER ECONOMIQUE
# ----------------------------------------------------------------------

def fetch_calendar() -> list:
    """Telecharge le calendrier et ne garde que les evenements High impact
    (3 etoiles), toutes devises confondues. Le filtrage par devise se fait
    ensuite separement (config['currencies']), pour pouvoir le changer
    depuis Telegram sans toucher au code."""
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
                    "Tentative %d/%d : reponse inattendue (probable blocage/limite). "
                    "Status=%s Content-Type=%s Extrait=%r",
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

    log.info("Calendrier telecharge : %d evenement(s) au total.", len(raw_events))

    events = []
    for e in raw_events:
        try:
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

    log.info("%d evenement(s) 3 etoiles au total (toutes devises).", len(events))
    return events


# ----------------------------------------------------------------------
# ETAT ET CONFIGURATION (persistes via commit git)
# ----------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2))


def load_state() -> dict:
    return load_json(STATE_FILE, {})


def save_state(state: dict) -> None:
    save_json(STATE_FILE, state)


def load_config() -> dict:
    config = load_json(CONFIG_FILE, {})
    config.setdefault("currencies", list(DEFAULT_CURRENCIES))
    config.setdefault("last_update_id", None)
    return config


def save_config(config: dict) -> None:
    save_json(CONFIG_FILE, config)


# ----------------------------------------------------------------------
# COMMANDES TELEGRAM
# ----------------------------------------------------------------------

def build_next_event_reply(config: dict, events: list, now: datetime) -> str:
    matching = [
        e for e in events
        if e["country"] in config["currencies"] and e["time"] > now
    ]
    if not matching:
        return "Aucune annonce 3\u2b50 a venir cette semaine pour les devises suivies."
    matching.sort(key=lambda e: e["time"])
    nxt = matching[0]
    delta_min = round((nxt["time"] - now).total_seconds() / 60)
    local_time = nxt["time"] + timedelta(hours=LOCAL_TZ_OFFSET_HOURS)
    flag = FLAGS.get(nxt["country"], "")
    return (
        f"\U0001F4C5 <b>Prochaine annonce 3\u2b50</b>\n"
        f"Sujet : {nxt['title']} {flag}\n"
        f"Heure : {local_time.strftime('%a %d/%m %H:%M')} (GMT+1)\n"
        f"Dans environ {delta_min} min"
    )


def handle_commands(config: dict, events: list, now: datetime) -> None:
    updates = get_telegram_updates(config.get("last_update_id"))
    log.info("Commandes : %d nouveau(x) message(s) recu(s) via getUpdates.", len(updates))

    for update in updates:
        # On avance l'offset dans tous les cas, pour ne jamais retraiter
        # un vieux message (meme ignore).
        config["last_update_id"] = update["update_id"] + 1

        msg = update.get("message") or update.get("channel_post")
        if not msg or "text" not in msg:
            log.info("Update %s ignore (pas de texte / type non gere).", update.get("update_id"))
            continue

        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()
        log.info("Message recu : chat_id=%s (attendu=%s) texte=%r", chat_id, CHAT_ID, text)

        if str(chat_id) != str(CHAT_ID):
            log.info("Message ignore : provient d'un autre chat que celui configure.")
            continue

        if not text:
            continue
        command = text.split()[0].split("@")[0].lower()

        if command == "/prochaine":
            reply = build_next_event_reply(config, events, now)
        elif command == "/devises":
            parts = text.split(maxsplit=1)
            if len(parts) == 1:
                reply = "Devises suivies actuellement : " + ", ".join(config["currencies"])
            else:
                new_currencies = sorted({c.strip().upper() for c in parts[1].split(",") if c.strip()})
                if not new_currencies:
                    reply = "Format invalide. Exemple : /devises USD,EUR,GBP,JPY"
                else:
                    config["currencies"] = new_currencies
                    reply = "\u2705 Devises mises a jour : " + ", ".join(new_currencies)
        elif command in ("/aide", "/start", "/help"):
            reply = COMMANDS_HELP
        else:
            continue  # commande inconnue, on ignore silencieusement

        send_telegram_message(reply, chat_id=chat_id)
        log.info("Commande traitee : %s", command)


# ----------------------------------------------------------------------
# ALERTES
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
    config = load_config()
    events = fetch_calendar()

    handle_commands(config, events, now)

    sent = 0
    for event in events:
        if event["country"] not in config["currencies"]:
            continue
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

    cutoff = now - timedelta(hours=STATE_RETENTION_HOURS)
    state = {
        eid: ts for eid, ts in state.items()
        if date_parser.parse(ts) > cutoff
    }

    save_state(state)
    save_config(config)
    log.info(
        "%d alerte(s) envoyee(s). Devises suivies : %s. Etat sauvegarde (%d evenement(s) suivis).",
        sent, ", ".join(config["currencies"]), len(state),
    )


if __name__ == "__main__":
    run_once()
