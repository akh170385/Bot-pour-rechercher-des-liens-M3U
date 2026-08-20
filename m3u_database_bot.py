import os
import re
import json
import time
import logging
from datetime import datetime
from typing import List, Set, Dict, Optional, Any

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
    """Charge l'index depuis le fichier JSON."""
    global index

    if not os.path.exists(INDEX_FILE):
        index = {}
        logger.info("📁 Aucun index local trouvé. Index vide.")
        return

    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            index = json.load(f)
        logger.info(f"📂 Index local chargé : {len(index)} fichiers")
    except Exception as e:
        logger.exception(f"❌ Impossible de charger {INDEX_FILE}: {e}")
        index = {}


def save_index():
    """Sauvegarde l'index dans le fichier JSON."""
    try:
        with open(INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
        logger.info("💾 Index sauvegardé localement.")
    except Exception as e:
        logger.exception(f"❌ Erreur sauvegarde index : {e}")


# Charger l'index au démarrage du module
load_index()


# ============================================================
# PAGINATION STATE
# ============================================================

# Structure: {
#   search_id: {
#       "chat_id": int,
#       "results": List[str],
#       "page": int,
#       "total_pages": int,
#       "main_message_id": int,
#       "extra_message_ids": List[int],
#       "timestamp": float
#   }
# }
pagination_state: Dict[str, Dict] = {}

# Nombre de résultats par page
RESULTS_PER_PAGE = 10

# Durée de vie d'un état de pagination (1 heure)
STATE_EXPIRY_SECONDS = 3600


def cleanup_expired_states():
    """Supprime les états de pagination expirés."""
    current_time = time.time()
    expired_keys = []
    
    for search_id, state in pagination_state.items():
        if current_time - state.get("timestamp", 0) > STATE_EXPIRY_SECONDS:
            expired_keys.append(search_id)
    
    for key in expired_keys:
        state = pagination_state[key]
        chat_id = state.get("chat_id")
        extra_ids = state.get("extra_message_ids", [])
        
        # Nettoyer les messages supplémentaires
        for msg_id in extra_ids:
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
        
        del pagination_state[key]
        logger.info(f"🧹 État expiré nettoyé: {key}")


def generate_search_id(chat_id: int, timestamp: int) -> str:
    """Génère un ID unique pour chaque recherche."""
    return f"{chat_id}_{timestamp}"


def get_page_results(results: List[str], page: int) -> List[str]:
    """Retourne les résultats pour une page donnée."""
    start_idx = (page - 1) * RESULTS_PER_PAGE
    end_idx = start_idx + RESULTS_PER_PAGE
    return results[start_idx:end_idx]


def get_total_pages(total_results: int) -> int:
    """Calcule le nombre total de pages."""
    if total_results == 0:
        return 1
    return (total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE


def split_text_into_chunks(text: str, max_length: int = 4000) -> List[str]:
    """
    Divise un texte en plusieurs morceaux sans couper les lignes.
    Utile pour les résultats très longs qui dépassent la limite Telegram.
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    lines = text.split('\n')
    current_chunk = ""
    
    for line in lines:
        if len(line) > max_length:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""
            for i in range(0, len(line), max_length - 100):
                chunks.append(line[i:i + max_length - 100])
            continue
        
        if len(current_chunk) + len(line) + 1 <= max_length:
            if current_chunk:
                current_chunk += '\n' + line
            else:
                current_chunk = line
        else:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = line
    
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks


def format_page_results(results: List[str], total_results: int, current_page: int, total_pages: int) -> str:
    """Formate les résultats d'une page avec les métadonnées."""
    result_text = (
        f"✅ {total_results} résultat(s) trouvé(s)\n"
        f"📄 Page {current_page} / {total_pages}\n\n"
    )
    
    for i, block in enumerate(results, 1):
        global_idx = (current_page - 1) * RESULTS_PER_PAGE + i
        result_text += f"[{global_idx}]\n{block}\n\n"
    
    return result_text


def cleanup_extra_messages(chat_id: int, extra_message_ids: List[int]) -> None:
    """Supprime les messages supplémentaires d'une page."""
    for msg_id in extra_message_ids:
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception as e:
            logger.debug(f"⚠️ Impossible de supprimer le message {msg_id}: {e}")


def send_paginated_message(chat_id: int, search_id: str, results: List[str], 
                           current_page: int, total_pages: int, 
                           reply_to_message_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Envoie un message avec pagination. Retourne un dict avec:
    - main_message_id: ID du message principal (avec les boutons)
    - extra_message_ids: Liste des IDs des messages supplémentaires
    Gère le découpage automatique si une page dépasse la limite Telegram.
    """
    page_results = get_page_results(results, current_page)
    formatted_text = format_page_results(page_results, len(results), current_page, total_pages)
    
    extra_message_ids = []
    main_message_id = None
    
    if len(formatted_text) > 4000:
        chunks = split_text_into_chunks(formatted_text, 4000)
        markup = build_pagination_markup(chat_id, search_id, current_page, total_pages)
        
        msg = bot.send_message(
            chat_id,
            chunks[0],
            reply_markup=markup,
            reply_to_message_id=reply_to_message_id if reply_to_message_id else None
        )
        main_message_id = msg.message_id
        
        for chunk in chunks[1:]:
            extra_msg = bot.send_message(chat_id, chunk)
            extra_message_ids.append(extra_msg.message_id)
    else:
        markup = build_pagination_markup(chat_id, search_id, current_page, total_pages)
        
        msg = bot.send_message(
            chat_id,
            formatted_text,
            reply_markup=markup,
            reply_to_message_id=reply_to_message_id if reply_to_message_id else None
        )
        main_message_id = msg.message_id
    
    return {
        "main_message_id": main_message_id,
        "extra_message_ids": extra_message_ids
    }


def build_pagination_markup(chat_id: int, search_id: str, current_page: int, total_pages: int):
    """Construit le clavier inline pour la pagination."""
    from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

    markup = InlineKeyboardMarkup(row_width=2)
    buttons = []

    if current_page > 1:
        buttons.append(
            InlineKeyboardButton(
                "⬅️ Précédent",
                callback_data=f"page_{search_id}_{current_page - 1}"
            )
        )
    else:
        buttons.append(
            InlineKeyboardButton(
                "⬅️ Précédent",
                callback_data="disabled"
            )
        )

    if current_page < total_pages:
        buttons.append(
            InlineKeyboardButton(
                "➡️ Suivant",
                callback_data=f"page_{search_id}_{current_page + 1}"
            )
        )
    else:
        buttons.append(
            InlineKeyboardButton(
                "➡️ Suivant",
                callback_data="disabled"
            )
        )

    markup.row(*buttons)
    
    if total_pages > 2:
        nav_buttons = []
        if current_page > 2:
            nav_buttons.append(
                InlineKeyboardButton(
                    "⏮️ Début",
                    callback_data=f"page_{search_id}_1"
                )
            )
        if current_page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    "⏭️ Fin",
                    callback_data=f"page_{search_id}_{total_pages}"
                )
            )
        if nav_buttons:
            markup.row(*nav_buttons)

    return markup


# ============================================================
# OUTILS
# ============================================================

def get_all_links() -> Set[str]:
    """Récupère tous les liens uniques de tous les fichiers."""
    all_links = set()

    for filename in index.keys():
        filepath = os.path.join(DATA_FOLDER, filename)

        if not os.path.exists(filepath):
            logger.warning(f"⚠️ Fichier manquant: {filename}")
            continue

        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        all_links.add(line)
        except Exception as e:
            logger.warning(f"⚠️ Erreur lecture {filename}: {e}")

    return all_links


def normalize_server(server_url: str) -> str:
    """Normalise l'URL du serveur pour la recherche."""
    server = server_url.strip()
    server = re.sub(r"^https?://", "", server, flags=re.IGNORECASE)
    server = server.rstrip("/")
    return server.lower()


def search_links_by_server(server_url: str) -> List[str]:
    """
    Recherche tous les liens correspondant au serveur dans tous les fichiers.
    Retourne TOUS les résultats trouvés sans aucune limite.
    """
    found_lines = []
    server_clean = normalize_server(server_url)

    logger.info(f"🔎 Recherche serveur : {server_clean}")

    for filename in index.keys():
        filepath = os.path.join(DATA_FOLDER, filename)

        if not os.path.exists(filepath):
            logger.warning(f"⚠️ Fichier manquant: {filename}")
            continue

        try:
            with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()

            blocks = re.split(r"━━━━━━━━━━━━━━━━━━", content)

            for block in blocks:
                block_clean = block.strip()
                if not block_clean:
                    continue

                block_normalized = normalize_server(block_clean)

                if server_clean in block_normalized or server_clean in block_clean.lower():
                    found_lines.append(block_clean)

        except Exception as e:
            logger.warning(f"⚠️ Erreur recherche dans {filename}: {e}")

    return found_lines


def is_admin(user_id: int) -> bool:
    """Vérifie si l'utilisateur est administrateur."""
    return user_id in ADMIN_IDS


# ============================================================
# CONFIGURATION DU WEBHOOK
# ============================================================

def setup_webhook():
    """Configure le webhook Telegram au démarrage."""
    try:
        render_url = os.environ.get("RENDER_EXTERNAL_URL")
        webhook_url = os.environ.get("WEBHOOK_URL")
        
        if webhook_url:
            url = webhook_url
        elif render_url:
            url = f"{render_url}/telegram/webhook"
        else:
            logger.warning("⚠️ Aucune URL de webhook configurée.")
            return False
        
        logger.info(f"🔗 Configuration du webhook: {url}")
        bot.set_webhook(url=url)
        logger.info("✅ Webhook configuré avec succès.")
        return True
        
    except Exception as e:
        logger.exception(f"❌ Erreur configuration webhook: {e}")
        return False


# Configurer le webhook au démarrage du module
# Cela fonctionne avec Gunicorn en production
WEBHOOK_CONFIGURED = setup_webhook()


# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_handler(message):
    logger.info(f"📩 /start reçu de user_id={message.from_user.id}")

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
        bot.reply_to(message, welcome_text)
        logger.info("✅ Réponse /start envoyée.")
    except Exception as e:
        logger.exception(f"❌ Erreur réponse /start : {e}")


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(commands=["help"])
def help_handler(message):
    logger.info(f"📩 /help reçu de user_id={message.from_user.id}")

    help_text = """
🤖 Aide

🔍 /m3u http://serveur.com:8080

📊 /stats

💾 /save

📤 Administrateur :
envoyer un fichier .txt
"""

    try:
        bot.reply_to(message, help_text)
        logger.info("✅ Réponse /help envoyée.")
    except Exception as e:
        logger.exception(f"❌ Erreur réponse /help : {e}")


# ============================================================
# /M3U /SEARCH
# ============================================================

@bot.message_handler(commands=["m3u", "search"])
def m3u_handler(message):
    logger.info(f"📩 Commande recherche reçue : {message.text}")

    try:
        text = message.text or ""
        parts = text.split(" ", 1)

        if len(parts) < 2:
            bot.reply_to(
                message,
                "❌ Format incorrect.\n\n"
                "Utilise :\n"
                "/m3u http://serveur.com:8080"
            )
            return

        server_url = parts[1].strip()

        if not (server_url.startswith("http://") or server_url.startswith("https://")):
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

        logger.info(f"🔎 Recherche lancée : {server_url}")

        # Récupérer TOUS les résultats sans aucune limite
        blocks = search_links_by_server(server_url)

        logger.info(f"🔎 Résultats trouvés : {len(blocks)}")

        chat_id = message.chat.id
        
        # Nettoyer les états expirés
        cleanup_expired_states()
        
        # Nettoyer tous les anciens états pour ce chat
        keys_to_remove = []
        for key, state in pagination_state.items():
            if state.get("chat_id") == chat_id:
                # Nettoyer les messages supplémentaires
                if "extra_message_ids" in state:
                    cleanup_extra_messages(chat_id, state["extra_message_ids"])
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            del pagination_state[key]

        if blocks:
            total_pages = get_total_pages(len(blocks))
            current_page = 1
            search_id = generate_search_id(chat_id, int(time.time()))

            result = send_paginated_message(
                chat_id,
                search_id,
                blocks,
                current_page,
                total_pages,
                search_msg.message_id
            )

            pagination_state[search_id] = {
                "chat_id": chat_id,
                "results": blocks,
                "page": current_page,
                "total_pages": total_pages,
                "main_message_id": result["main_message_id"],
                "extra_message_ids": result["extra_message_ids"],
                "timestamp": time.time()
            }

            try:
                bot.delete_message(chat_id, search_msg.message_id)
            except Exception as e:
                logger.warning(f"⚠️ Impossible de supprimer le message temporaire: {e}")

        else:
            result_text = f"❌ Aucun résultat trouvé pour :\n{server_url}"

            try:
                bot.edit_message_text(
                    result_text,
                    search_msg.chat.id,
                    search_msg.message_id
                )
            except Exception as e:
                logger.warning(f"⚠️ Impossible de modifier le message de recherche : {e}")
                bot.reply_to(message, result_text)

    except Exception as e:
        logger.exception(f"❌ Erreur commande M3U : {e}")
        try:
            bot.reply_to(
                message,
                "❌ Une erreur est survenue pendant la recherche."
            )
        except Exception:
            pass


# ============================================================
# /STATS
# ============================================================

@bot.message_handler(commands=["stats"])
def stats_handler(message):
    logger.info(f"📩 /stats reçu de user_id={message.from_user.id}")

    try:
        total_files = len(index)
        total_links = len(get_all_links())

        stats_text = (
            "📊 Statistiques\n\n"
            f"📁 Fichiers : {total_files}\n\n"
            f"🔗 Liens : {total_links}\n\n"
            "🔄 Statut : ✅ en ligne\n\n"
            "🌐 Mode : Webhook"
        )

        bot.reply_to(message, stats_text)
    except Exception as e:
        logger.exception(f"❌ Erreur /stats : {e}")


# ============================================================
# /SAVE
# ============================================================

@bot.message_handler(commands=["save"])
def save_handler(message):
    logger.info(f"📩 /save reçu de user_id={message.from_user.id}")

    try:
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "❌ Permission refusée.")
            return

        save_index()
        bot.reply_to(message, "✅ Index sauvegardé localement.")
    except Exception as e:
        logger.exception(f"❌ Erreur /save : {e}")
        try:
            bot.reply_to(message, f"❌ Erreur sauvegarde : {e}")
        except Exception:
            pass


# ============================================================
# RÉCEPTION DES FICHIERS TXT
# ============================================================

@bot.message_handler(content_types=["document"])
def document_handler(message):
    logger.info(f"📥 Document reçu de user_id={message.from_user.id}")

    try:
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "❌ Permission refusée.")
            return

        document = message.document

        if not document:
            bot.reply_to(message, "❌ Document introuvable.")
            return

        if not document.file_name:
            bot.reply_to(message, "❌ Nom du fichier introuvable.")
            return

        if not document.file_name.lower().endswith(".txt"):
            bot.reply_to(message, "❌ Seuls les fichiers .txt sont acceptés.")
            return

        logger.info(f"📥 Téléchargement : {document.file_name}")

        file_info = bot.get_file(document.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        timestamp = int(time.time())
        filename = f"{timestamp}_{document.file_name}"
        filepath = os.path.join(DATA_FOLDER, filename)

        with open(filepath, "wb") as f:
            f.write(downloaded_file)

        link_count = 0
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
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
            "✅ Fichier ajouté !\n\n"
            f"📁 {document.file_name}\n\n"
            f"🔗 {link_count} liens\n\n"
            "💾 Index sauvegardé."
        )

        logger.info(f"✅ Fichier traité : {filename} ({link_count} liens)")

    except Exception as e:
        logger.exception(f"❌ Erreur traitement fichier : {e}")
        try:
            bot.reply_to(message, f"❌ Erreur : {e}")
        except Exception:
            pass


# ============================================================
# CALLBACKS
# ============================================================

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    logger.info(f"🔘 Callback reçu : {call.data}")

    try:
        if call.data == "disabled":
            bot.answer_callback_query(call.id, "❌ Bouton désactivé")
            return

        if call.data.startswith("page_"):
            parts = call.data.split("_")
            if len(parts) == 3:
                search_id = parts[1]
                page_str = parts[2]

                try:
                    target_page = int(page_str)

                    # Nettoyer les états expirés
                    cleanup_expired_states()

                    # Récupérer l'état exact via search_id
                    state = pagination_state.get(search_id)

                    if state is None:
                        bot.answer_callback_query(
                            call.id,
                            "⚠️ Recherche expirée ou inexistante. Relancez la recherche."
                        )
                        return

                    chat_id = state["chat_id"]
                    
                    # Vérifier que le chat_id correspond
                    if chat_id != call.message.chat.id:
                        bot.answer_callback_query(
                            call.id,
                            "⚠️ Cette recherche n'est pas dans ce chat."
                        )
                        return

                    results = state["results"]
                    total_pages = state["total_pages"]

                    if target_page < 1 or target_page > total_pages:
                        bot.answer_callback_query(call.id, "❌ Page invalide")
                        return

                    # Nettoyer les anciens messages supplémentaires
                    if "extra_message_ids" in state:
                        cleanup_extra_messages(chat_id, state["extra_message_ids"])
                        state["extra_message_ids"] = []

                    state["page"] = target_page

                    page_results = get_page_results(results, target_page)
                    formatted_text = format_page_results(
                        page_results, 
                        len(results), 
                        target_page, 
                        total_pages
                    )

                    if len(formatted_text) > 4000:
                        chunks = split_text_into_chunks(formatted_text, 4000)
                        markup = build_pagination_markup(chat_id, search_id, target_page, total_pages)
                        
                        try:
                            bot.edit_message_text(
                                chunks[0],
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup
                            )
                        except Exception as e:
                            logger.warning(f"⚠️ Erreur lors de l'édition du message : {e}")
                            new_msg = bot.send_message(
                                call.message.chat.id,
                                chunks[0],
                                reply_markup=markup
                            )
                            state["main_message_id"] = new_msg.message_id
                            try:
                                bot.delete_message(call.message.chat.id, call.message.message_id)
                            except Exception:
                                pass
                        
                        extra_ids = []
                        for chunk in chunks[1:]:
                            extra_msg = bot.send_message(call.message.chat.id, chunk)
                            extra_ids.append(extra_msg.message_id)
                        state["extra_message_ids"] = extra_ids
                    else:
                        markup = build_pagination_markup(chat_id, search_id, target_page, total_pages)
                        
                        try:
                            bot.edit_message_text(
                                formatted_text,
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup
                            )
                        except Exception as e:
                            logger.warning(f"⚠️ Erreur lors de l'édition du message : {e}")
                            new_msg = bot.send_message(
                                call.message.chat.id,
                                formatted_text,
                                reply_markup=markup
                            )
                            state["main_message_id"] = new_msg.message_id
                            try:
                                bot.delete_message(call.message.chat.id, call.message.message_id)
                            except Exception:
                                pass

                    bot.answer_callback_query(call.id)

                except ValueError:
                    bot.answer_callback_query(call.id, "❌ Erreur de pagination")
            else:
                bot.answer_callback_query(call.id, "❌ Format de pagination invalide")
        else:
            bot.answer_callback_query(call.id)

    except Exception as e:
        logger.warning(f"⚠️ Erreur callback : {e}")
        try:
            bot.answer_callback_query(call.id, "❌ Erreur lors du traitement")
        except Exception:
            pass


# ============================================================
# MESSAGE INCONNU
# ============================================================

@bot.message_handler(func=lambda message: True, content_types=["text"])
def unknown_message_handler(message):
    logger.info(f"📩 Message texte non reconnu de user_id={message.from_user.id}: {message.text}")

    try:
        bot.reply_to(
            message,
            "🤖 Commande non reconnue.\n\n"
            "Utilise /help pour voir les commandes."
        )
    except Exception as e:
        logger.exception(f"❌ Erreur message inconnu : {e}")


# ============================================================
# ROUTE PRINCIPALE
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
        "mode": "webhook",
        "index_size": len(index),
        "webhook_configured": WEBHOOK_CONFIGURED
    }, 200


