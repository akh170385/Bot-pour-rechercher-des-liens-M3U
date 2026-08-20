import os
import re
import json
import time
import logging
from datetime import datetime
from typing import List, Set

import telebot
from flask import Flask, request


# ============================================================
# LOGS
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN n'est pas défini dans les variables Render."
    )


# ============================================================
# ADMIN IDS
# ============================================================

try:
    ADMIN_IDS = [
        int(x.strip())
        for x in os.environ.get("ADMIN_IDS", "").split(",")
        if x.strip()
    ]
except ValueError:
    ADMIN_IDS = []


if not ADMIN_IDS:
    logger.warning(
        "⚠️ ADMIN_IDS n'est pas configuré."
    )


# ============================================================
# STOCKAGE
# ============================================================

DATA_FOLDER = "m3u_database"
INDEX_FILE = "m3u_index.json"

os.makedirs(DATA_FOLDER, exist_ok=True)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# TELEGRAM BOT
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None,
    threaded=False
)


# ============================================================
# DATABASE / INDEX
# ============================================================

index = {}


def load_index():
    global index

    if not os.path.exists(INDEX_FILE):
        index = {}

        logger.info(
            "📁 Aucun index local trouvé. Index vide."
        )

        return

    try:

        with open(
            INDEX_FILE,
            "r",
            encoding="utf-8"
        ) as f:

            index = json.load(f)

        logger.info(
            f"📂 Index local chargé : {len(index)} fichiers"
        )

    except Exception as e:

        logger.exception(
            f"❌ Impossible de charger {INDEX_FILE}: {e}"
        )

        index = {}


