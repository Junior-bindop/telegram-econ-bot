"""
Bot Telegram - Alertes annonces economiques 3 etoiles (personnalisable)
Version "continue" avec long polling : un run reste actif plusieurs heures
(jusqu'a la limite GitHub de 6h) et repond aux commandes quasi instantanement
via le long polling Telegram, au lieu de dependre du cron GitHub Actions qui
peut deriver de plusieurs heures. Juste avant sa limite de temps, le run
relance automatiquement un nouveau run pour prendre le relais (chaine
continue, invisible pour l'utilisateur).

Le calendrier est telecharge UNE SEULE FOIS au demarrage du run et garde en
memoire toute sa duree : les commandes (/prochaine, /devises) repondent
instantanement a partir de cette memoire, sans nouvelle requete au calendrier.

Chaque evenement 3 etoiles declenche DEUX alertes independantes :
- une environ 5 minutes avant le debut
- une au moment meme du debut
"""

import json
import logging
import os
import subprocess
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
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")  # fourni automatiquement par Actions
WORKFLOW_FILE = "econ-bot.yml"

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

WARNING_LEAD_MINUTES = 5     # alerte "bientot"
FINAL_GRACE_MINUTES = 5      # alerte "maintenant" (tolerance apres le debut)
LOCAL_TZ_OFFSET_HOURS = 1    # Cameroun (UTC+1, pas de changement d'heure)
LOCAL_TZ = timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS))

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
STATE_FILE = Path(__file__).parent / "notified_events.json"
CONFIG_FILE = Path(__file__).parent / "bot_config.json"
STATE_RETENTION_HOURS = 24

POLL_TIMEOUT_SECONDS = 25          # duree du long polling Telegram
MAX_RUNTIME_SECONDS = int(5.75 * 3600)   # ~5h45, sous la limite GitHub de 6h

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
    """Long polling : Telegram garde la connexion ouverte et repond des
    qu'un message arrive (quasi instantane), ou apres POLL_TIMEOUT_SECONDS
    si rien ne se passe."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": POLL_TIMEOUT_SECONDS}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(url, params=params, timeout=POLL_TIMEOUT_SECONDS + 10)
        r.raise_for_status()
        return r.json().get("result", [])
    except requests.RequestException as e:
        log.error("Echec recuperation des messages Telegram : %s", e)
        return []


# ----------------------------------------------------------------------
# CALENDRIER ECONOMIQUE
# ----------------------------------------------------------------------

def fetch_calendar() -> list:
    """Telecharge le calendrier une seule fois par run et ne garde que les
    evenements High impact (3 etoiles), toutes devises confondues. Le
    filtrage par devise se fait ensuite separement (config['currencies'])."""
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

    log.info("Calendrier charge en memoire : %d evenement(s) 3 etoiles (toutes devises).", len(events))
    return events


# ----------------------------------------------------------------------
# ETAT ET CONFIGURATION (persistes via commit git incremental)
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


def git_commit_and_push(message: str) -> None:
    repo_dir = str(Path(__file__).parent)
    try:
        subprocess.run(
            ["git", "add", str(STATE_FILE), str(CONFIG_FILE)],
            check=True, cwd=repo_dir,
        )
        diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo_dir)
        if diff.returncode == 0:
            return  # rien de nouveau a committer
        subprocess.run(["git", "commit", "-m", message], check=True, cwd=repo_dir)
        subprocess.run(["git", "push"], check=True, cwd=repo_dir)
        log.info("Etat sauvegarde sur GitHub : %s", message)
    except subprocess.CalledProcessError as e:
        log.error("Echec git commit/push : %s", e)


def trigger_next_run() -> None:
    """Relance un nouveau run via l'API GitHub pour prendre le relais avant
    que le run actuel n'atteigne sa limite de temps (~6h max GitHub)."""
    if not GITHUB_REPOSITORY or not GITHUB_TOKEN:
        log.warning("Relai impossible (GITHUB_REPOSITORY/GITHUB_TOKEN manquant).")
        return
    url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }
    try:
        r = requests.post(url, headers=headers, json={"ref": "main"}, timeout=10)
        r.raise_for_status()
        log.info("Relai reussi : un nouveau run vient d'etre declenche.")
    except requests.RequestException as e:
        log.error("Echec du relai vers le prochain run : %s", e)


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
    local_time = nxt["time"].astimezone(LOCAL_TZ)
    flag = FLAGS.get(nxt["country"], "")
    return (
        f"\U0001F4C5 <b>Prochaine annonce 3\u2b50</b>\n"
        f"Sujet : {nxt['title']} {flag}\n"
        f"Heure : {local_time.strftime('%a %d/%m %H:%M')} (GMT+1)\n"
        f"Dans environ {delta_min} min"
    )


def handle_commands(config: dict, events: list, now: datetime) -> None:
    """Bloque jusqu'a POLL_TIMEOUT_SECONDS, ou repond des qu'un message
    arrive (long polling). Ne touche a rien si aucun message recu."""
    updates = get_telegram_updates(config.get("last_update_id"))
    if not updates:
        return
    log.info("Commandes : %d nouveau(x) message(s) recu(s) via getUpdates.", len(updates))

    for update in updates:
        config["last_update_id"] = update["update_id"] + 1

        msg = update.get("message") or update.get("channel_post")
        if not msg or "text" not in msg:
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
            continue

        send_telegram_message(reply, chat_id=chat_id)
        log.info("Commande traitee : %s", command)


