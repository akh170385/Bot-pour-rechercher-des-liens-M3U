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
    raise RuntimeError("BOT_TOKEN n'est pas défini dans les variables Render.")

# ADMIN_IDS doit être par exemple :
# 123456789
# ou :
# 123456789,987654321

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
        "⚠️ ADMIN_IDS n'est pas configuré correctement. "
        "Les fonctions administrateur seront refusées."
    )


# ============================================================
# STOCKAGE LOCAL PROVISOIRE
# ============================================================

DATA_FOLDER = "m3u_database"
INDEX_FILE = "m3u_index.json"

os.makedirs(DATA_FOLDER, exist_ok=True)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# BOT
# ============================================================

bot = telebot.TeleBot(
    BOT_TOKEN,
    parse_mode=None
)


# ============================================================
# DATABASE / INDEX
# ============================================================

index = {}


def load_index():
    """
    Charge l'index JSON local.
    Pour l'instant, aucun stockage externe n'est utilisé.
    """

    global index

    if not os.path.exists(INDEX_FILE):
        index = {}
        logger.info("📁 Aucun index local trouvé. Index vide.")
        return

    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            index = json.load(f)

        logger.info(
            f"📂 Index local chargé : {len(index)} fichiers"
        )

    except Exception as e:
        logger.error(
            f"❌ Impossible de charger {INDEX_FILE}: {e}"
        )
        index = {}


def save_index():
    """
    Sauvegarde provisoire uniquement dans le fichier JSON local.

    Plus tard, nous pourrons remplacer cette fonction par Supabase
    sans toucher au système Telegram/Webhook.
    """

    try:
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(
                index,
                f,
                indent=2,
                ensure_ascii=False
            )

        logger.info("💾 Index sauvegardé localement.")

    except Exception as e:
        logger.error(
            f"❌ Erreur sauvegarde index : {e}"
        )


# ============================================================
# OUTILS
# ============================================================

def get_all_links() -> Set[str]:
    """
    Retourne tous les liens trouvés dans les fichiers M3U/TXT.
    """

    all_links = set()

    for filename in index.keys():

        filepath = os.path.join(DATA_FOLDER, filename)

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

                    if line and not line.startswith("#"):
                        all_links.add(line)

        except Exception as e:

            logger.warning(
                f"⚠️ Erreur lecture {filename}: {e}"
            )

    return all_links


def search_links_by_server(server_url: str) -> List[str]:
    """
    Recherche un serveur dans tous les fichiers enregistrés.
    """

    found_lines = []

    server_clean = (
        server_url
        .replace("http://", "")
        .replace("https://", "")
        .strip()
        .rstrip("/")
    )

    for filename in index.keys():

        filepath = os.path.join(DATA_FOLDER, filename)

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

                if (
                    server_clean in block
                    or server_url in block
                ):
                    cleaned = block.strip()

                    if cleaned:
                        found_lines.append(cleaned)

        except Exception as e:

            logger.warning(
                f"⚠️ Erreur recherche dans {filename}: {e}"
            )

    return found_lines


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ============================================================
# COMMAND /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_handler(message):

    welcome_text = """
🤖 *Bot M3U Database*

📋 *Commandes :*

🔍 `/m3u <serveur>` - Recherche des liens M3U

📊 `/stats` - Statistiques

💾 `/save` - Sauvegarde manuelle

📤 Envoyer un fichier `.txt` - Ajouter des liens *(admin)*

✅ Le bot fonctionne maintenant avec Telegram Webhook.
"""

    try:

        bot.reply_to(
            message,
            welcome_text,
            parse_mode="Markdown"
        )

    except Exception as e:

        logger.error(
            f"❌ Erreur /start : {e}"
        )


# ============================================================
# COMMAND /HELP
# ============================================================

@bot.message_handler(commands=["help"])
def help_handler(message):

    help_text = """
🤖 *Aide*

🔍 `/m3u http://serveur.com:8080`

📊 `/stats`

💾 `/save`

📤 Administrateur : envoyer un fichier `.txt`
"""

    try:

        bot.reply_to(
            message,
            help_text,
            parse_mode="Markdown"
        )

    except Exception as e:

        logger.error(
            f"❌ Erreur /help : {e}"
        )


# ============================================================
# COMMAND /M3U ET /SEARCH
# ============================================================