# ============================================================
# WEBHOOK TELEGRAM
# ============================================================

@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    logger.info("📨 WEBHOOK TELEGRAM REÇU")

    try:
        if not request.is_json:
            logger.warning(f"⚠️ Webhook reçu mais Content-Type n'est pas JSON. Content-Type: {request.content_type}")
            return "Bad Request", 400

        json_string = request.get_data(as_text=True)

        if not json_string:
            logger.warning("⚠️ Webhook vide.")
            return "OK", 200

        logger.info(f"📦 Update Telegram reçu ({len(json_string)} caractères)")

        try:
            data = json.loads(json_string)
        except json.JSONDecodeError as e:
            logger.exception(f"❌ JSON Telegram invalide : {e}")
            return "Bad Request", 400

        update_keys = list(data.keys())
        logger.info(f"📋 Type d'update : {update_keys}")

        if "message" in data:
            msg = data["message"]
            user = msg.get("from", {})
            logger.info(
                f"👤 Message Telegram : "
                f"user_id={user.get('id')} "
                f"username={user.get('username')} "
                f"text={msg.get('text')}"
            )
        elif "callback_query" in data:
            callback = data["callback_query"]
            logger.info(f"🔘 Callback Telegram reçu : {callback.get('data')}")

        update = telebot.types.Update.de_json(json_string)

        if update is None:
            logger.warning("⚠️ Impossible de créer l'objet Update Telegram.")
            return "OK", 200

        logger.info(f"⚙️ Traitement de l'update id={getattr(update, 'update_id', 'unknown')}")

        bot.process_new_updates([update])

        logger.info("✅ Update Telegram traité avec succès.")
        return "OK", 200

    except Exception as e:
        logger.exception(f"❌ ERREUR CRITIQUE WEBHOOK : {e}")
        return "Internal Server Error", 500


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    # Ce bloc n'est exécuté qu'en développement local
    # En production avec Gunicorn, ce code n'est pas exécuté
    logger.info("🚀 Bot M3U Database démarré en mode développement")
    # Le webhook est déjà configuré au niveau du module
    # Pour le développement local, on utilise le polling
    if not WEBHOOK_CONFIGURED:
        logger.warning("⚠️ Webhook non configuré, démarrage en mode polling")
        bot.polling(non_stop=True)
    else:
        logger.info("✅ Webhook configuré, démarrage du serveur Flask")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