# ----------------------------------------------------------------------
# ALERTES
# ----------------------------------------------------------------------

def format_message(event: dict, mode: str, minutes_left: int = None) -> str:
    local_time = event["time"].astimezone(LOCAL_TZ)
    flag = FLAGS.get(event["country"], "")

    details_lines = []
    if event["forecast"] != "\u2014":
        details_lines.append(f"Prevision : {event['forecast']}")
    if event["previous"] != "\u2014":
        details_lines.append(f"Precedent : {event['previous']}")
    if not details_lines:
        details_lines.append("Pas de donnees previsionnelles disponibles")
    details = "\n".join(details_lines)

    if mode == "warning":
        header = "\U0001F4E2 <b>ANNONCE ECONOMIQUE</b> \U0001F6A8"
        timing = f"Heure : {local_time.strftime('%H:%M')} (GMT+1) \u2014 dans ~{minutes_left} min"
    else:
        header = "\U0001F534 <b>ANNONCE EN COURS MAINTENANT</b>"
        timing = f"Heure : {local_time.strftime('%H:%M')} (GMT+1)"

    return (
        f"{header}\n"
        f"Sujet : {event['title']} {flag}\n"
        f"Importance : \U0001F31F\U0001F31F\U0001F31F\n"
        f"Details :\n{details}\n"
        f"{timing}"
    )


# ----------------------------------------------------------------------
# BOUCLE PRINCIPALE (continue, long polling, relai avant expiration)
# ----------------------------------------------------------------------

def run_forever():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("BOT_TOKEN et CHAT_ID doivent etre definis (secrets GitHub Actions).")

    start = time.monotonic()
    state = load_state()
    config = load_config()
    events = fetch_calendar()   # une seule fois, garde en memoire tout le run

    log.info(
        "Demarrage du mode continu. %d evenement(s) 3 etoiles charges. Devises : %s.",
        len(events), ", ".join(config["currencies"]),
    )

    last_heartbeat = time.monotonic()

    while time.monotonic() - start < MAX_RUNTIME_SECONDS:
        try:
            now = datetime.now(timezone.utc)

            if time.monotonic() - last_heartbeat > 300:  # toutes les ~5 min
                log.info(
                    "Toujours actif depuis %d min. En attente de commandes/annonces...",
                    int((time.monotonic() - start) / 60),
                )
                last_heartbeat = time.monotonic()

            # --- Commandes (long polling, quasi instantane) ---
            poll_start = time.monotonic()
            config_before = json.dumps(config, sort_keys=True)
            handle_commands(config, events, now)
            if json.dumps(config, sort_keys=True) != config_before:
                save_config(config)
                git_commit_and_push("Update bot config (commande Telegram)")

            # Securite anti-martelage si getUpdates a echoue tres vite (erreur reseau)
            if time.monotonic() - poll_start < 1:
                time.sleep(3)

            # --- Alertes : "bientot" (5 min avant) + "maintenant" (au debut) ---
            for event in events:
                if event["country"] not in config["currencies"]:
                    continue
                minutes_left = (event["time"] - now).total_seconds() / 60

                warn_key = event["id"] + ":warning"
                if warn_key not in state and 0 < minutes_left <= WARNING_LEAD_MINUTES:
                    if send_telegram_message(format_message(event, "warning", round(minutes_left))):
                        state[warn_key] = event["time"].isoformat()
                        save_state(state)
                        git_commit_and_push("Update notified events (bientot)")
                        log.info("Alerte 'bientot' envoyee : %s (%s)", event["title"], event["country"])

                final_key = event["id"] + ":final"
                if final_key not in state and -FINAL_GRACE_MINUTES <= minutes_left <= 0:
                    if send_telegram_message(format_message(event, "final")):
                        state[final_key] = event["time"].isoformat()
                        save_state(state)
                        git_commit_and_push("Update notified events (maintenant)")
                        log.info("Alerte 'maintenant' envoyee : %s (%s)", event["title"], event["country"])

            # Purge occasionnelle des vieilles entrees (pas besoin d'un commit dedie)
            if int(time.monotonic() - start) % 600 < 2:  # environ toutes les 10 min
                cutoff = now - timedelta(hours=STATE_RETENTION_HOURS)
                pruned = {eid: ts for eid, ts in state.items() if date_parser.parse(ts) > cutoff}
                if len(pruned) != len(state):
                    state = pruned
                    save_state(state)
                    git_commit_and_push("Purge des vieux evenements")

        except Exception as ex:
            # Une erreur isolee (reseau, parsing, etc.) ne doit jamais faire
            # planter tout le run de plusieurs heures : on journalise et on
            # continue a la prochaine iteration.
            log.error("Erreur inattendue dans la boucle (ignoree, on continue) : %s", ex)
            time.sleep(5)

    log.info("Limite de temps interne atteinte (~%d min). Relai vers un nouveau run.",
              int(MAX_RUNTIME_SECONDS / 60))
    save_state(state)
    save_config(config)
    git_commit_and_push("Etat final avant relai")
    trigger_next_run()


if __name__ == "__main__":
    try:
        run_forever()
    except Exception as fatal_error:
        # Filet de securite ultime : meme en cas de plantage totalement
        # imprevu, on tente de relancer le prochain run pour ne jamais
        # casser la chaine de relais.
        log.error("Erreur fatale imprevue : %s", fatal_error)
        trigger_next_run()
        raise