@bot.message_handler(commands=["m3u", "search"])
def m3u_handler(message):

    try:

        text = message.text

        if not text or len(text.split()) < 2:

            bot.reply_to(
                message,
                "❌ Format : `/m3u <serveur>`\n"
                "Exemple : `/m3u http://serveur.com:8080`",
                parse_mode="Markdown"
            )

            return

        server_url = text.split(" ", 1)[1].strip()

        if not (
            server_url.startswith("http://")
            or server_url.startswith("https://")
        ):

            bot.reply_to(
                message,
                "❌ URL invalide.\n\n"
                "Exemple : `http://serveur.com:8080`",
                parse_mode="Markdown"
            )

            return

        search_msg = bot.reply_to(
            message,
            f"🔍 Recherche pour : `{server_url}`...",
            parse_mode="Markdown"
        )

        blocks = search_links_by_server(server_url)

        if blocks:

            # On limite l'affichage pour éviter de dépasser
            # les limites de longueur des messages Telegram.

            result_text = (
                f"✅ *{len(blocks)} résultat(s) trouvé(s)*\n\n"
            )

            for i, block in enumerate(blocks[:10], 1):

                result_text += (
                    f"*[{i}]*\n"
                    f"{block}\n\n"
                )

            if len(blocks) > 10:

                result_text += (
                    f"... et {len(blocks) - 10} "
                    f"autres résultats."
                )

        else:

            result_text = (
                f"❌ Aucun serveur trouvé pour : "
                f"`{server_url}`"
            )

        # Telegram limite les messages à environ 4096 caractères.
        if len(result_text) > 4000:
            result_text = result_text[:3950] + "\n\n..."

        bot.edit_message_text(
            result_text,
            search_msg.chat.id,
            search_msg.message_id,
            parse_mode="Markdown"
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur commande M3U : {e}"
        )

        try:

            bot.reply_to(
                message,
                f"❌ Erreur pendant la recherche : {e}"
            )

        except Exception:
            pass


# ============================================================
# COMMAND /STATS
# ============================================================

@bot.message_handler(commands=["stats"])
def stats_handler(message):

    try:

        total_files = len(index)
        total_links = len(get_all_links())

        stats_text = f"""
📊 *Statistiques*

📁 Fichiers : {total_files}

🔗 Liens : {total_links}

🔄 Statut : ✅ en ligne

🌐 Mode : Webhook
"""

        bot.reply_to(
            message,
            stats_text,
            parse_mode="Markdown"
        )

    except Exception as e:

        logger.error(
            f"❌ Erreur /stats : {e}"
        )


# ============================================================
# COMMAND /SAVE
# ============================================================

@bot.message_handler(commands=["save"])
def save_handler(message):

    try:

        if not is_admin(message.from_user.id):

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

        logger.error(
            f"❌ Erreur /save : {e}"
        )

        bot.reply_to(
            message,
            f"❌ Erreur sauvegarde : {e}"
        )


# ============================================================
# RÉCEPTION DES FICHIERS TXT
# ============================================================

@bot.message_handler(content_types=["document"])
def document_handler(message):

    if not is_admin(message.from_user.id):

        bot.reply_to(
            message,
            "❌ Permission refusée."
        )

        return

    document = message.document

    if not document.file_name:

        bot.reply_to(
            message,
            "❌ Nom du fichier introuvable."
        )

        return

    if not document.file_name.lower().endswith(".txt"):

        bot.reply_to(
            message,
            "❌ Seuls les fichiers `.txt` sont acceptés.",
            parse_mode="Markdown"
        )

        return

    try:

        logger.info(
            f"📥 Réception fichier : {document.file_name}"
        )

        file_info = bot.get_file(
            document.file_id
        )

        downloaded_file = bot.download_file(
            file_info.file_path
        )

        timestamp = int(time.time())

        filename = (
            f"{timestamp}_{document.file_name}"
        )

        filepath = os.path.join(
            DATA_FOLDER,
            filename
        )

        with open(filepath, "wb") as f:
            f.write(downloaded_file)

        link_count = 0

        with open(
            filepath,
            "r",
            encoding="utf-8",
            errors="ignore"
        ) as f:

            for line in f:

                line = line.strip()

                if line and not line.startswith("#"):
                    link_count += 1

        index[filename] = {
            "original_name": document.file_name,
            "date_added": datetime.now().isoformat(),
            "links": link_count,
            "size": document.file_size
        }

        save_index()

        bot.reply_to(
            message,
            f"""
✅ *Fichier ajouté !*

📁 {document.file_name}

🔗 {link_count} liens

💾 Index sauvegardé.
""",
            parse_mode="Markdown"
        )

        logger.info(
            f"✅ Fichier traité : {filename} "
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

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):

    try:

        bot.answer_callback_query(
            call.id
        )

    except Exception as e:

        logger.warning(
            f"⚠️ Erreur callback : {e}"
        )