def save_index():

    try:

        with open(
            INDEX_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            json.dump(
                index,
                f,
                indent=2,
                ensure_ascii=False
            )

        logger.info(
            "💾 Index sauvegardé localement."
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur sauvegarde index : {e}"
        )


# ============================================================
# OUTILS
# ============================================================

def get_all_links() -> Set[str]:

    all_links = set()

    for filename in index.keys():

        filepath = os.path.join(
            DATA_FOLDER,
            filename
        )

        if not os.path.exists(filepath):
            continue

        try:

            with open(
                filepath,
                "r",
                encoding="utf-8",
                errors="ignore"
            ) as f:

                for line in f:

                    line = line.strip()

                    if (
                        line
                        and not line.startswith("#")
                    ):
                        all_links.add(line)

        except Exception as e:

            logger.warning(
                f"⚠️ Erreur lecture {filename}: {e}"
            )

    return all_links


def normalize_server(server_url: str) -> str:

    server = server_url.strip()

    server = re.sub(
        r"^https?://",
        "",
        server,
        flags=re.IGNORECASE
    )

    server = server.rstrip("/")

    return server.lower()


def search_links_by_server(
    server_url: str
) -> List[str]:

    found_lines = []

    server_clean = normalize_server(
        server_url
    )

    logger.info(
        f"🔎 Recherche serveur : {server_clean}"
    )

    for filename in index.keys():

        filepath = os.path.join(
            DATA_FOLDER,
            filename
        )

        if not os.path.exists(filepath):
            continue

        try:

            with open(
                filepath,
                "r",
                encoding="utf-8",
                errors="ignore"
            ) as f:

                content = f.read()

            blocks = re.split(
                r"━━━━━━━━━━━━━━━━━━",
                content
            )

            for block in blocks:

                block_clean = block.strip()

                if not block_clean:
                    continue

                block_normalized = normalize_server(
                    block_clean
                )

                if (
                    server_clean in block_normalized
                    or server_clean in block_clean.lower()
                ):

                    found_lines.append(
                        block_clean
                    )

        except Exception as e:

            logger.warning(
                f"⚠️ Erreur recherche dans "
                f"{filename}: {e}"
            )

    return found_lines


def is_admin(user_id: int) -> bool:

    return user_id in ADMIN_IDS


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_handler(message):

    logger.info(
        f"📩 /start reçu de "
        f"user_id={message.from_user.id}"
    )

    welcome_text = """
🤖 Bot M3U Database

📋 Commandes :

🔍 /m3u <serveur>
Recherche des liens M3U.

📊 /stats
Affiche les statistiques.

💾 /save
Sauvegarde manuelle (admin).

📤 Envoyer un fichier .txt
Ajoute des liens (admin).

✅ Bot connecté via Telegram Webhook.
"""

    try:

        bot.reply_to(
            message,
            welcome_text
        )

        logger.info(
            "✅ Réponse /start envoyée."
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur réponse /start : {e}"
        )


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(commands=["help"])
def help_handler(message):

    logger.info(
        f"📩 /help reçu de "
        f"user_id={message.from_user.id}"
    )

    help_text = """
🤖 Aide

🔍 /m3u http://serveur.com:8080

📊 /stats

💾 /save

📤 Administrateur :
envoyer un fichier .txt
"""

    try:

        bot.reply_to(
            message,
            help_text
        )

        logger.info(
            "✅ Réponse /help envoyée."
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur réponse /help : {e}"
        )


# ============================================================
# /M3U /SEARCH
# ============================================================

@bot.message_handler(
    commands=["m3u", "search"]
)
def m3u_handler(message):

    logger.info(
        f"📩 Commande recherche reçue : "
        f"{message.text}"
    )

    try:

        text = message.text or ""

        parts = text.split(
            " ",
            1
        )

        if len(parts) < 2:

            bot.reply_to(
                message,
                "❌ Format incorrect.\n\n"
                "Utilise :\n"
                "/m3u http://serveur.com:8080"
            )

            return

        server_url = parts[1].strip()

        if not (
            server_url.startswith(
                "http://"
            )
            or server_url.startswith(
                "https://"
            )
        ):

            bot.reply_to(
                message,
                "❌ URL invalide.\n\n"
                "Exemple :\n"
                "http://serveur.com:8080"
            )

            return

        search_msg = bot.reply_to(
            message,
            f"🔍 Recherche pour :\n{server_url}"
        )

        logger.info(
            f"🔎 Recherche lancée : {server_url}"
        )

        blocks = search_links_by_server(
            server_url
        )

        logger.info(
            f"🔎 Résultats trouvés : {len(blocks)}"
        )

        if blocks:

            result_text = (
                f"✅ {len(blocks)} résultat(s) trouvé(s)\n\n"
            )

            for i, block in enumerate(
                blocks[:10],
                1
            ):

                result_text += (
                    f"[{i}]\n"
                    f"{block}\n\n"
                )

            if len(blocks) > 10:

                result_text += (
                    f"... et {len(blocks) - 10} "
                    f"autres résultats."
                )

        else:

            result_text = (
                "❌ Aucun résultat trouvé pour :\n"
                f"{server_url}"
            )

        if len(result_text) > 4000:

            result_text = (
                result_text[:3950]
                + "\n\n..."
            )

        try:

            bot.edit_message_text(
                result_text,
                search_msg.chat.id,
                search_msg.message_id
            )

        except Exception as e:

            logger.warning(
                f"⚠️ Impossible de modifier "
                f"le message de recherche : {e}"
            )

            bot.reply_to(
                message,
                result_text
            )

    except Exception as e:

        logger.exception(
            f"❌ Erreur commande M3U : {e}"
        )

        try:

            bot.reply_to(
                message,
                "❌ Une erreur est survenue "
                "pendant la recherche."
            )

        except Exception:
            pass


# ============================================================
# /STATS
# ============================================================

@bot.message_handler(commands=["stats"])
def stats_handler(message):

    logger.info(
        f"📩 /stats reçu de "
        f"user_id={message.from_user.id}"
    )

    try:

        total_files = len(index)

        total_links = len(
            get_all_links()
        )

        stats_text = (
            "📊 Statistiques\n\n"
            f"📁 Fichiers : {total_files}\n\n"
            f"🔗 Liens : {total_links}\n\n"
            "🔄 Statut : ✅ en ligne\n\n"
            "🌐 Mode : Webhook"
        )

        bot.reply_to(
            message,
            stats_text
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur /stats : {e}"
        )


# ============================================================
# /SAVE
# ============================================================

@bot.message_handler(commands=["save"])
def save_handler(message):

    logger.info(
        f"📩 /save reçu de "
        f"user_id={message.from_user.id}"
    )

    try:

        if not is_admin(
            message.from_user.id
        ):

            bot.reply_to(
                message,
                "❌ Permission refusée."
            )

            return

        save_index()

        bot.reply_to(
            message,
            "✅ Index sauvegardé localement."
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur /save : {e}"
        )

        try:

            bot.reply_to(
                message,
                f"❌ Erreur sauvegarde : {e}"
            )

        except Exception:
            pass


# ============================================================
# RÉCEPTION DES FICHIERS TXT
# ============================================================

@bot.message_handler(
    content_types=["document"]
)
def document_handler(message):

    logger.info(
        f"📥 Document reçu de "
        f"user_id={message.from_user.id}"
    )

    try:

        if not is_admin(
            message.from_user.id
        ):

            bot.reply_to(
                message,
                "❌ Permission refusée."
            )

            return

        document = message.document

        if not document:

            bot.reply_to(
                message,
                "❌ Document introuvable."
            )

            return

        if not document.file_name:

            bot.reply_to(
                message,
                "❌ Nom du fichier introuvable."
            )

            return

        if not document.file_name.lower().endswith(
            ".txt"
        ):

            bot.reply_to(
                message,
                "❌ Seuls les fichiers .txt "
                "sont acceptés."
            )

            return

        logger.info(
            f"📥 Téléchargement : "
            f"{document.file_name}"
        )

        file_info = bot.get_file(
            document.file_id
        )

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        timestamp = int(
            time.time()
        )

        filename = (
            f"{timestamp}_"
            f"{document.file_name}"
        )

        filepath = os.path.join(
            DATA_FOLDER,
            filename
        )

        with open(
            filepath,
            "wb"
        ) as f:

            f.write(
                downloaded_file
            )

        link_count = 0

        with open(
            filepath,
            "r",
            encoding="utf-8",
            errors="ignore"
        ) as f:

            for line in f:

                line = line.strip()

                if (
                    line
                    and not line.startswith("#")
                ):

                    link_count += 1

        index[filename] = {
            "original_name":
                document.file_name,

            "date_added":
                datetime.now().isoformat(),

            "links":
                link_count,

            "size":
                document.file_size
        }

        save_index()

        bot.reply_to(
            message,
            "✅ Fichier ajouté !\n\n"
            f"📁 {document.file_name}\n\n"
            f"🔗 {link_count} liens\n\n"
            "💾 Index sauvegardé."
        )

        logger.info(
            f"✅ Fichier traité : "
            f"{filename} "
            f"({link_count} liens)"
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur traitement fichier : {e}"
        )

        try:

            bot.reply_to(
                message,
                f"❌ Erreur : {e}"
            )

        except Exception:
            pass


# ============================================================
# CALLBACKS
# ============================================================

@bot.callback_query_handler(
    func=lambda call: True
)
def callback_handler(call):

    logger.info(
        f"🔘 Callback reçu : "
        f"{call.data}"
    )

    try:

        bot.answer_callback_query(
            call.id
        )

    except Exception as e:

        logger.warning(
            f"⚠️ Erreur callback : {e}"
        )


# ============================================================
# MESSAGE INCONNU
# ============================================================

@bot.message_handler(
    func=lambda message: True,
    content_types=[
        "text"
    ]
)
def unknown_message_handler(message):

    logger.info(
        f"📩 Message texte non reconnu "
        f"de user_id={message.from_user.id}: "
        f"{message.text}"
    )

    try:

        bot.reply_to(
            message,
            "🤖 Commande non reconnue.\n\n"
            "Utilise /help pour voir les commandes."
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur message inconnu : {e}"
        )


# ============================================================
# ROUTE PRINCIPALE
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return "Bot Telegram M3U OK", 200


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route(
    "/health",
    methods=["GET"]
)
def health():

    return {
        "status": "ok",
        "bot": "running",
        "mode": "webhook"
    }, 200


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

@app.route(
    "/telegram/webhook",
    methods=["POST"]
)
def telegram_webhook():

    logger.info(
        "📨 WEBHOOK TELEGRAM REÇU"
    )

    try:

        if not request.is_json:

            logger.warning(
                "⚠️ Webhook reçu mais "
                "Content-Type n'est pas JSON."
            )

            logger.warning(
                f"Content-Type : "
                f"{request.content_type}"
            )

            return "Bad Request", 400

        json_string = request.get_data(
            as_text=True
        )

        if not json_string:

            logger.warning(
                "⚠️ Webhook vide."
            )

            return "OK", 200

        logger.info(
            f"📦 Update Telegram reçu "
            f"({len(json_string)} caractères)"
        )

        try:

            data = json.loads(
                json_string
            )

        except json.JSONDecodeError as e:

            logger.exception(
                f"❌ JSON Telegram invalide : {e}"
            )

            return "Bad Request", 400

        # ----------------------------------------------------
        # Informations de diagnostic
        # ----------------------------------------------------

        update_keys = list(
            data.keys()
        )

        logger.info(
            f"📋 Type d'update : "
            f"{update_keys}"
        )

        if "message" in data:

            msg = data["message"]

            user = msg.get(
                "from",
                {}
            )

            logger.info(
                "👤 Message Telegram : "
                f"user_id={user.get('id')} "
                f"username={user.get('username')} "
                f"text={msg.get('text')}"
            )

        elif "callback_query" in data:

            callback = data[
                "callback_query"
            ]

            logger.info(
                "🔘 Callback Telegram reçu : "
                f"{callback.get('data')}"
            )

        # ----------------------------------------------------
        # Conversion Update
        # ----------------------------------------------------

        update = telebot.types.Update.de_json(
            json_string
        )

        if update is None:

            logger.warning(
                "⚠️ Impossible de créer "
                "l'objet Update Telegram."
            )

            return "OK", 200

        logger.info(
            f"⚙️ Traitement de l'update "
            f"id={getattr(update, 'update_id', 'unknown')}"
        )

        # ----------------------------------------------------
        # Envoi aux handlers
        # ----------------------------------------------------

        bot.process_new_updates(
            [update]
        )

        logger.info(
            "✅ Update Telegram traité avec succès."
        )

        return "OK", 200

    except Exception as e:

        logger.exception(
            f"❌ ERREUR CRITIQUE WEBHOOK : {e}"
        )

        # On retourne 500 afin que Telegram
        # puisse éventuellement réessayer.
        retu