# ============================================================
# ROUTE DE TEST
# ============================================================

@app.route("/", methods=["GET"])
def home():

    return "Bot Telegram M3U OK", 200


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health", methods=["GET"])
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

    try:

        # Telegram envoie normalement
        # application/json.

        if not request.is_json:

            logger.warning(
                "⚠️ Requête webhook non JSON reçue."
            )

            return "Bad Request", 400

        json_string = request.get_data(
            as_text=True
        )

        update = telebot.types.Update.de_json(
            json_string
        )

        if update is None:

            logger.warning(
                "⚠️ Update Telegram vide."
            )

            return "OK", 200

        # Envoie l'update aux handlers
        # pyTelegramBotAPI.

        bot.process_new_updates(
            [update]
        )

        return "OK", 200

    except Exception as e:

        logger.exception(
            f"❌ Erreur traitement webhook : {e}"
        )

        # On renvoie 200 pour éviter que Telegram
        # réessaie indéfiniment une mise à jour
        # déjà reçue.

        return "OK", 200


# ============================================================
# CONFIGURATION AUTOMATIQUE DU WEBHOOK
# ============================================================

def configure_webhook():

    """
    Configure automatiquement le webhook Telegram.

    Render fournit normalement RENDER_EXTERNAL_URL.
    On peut également définir WEBHOOK_URL manuellement.
    """

    webhook_base_url = (
        os.environ.get("WEBHOOK_URL")
        or os.environ.get("RENDER_EXTERNAL_URL")
    )

    if not webhook_base_url:

        logger.warning(
            "⚠️ WEBHOOK_URL / RENDER_EXTERNAL_URL "
            "non disponible."
        )

        return False

    webhook_base_url = webhook_base_url.rstrip("/")

    webhook_url = (
        f"{webhook_base_url}"
        f"/telegram/webhook"
    )

    try:

        # On supprime l'ancien webhook
        # avant de configurer le nouveau.

        logger.info(
            "🧹 Suppression de l'ancien webhook..."
        )

        bot.delete_webhook(
            drop_pending_updates=False
        )

        time.sleep(1)

        logger.info(
            f"🌐 Configuration webhook : {webhook_url}"
        )

        result = bot.set_webhook(
            url=webhook_url
        )

        if result:

            logger.info(
                "✅ Webhook Telegram configuré avec succès."
            )

        else:

            logger.error(
                "❌ Telegram a refusé la configuration du webhook."
            )

        return result

    except Exception as e:

        logger.exception(
            f"❌ Erreur configuration webhook : {e}"
        )

        return False


# ============================================================
# INITIALISATION
# ============================================================

logger.info("============================================")
logger.info("🚀 INITIALISATION DU BOT")
logger.info("============================================")

load_index()

logger.info(
    f"📊 {len(index)} fichier(s) dans l'index."
)

try:

    me = bot.get_me()

    logger.info(
        f"✅ Bot Telegram connecté : "
        f"@{me.username}"
    )

except Exception as e:

    logger.error(
        f"❌ Impossible de contacter Telegram : {e}"
    )


# ============================================================
# WEBHOOK
# ============================================================

configure_webhook()


logger.info("============================================")
logger.info("✅ BOT PRÊT")
logger.info("🌐 MODE : WEBHOOK")
logger.info("🚫 POLLING : DÉSACTIVÉ")
logger.info("============================================")


# ============================================================
# LANCEMENT LOCAL UNIQUEMENT
# ============================================================

if __name__ == "__main__":

    port = int(
        os.environ.get("PORT", 10000)
    )

    logger.info(
        f"🌐 Serveur Flask démarrage sur "
        f"0.0.0.0:{port}"
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
