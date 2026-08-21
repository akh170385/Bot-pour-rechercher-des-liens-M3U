import os
import re
import json
import time
import logging
from datetime import datetime
from typing import List, Set, Dict, Optional, Any

import telebot
from flask import Flask, request
from supabase import create_client, Client


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
# SUPABASE CONFIGURATION
# ============================================================

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    logger.warning(
        "⚠️ SUPABASE_URL ou SUPABASE_KEY non définis. "
        "Le bot fonctionnera sans stockage persistant."
    )
    supabase = None
else:
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("✅ Connexion à Supabase établie.")
    except Exception as e:
        logger.exception(f"❌ Erreur de connexion à Supabase: {e}")
        supabase = None


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
# SUPABASE OPERATIONS
# ============================================================

def get_all_files_from_supabase() -> List[Dict]:
    """Récupère tous les fichiers depuis Supabase."""
    if not supabase:
        return []

    try:
        response = supabase.table("m3u_files").select("*").execute()
        return response.data
    except Exception as e:
        logger.exception(f"❌ Erreur récupération fichiers Supabase: {e}")
        return []


def get_file_from_supabase(filename: str) -> Optional[Dict]:
    """Récupère un fichier spécifique depuis Supabase."""
    if not supabase:
        return None

    try:
        response = (
            supabase
            .table("m3u_files")
            .select("*")
            .eq("filename", filename)
            .execute()
        )

        if response.data:
            return response.data[0]

        return None

    except Exception as e:
        logger.exception(
            f"❌ Erreur récupération fichier {filename}: {e}"
        )
        return None


def save_file_to_supabase(
    filename: str,
    original_name: str,
    file_content: str,
    links_count: int,
    file_size: int
) -> bool:
    """Sauvegarde un fichier dans Supabase."""
    if not supabase:
        return False

    try:
        data = {
            "filename": filename,
            "original_name": original_name,
            "file_content": file_content,
            "date_added": datetime.now().isoformat(),
            "links_count": links_count,
            "file_size": file_size
        }

        supabase.table("m3u_files").insert(data).execute()
        return True

    except Exception as e:
        logger.exception(
            f"❌ Erreur sauvegarde fichier {filename}: {e}"
        )
        return False


def delete_file_from_supabase(filename: str) -> bool:
    """Supprime un fichier de Supabase."""
    if not supabase:
        return False

    try:
        supabase.table("m3u_files").delete().eq(
            "filename", filename
        ).execute()

        return True

    except Exception as e:
        logger.exception(
            f"❌ Erreur suppression fichier {filename}: {e}"
        )
        return False


def get_all_links_from_supabase() -> Set[str]:
    """Récupère tous les liens uniques de tous les fichiers dans Supabase."""
    if not supabase:
        return set()

    all_links = set()
    files = get_all_files_from_supabase()

    for file_data in files:
        file_content = file_data.get("file_content", "")

        if not file_content:
            continue

        for line in file_content.split("\n"):
            line = line.strip()

            if line and not line.startswith("#"):
                all_links.add(line)

    return all_links


def search_links_in_supabase(server_url: str) -> List[str]:
    """
    Recherche tous les liens correspondant au serveur
    dans tous les fichiers Supabase.

    Retourne TOUS les résultats trouvés sans aucune limite.
    """
    if not supabase:
        return []

    found_lines = []
    server_clean = normalize_server(server_url)

    logger.info(
        f"🔎 Recherche serveur dans Supabase: {server_clean}"
    )

    files = get_all_files_from_supabase()

    for file_data in files:
        file_content = file_data.get("file_content", "")

        if not file_content:
            continue

        try:
            blocks = re.split(
                r"━━━━━━━━━━━━━━━━━━",
                file_content
            )

            for block in blocks:
                block_clean = block.strip()

                if not block_clean:
                    continue

                block_normalized = normalize_server(block_clean)

                if (
                    server_clean in block_normalized
                    or server_clean in block_clean.lower()
                ):
                    found_lines.append(block_clean)

        except Exception as e:
            logger.warning(
                f"⚠️ Erreur recherche dans fichier "
                f"{file_data.get('filename')}: {e}"
            )

    return found_lines


# ============================================================
# AUTHENTIFICATION SYSTEM
# ============================================================

BOT_ACCESS_PASSWORD = os.environ.get("BOT_ACCESS_PASSWORD")

if not BOT_ACCESS_PASSWORD:
    logger.warning(
        "⚠️ BOT_ACCESS_PASSWORD n'est pas défini. "
        "L'authentification privée ne fonctionnera pas."
    )


# Cache d'authentification avec timestamp
# Structure:
# {
#     user_id: {
#         "authenticated": bool,
#         "timestamp": float
#     }
# }

auth_cache: Dict[int, Dict] = {}

AUTH_CACHE_EXPIRY_SECONDS = 300


def get_authenticated_users_from_supabase() -> List[Dict]:
    """Récupère tous les utilisateurs authentifiés depuis Supabase."""
    if not supabase:
        return []

    try:
        response = (
            supabase
            .table("authenticated_users")
            .select("*")
            .execute()
        )

        return response.data

    except Exception as e:
        logger.exception(
            f"❌ Erreur récupération utilisateurs authentifiés: {e}"
        )
        return []


def save_authenticated_user_to_supabase(
    user_id: int,
    username: str = None,
    first_name: str = None,
    last_name: str = None
) -> bool:
    """Sauvegarde ou met à jour un utilisateur authentifié."""
    if not supabase:
        return False

    try:
        now = datetime.now().isoformat()

        data = {
            "user_id": user_id,
            "username": username,
            "first_name": first_name,
            "last_name": last_name,
            "authenticated_at": now,
            "last_activity": now
        }

        supabase.table("authenticated_users").upsert(
            data,
            on_conflict="user_id"
        ).execute()

        return True

    except Exception as e:
        logger.exception(
            f"❌ Erreur sauvegarde utilisateur authentifié "
            f"{user_id}: {e}"
        )
        return False


def remove_authenticated_user_from_supabase(user_id: int) -> bool:
    """Supprime un utilisateur authentifié de Supabase."""
    if not supabase:
        return False

    try:
        supabase.table("authenticated_users").delete().eq(
            "user_id", user_id
        ).execute()

        return True

    except Exception as e:
        logger.exception(
            f"❌ Erreur suppression utilisateur authentifié "
            f"{user_id}: {e}"
        )
        return False


def load_authenticated_users_to_cache():
    """Charge les utilisateurs authentifiés depuis Supabase."""
    users = get_authenticated_users_from_supabase()
    current_time = time.time()

    for user in users:
        user_id = user.get("user_id")

        if user_id:
            auth_cache[user_id] = {
                "authenticated": True,
                "timestamp": current_time
            }

    logger.info(
        f"👥 {len(users)} utilisateurs authentifiés "
        f"chargés depuis Supabase"
    )


def is_user_authenticated(user_id: int) -> bool:
    """Vérifie si un utilisateur est authentifié."""

    if user_id in auth_cache:
        cache_entry = auth_cache[user_id]

        if (
            time.time()
            - cache_entry.get("timestamp", 0)
            < AUTH_CACHE_EXPIRY_SECONDS
        ):
            return cache_entry.get("authenticated", False)

    if not supabase:
        return False

    try:
        response = (
            supabase
            .table("authenticated_users")
            .select("*")
            .eq("user_id", user_id)
            .execute()
        )

        is_auth = len(response.data) > 0

        auth_cache[user_id] = {
            "authenticated": is_auth,
            "timestamp": time.time()
        }

        return is_auth

    except Exception as e:
        logger.exception(
            f"❌ Erreur vérification authentification "
            f"{user_id}: {e}"
        )
        return False


def authenticate_user(
    user_id: int,
    password: str,
    username: str = None,
    first_name: str = None,
    last_name: str = None
) -> bool:
    """Authentifie un utilisateur avec le mot de passe."""

    if not BOT_ACCESS_PASSWORD:
        return False

    if password != BOT_ACCESS_PASSWORD:
        return False

    success = save_authenticated_user_to_supabase(
        user_id,
        username,
        first_name,
        last_name
    )

    if success:
        auth_cache[user_id] = {
            "authenticated": True,
            "timestamp": time.time()
        }

        return True

    return False


def logout_user(user_id: int) -> bool:
    """Déconnecte un utilisateur."""

    success = remove_authenticated_user_from_supabase(user_id)

    if success:
        if user_id in auth_cache:
            del auth_cache[user_id]

        return True

    return False


def require_auth(func):
    """Décorateur pour protéger les fonctions privées."""

    def wrapper(message, *args, **kwargs):
        user_id = message.from_user.id
        chat_type = message.chat.type

        if chat_type == "private":

            if not is_user_authenticated(user_id):
                bot.reply_to(
                    message,
                    "🔐 Accès protégé.\n\n"
                    "Veuillez vous authentifier en envoyant "
                    "le mot de passe en message privé."
                )
                return

        elif chat_type in ["group", "supergroup"]:

            bot.reply_to(
                message,
                "🔐 Cette commande est réservée aux "
                "utilisateurs authentifiés.\n\n"
                "Veuillez utiliser cette commande en "
                "conversation privée avec le bot."
            )
            return

        return func(message, *args, **kwargs)

    return wrapper


# Charger les utilisateurs authentifiés au démarrage
load_authenticated_users_to_cache()


# ============================================================
# PAGINATION STATE
# ============================================================

# Structure:
# {
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

RESULTS_PER_PAGE = 10

STATE_EXPIRY_SECONDS = 3600


def cleanup_expired_states():
    """Supprime les états de pagination expirés."""

    current_time = time.time()
    expired_keys = []

    for search_id, state in pagination_state.items():

        if (
            current_time
            - state.get("timestamp", 0)
            > STATE_EXPIRY_SECONDS
        ):
            expired_keys.append(search_id)

    for key in expired_keys:

        state = pagination_state[key]
        chat_id = state.get("chat_id")
        extra_ids = state.get(
            "extra_message_ids",
            []
        )

        for msg_id in extra_ids:
            try:
                bot.delete_message(
                    chat_id,
                    msg_id
                )
            except Exception:
                pass

        del pagination_state[key]

        logger.info(
            f"🧹 État expiré nettoyé: {key}"
        )


def generate_search_id(
    chat_id: int,
    timestamp: int
) -> str:
    """
    Génère un ID unique pour chaque recherche.

    Le caractère | est utilisé comme séparateur
    afin d'éviter les conflits avec le parsing
    des callback_data.
    """
    return f"{chat_id}|{timestamp}"


def get_page_results(
    results: List[str],
    page: int
) -> List[str]:
    """Retourne les résultats pour une page donnée."""

    start_idx = (
        (page - 1)
        * RESULTS_PER_PAGE
    )

    end_idx = (
        start_idx
        + RESULTS_PER_PAGE
    )

    return results[start_idx:end_idx]


def get_total_pages(
    total_results: int
) -> int:
    """Calcule le nombre total de pages."""

    if total_results == 0:
        return 1

    return (
        total_results
        + RESULTS_PER_PAGE
        - 1
    ) // RESULTS_PER_PAGE


def split_text_into_chunks(
    text: str,
    max_length: int = 4000
) -> List[str]:
    """
    Divise un texte en plusieurs morceaux
    sans couper les lignes.
    """

    if len(text) <= max_length:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = ""

    for line in lines:

        if len(line) > max_length:

            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""

            for i in range(
                0,
                len(line),
                max_length - 100
            ):
                chunks.append(
                    line[
                        i:i + max_length - 100
                    ]
                )

            continue

        if (
            len(current_chunk)
            + len(line)
            + 1
            <= max_length
        ):

            if current_chunk:
                current_chunk += "\n" + line
            else:
                current_chunk = line

        else:

            if current_chunk:
                chunks.append(current_chunk)

            current_chunk = line

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def format_page_results(
    results: List[str],
    total_results: int,
    current_page: int,
    total_pages: int
) -> str:
    """Formate les résultats d'une page."""

    result_text = (
        f"✅ {total_results} résultat(s) trouvé(s)\n"
        f"📄 Page {current_page} / {total_pages}\n\n"
    )

    for i, block in enumerate(results, 1):

        global_idx = (
            (current_page - 1)
            * RESULTS_PER_PAGE
            + i
        )

        result_text += (
            f"[{global_idx}]\n"
            f"{block}\n\n"
        )

    return result_text


def cleanup_extra_messages(
    chat_id: int,
    extra_message_ids: List[int]
) -> None:
    """Supprime les messages supplémentaires d'une page."""

    for msg_id in extra_message_ids:

        try:
            bot.delete_message(
                chat_id,
                msg_id
            )

        except Exception as e:
            logger.debug(
                f"⚠️ Impossible de supprimer "
                f"le message {msg_id}: {e}"
            )


def send_paginated_message(
    chat_id: int,
    search_id: str,
    results: List[str],
    current_page: int,
    total_pages: int,
    reply_to_message_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Envoie un message avec pagination.

    Retourne:
    - main_message_id
    - extra_message_ids
    """

    page_results = get_page_results(
        results,
        current_page
    )

    formatted_text = format_page_results(
        page_results,
        len(results),
        current_page,
        total_pages
    )

    extra_message_ids = []
    main_message_id = None

    markup = build_pagination_markup(
        chat_id,
        search_id,
        current_page,
        total_pages
    )

    if len(formatted_text) > 4000:

        chunks = split_text_into_chunks(
            formatted_text,
            4000
        )

        msg = bot.send_message(
            chat_id,
            chunks[0],
            reply_markup=markup,
            reply_to_message_id=(
                reply_to_message_id
                if reply_to_message_id
                else None
            )
        )

        main_message_id = msg.message_id

        for chunk in chunks[1:]:

            extra_msg = bot.send_message(
                chat_id,
                chunk
            )

            extra_message_ids.append(
                extra_msg.message_id
            )

    else:

        msg = bot.send_message(
            chat_id,
            formatted_text,
            reply_markup=markup,
            reply_to_message_id=(
                reply_to_message_id
                if reply_to_message_id
                else None
            )
        )

        main_message_id = msg.message_id

    return {
        "main_message_id": main_message_id,
        "extra_message_ids": extra_message_ids
    }


def build_pagination_markup(
    chat_id: int,
    search_id: str,
    current_page: int,
    total_pages: int
):
    """Construit le clavier inline pour la pagination."""

    from telebot.types import (
        InlineKeyboardMarkup,
        InlineKeyboardButton
    )

    markup = InlineKeyboardMarkup(
        row_width=2
    )

    buttons = []

    if current_page > 1:

        buttons.append(
            InlineKeyboardButton(
                "⬅️ Précédent",
                callback_data=(
                    f"page_{search_id}_"
                    f"{current_page - 1}"
                )
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
                callback_data=(
                    f"page_{search_id}_"
                    f"{current_page + 1}"
                )
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
                    callback_data=(
                        f"page_{search_id}_1"
                    )
                )
            )

        if current_page < total_pages - 1:

            nav_buttons.append(
                InlineKeyboardButton(
                    "⏭️ Fin",
                    callback_data=(
                        f"page_{search_id}_"
                        f"{total_pages}"
                    )
                )
            )

        if nav_buttons:
            markup.row(*nav_buttons)

    return markup


# ============================================================
# OUTILS
# ============================================================

def normalize_server(
    server_url: str
) -> str:
    """Normalise l'URL du serveur pour la recherche."""

    server = server_url.strip()

    server = re.sub(
        r"^https?://",
        "",
        server,
        flags=re.IGNORECASE
    )

    server = server.rstrip("/")

    return server.lower()


def is_admin(user_id: int) -> bool:
    """Vérifie si l'utilisateur est administrateur."""
    return user_id in ADMIN_IDS


# ============================================================
# CONFIGURATION DU WEBHOOK
# ============================================================

def configure_webhook_with_retry(
    max_retries: int = 3,
    delay: int = 2
) -> bool:
    """
    Configure le webhook Telegram avec tentatives
    et vérification robuste.
    """

    for attempt in range(max_retries):

        try:

            render_url = os.environ.get(
                "RENDER_EXTERNAL_URL"
            )

            webhook_url = os.environ.get(
                "WEBHOOK_URL"
            )

            logger.info(
                "🔍 RENDER_EXTERNAL_URL: "
                f"{render_url if render_url else 'NON DÉFINI'}"
            )

            logger.info(
                "🔍 WEBHOOK_URL: "
                f"{webhook_url if webhook_url else 'NON DÉFINI'}"
            )

            if webhook_url:

                base_url = webhook_url

                if not base_url.endswith(
                    "/telegram/webhook"
                ):

                    if base_url.endswith("/"):
                        base_url += "telegram/webhook"
                    else:
                        base_url += "/telegram/webhook"

                url = base_url

            elif render_url:

                if render_url.endswith("/"):
                    url = (
                        f"{render_url}"
                        f"telegram/webhook"
                    )
                else:
                    url = (
                        f"{render_url}"
                        f"/telegram/webhook"
                    )

            else:

                logger.error(
                    "❌ Aucune URL de webhook configurée "
                    "(RENDER_EXTERNAL_URL ou WEBHOOK_URL)"
                )

                if attempt < max_retries - 1:
                    logger.info(
                        f"⏳ Nouvelle tentative "
                        f"dans {delay} secondes..."
                    )
                    time.sleep(delay)

                continue

            logger.info(
                f"🔗 Tentative {attempt + 1}/"
                f"{max_retries}: "
                f"Configuration du webhook sur {url}"
            )

            # Supprimer l'ancien webhook
            try:

                bot.delete_webhook()

                logger.info(
                    "🔗 Ancien webhook supprimé"
                )

            except Exception as e:

                logger.warning(
                    "⚠️ Erreur lors de la suppression "
                    f"de l'ancien webhook: {e}"
                )

            # Configurer le nouveau webhook
            bot.set_webhook(url=url)

            # Court délai pour la propagation
            time.sleep(1)

            # Vérification
            webhook_info = bot.get_webhook_info()

            logger.info(
                f"📡 Webhook configuré sur: "
                f"{webhook_info.url}"
            )

            logger.info(
                f"📡 Updates en attente: "
                f"{webhook_info.pending_update_count}"
            )

            logger.info(
                f"📡 Dernière erreur date: "
                f"{webhook_info.last_error_date}"
            )

            logger.info(
                f"📡 Dernière erreur message: "
                f"{webhook_info.last_error_message}"
            )

            # Vérification robuste
            if webhook_info.url == url:

                logger.info(
                    f"✅ Webhook configuré sur "
                    f"la bonne URL: {url}"
                )

                if webhook_info.pending_update_count > 0:

                    logger.warning(
                        "⚠️ "
                        f"{webhook_info.pending_update_count} "
                        "updates en attente"
                    )

                if webhook_info.last_error_message:

                    logger.error(
                        "❌ Erreur webhook détectée: "
                        f"{webhook_info.last_error_message}"
                    )

                    logger.error(
                        "📅 Date de l'erreur: "
                        f"{webhook_info.last_error_date}"
                    )

                    # Telegram fournit last_error_date
                    # comme timestamp Unix.
                    if (
                        webhook_info.last_error_date
                        and
                        time.time()
                        - webhook_info.last_error_date
                        < 300
                    ):

                        logger.error(
                            "⚠️ Erreur récente "
                            "(< 5 min) - "
                            "Le webhook peut ne pas fonctionner"
                        )

                        return False

                    else:

                        logger.info(
                            "ℹ️ Erreur ancienne, "
                            "le webhook peut s'être rétabli"
                        )

                        return True

                else:

                    logger.info(
                        "✅ Aucune erreur détectée, "
                        "webhook fonctionnel"
                    )

                    return True

            else:

                logger.error(
                    f"❌ Webhook configuré sur "
                    f"{webhook_info.url} "
                    f"au lieu de {url}"
                )

        except Exception as e:

            logger.exception(
                "❌ Erreur configuration webhook "
                f"(tentative {attempt + 1}): {e}"
            )

        if attempt < max_retries - 1:

            logger.info(
                f"⏳ Nouvelle tentative "
                f"dans {delay} secondes..."
            )

            time.sleep(delay)

    logger.error(
        "❌ Échec de la configuration du webhook "
        f"après {max_retries} tentatives"
    )

    return False


# Configurer le webhook au démarrage du module.
# Avec Gunicorn --workers=1, un seul processus effectue
# cette configuration.

WEBHOOK_CONFIGURED = configure_webhook_with_retry(
    max_retries=3,
    delay=2
)

if WEBHOOK_CONFIGURED:

    logger.info(
        "✅ Webhook configuré avec succès "
        "au démarrage"
    )

else:

    logger.error(
        "❌ WEBHOOK NON CONFIGURÉ - "
        "Le bot ne recevra pas les messages!"
    )

    logger.error(
        "⚠️ Vérifiez les variables d'environnement: "
        "RENDER_EXTERNAL_URL ou WEBHOOK_URL"
    )

# ============================================================
# /START
# ============================================================

@bot.message_handler(commands=["start"])
def start_handler(message):
    logger.info("📩 16. /start handler ENTRÉ")
    logger.info(
        f"📩 17. /start reçu de user_id="
        f"{message.from_user.id}"
    )

    welcome_text = """
🤖 Bot M3U Database

📋 Commandes publiques :

🔍 /m3u <serveur>
Recherche des liens M3U (accessible à tous)

📋 Commandes privées (authentification requise) :

📊 /stats
Affiche les statistiques.

💾 /save
Sauvegarde manuelle (admin).

📤 Envoyer un fichier .txt
Ajoute des liens (admin).

🔐 Pour accéder aux fonctions privées, envoyez le mot de passe en message privé.
"""

    try:

        logger.info(
            "📤 18. Tentative d'envoi de la réponse /start"
        )

        bot.reply_to(
            message,
            welcome_text
        )

        logger.info(
            "✅ 19. Réponse /start envoyée avec succès."
        )

    except Exception as e:

        logger.exception(
            f"❌ 20. Erreur réponse /start : {e}"
        )


# ============================================================
# /HELP
# ============================================================

@bot.message_handler(commands=["help"])
def help_handler(message):
    logger.info(
        f"📩 /help reçu de user_id="
        f"{message.from_user.id}"
    )

    help_text = """
🤖 Aide

🔍 /m3u http://serveur.com:8080

📊 /stats (authentification requise)

💾 /save (authentification requise)

📤 Administrateur :
envoyer un fichier .txt (authentification requise)
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
# /M3U /SEARCH - TOTALEMENT LIBRE
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

        if not (
            server_url.startswith("http://")
            or
            server_url.startswith("https://")
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

        blocks = search_links_in_supabase(
            server_url
        )

        logger.info(
            f"🔎 Résultats trouvés : {len(blocks)}"
        )

        chat_id = message.chat.id

        cleanup_expired_states()

        keys_to_remove = []

        for key, state in pagination_state.items():

            if state.get("chat_id") == chat_id:

                if "extra_message_ids" in state:

                    cleanup_extra_messages(
                        chat_id,
                        state["extra_message_ids"]
                    )

                keys_to_remove.append(key)

        for key in keys_to_remove:
            del pagination_state[key]

        if blocks:

            total_pages = get_total_pages(
                len(blocks)
            )

            current_page = 1

            search_id = generate_search_id(
                chat_id,
                int(time.time())
            )

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
                "main_message_id": result[
                    "main_message_id"
                ],
                "extra_message_ids": result[
                    "extra_message_ids"
                ],
                "timestamp": time.time()
            }

            try:

                bot.delete_message(
                    chat_id,
                    search_msg.message_id
                )

            except Exception as e:

                logger.warning(
                    "⚠️ Impossible de supprimer "
                    f"le message temporaire: {e}"
                )

        else:

            result_text = (
                "❌ Aucun résultat trouvé pour :\n"
                f"{server_url}"
            )

            try:

                bot.edit_message_text(
                    result_text,
                    search_msg.chat.id,
                    search_msg.message_id
                )

            except Exception as e:

                logger.warning(
                    "⚠️ Impossible de modifier "
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
# /STATS - PROTÉGÉ PAR AUTHENTIFICATION
# ============================================================

@bot.message_handler(commands=["stats"])
def stats_handler(message):

    logger.info(
        f"📩 /stats reçu de user_id="
        f"{message.from_user.id}"
    )

    user_id = message.from_user.id
    chat_type = message.chat.type

    if chat_type == "private":

        if not is_user_authenticated(user_id):

            bot.reply_to(
                message,
                "🔐 Accès protégé.\n\n"
                "Veuillez vous authentifier en envoyant "
                "le mot de passe en message privé."
            )

            return

    else:

        bot.reply_to(
            message,
            "🔐 Cette commande est réservée aux "
            "utilisateurs authentifiés.\n\n"
            "Veuillez utiliser cette commande en "
            "conversation privée avec le bot."
        )

        return

    try:

        files = get_all_files_from_supabase()

        total_files = len(files)

        total_links = len(
            get_all_links_from_supabase()
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
# /SAVE - PROTÉGÉ PAR AUTHENTIFICATION ET ADMIN_IDS
# ============================================================

@bot.message_handler(commands=["save"])
def save_handler(message):

    logger.info(
        f"📩 /save reçu de user_id="
        f"{message.from_user.id}"
    )

    user_id = message.from_user.id
    chat_type = message.chat.type

    if chat_type == "private":

        if not is_user_authenticated(user_id):

            bot.reply_to(
                message,
                "🔐 Accès protégé.\n\n"
                "Veuillez vous authentifier en envoyant "
                "le mot de passe en message privé."
            )

            return

    else:

        bot.reply_to(
            message,
            "🔐 Cette commande est réservée aux "
            "utilisateurs authentifiés.\n\n"
            "Veuillez utiliser cette commande en "
            "conversation privée avec le bot."
        )

        return

    try:

        if not is_admin(user_id):

            bot.reply_to(
                message,
                "❌ Permission refusée. "
                "Vous n'êtes pas administrateur."
            )

            return

        if supabase:

            bot.reply_to(
                message,
                "✅ Les données sont déjà stockées "
                "dans Supabase."
            )

        else:

            bot.reply_to(
                message,
                "❌ Supabase n'est pas configuré."
            )

    except Exception as e:

        logger.exception(
            f"❌ Erreur /save : {e}"
        )

        try:

            bot.reply_to(
                message,
                f"❌ Erreur : {e}"
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
        f"📥 Document reçu de user_id="
        f"{message.from_user.id}"
    )

    user_id = message.from_user.id
    chat_type = message.chat.type

    if chat_type == "private":

        if not is_user_authenticated(user_id):

            bot.reply_to(
                message,
                "🔐 Accès protégé.\n\n"
                "Veuillez vous authentifier en envoyant "
                "le mot de passe en message privé."
            )

            return

    else:

        bot.reply_to(
            message,
            "🔐 Cette fonction est réservée aux "
            "utilisateurs authentifiés.\n\n"
            "Veuillez utiliser cette fonction en "
            "conversation privée avec le bot."
        )

        return

    try:

        if not is_admin(user_id):

            bot.reply_to(
                message,
                "❌ Permission refusée. "
                "Vous n'êtes pas administrateur."
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

        if not supabase:

            bot.reply_to(
                message,
                "❌ Supabase n'est pas configuré. "
                "Impossible de sauvegarder le fichier."
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

        timestamp = int(time.time())

        filename = (
            f"{timestamp}_"
            f"{document.file_name}"
        )

        file_content = downloaded_file.decode(
            "utf-8",
            errors="ignore"
        )

        link_count = 0

        for line in file_content.split("\n"):

            line = line.strip()

            if (
                line
                and not line.startswith("#")
            ):
                link_count += 1

        success = save_file_to_supabase(
            filename=filename,
            original_name=document.file_name,
            file_content=file_content,
            links_count=link_count,
            file_size=document.file_size
        )

        if success:

            bot.reply_to(
                message,
                "✅ Fichier ajouté dans Supabase !\n\n"
                f"📁 {document.file_name}\n\n"
                f"🔗 {link_count} liens\n\n"
                "💾 Stockage cloud activé."
            )

            logger.info(
                f"✅ Fichier sauvegardé dans Supabase : "
                f"{filename} ({link_count} liens)"
            )

        else:

            bot.reply_to(
                message,
                "❌ Erreur lors de la sauvegarde "
                "dans Supabase."
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
# MESSAGES PRIVÉS POUR AUTHENTIFICATION
# ============================================================

@bot.message_handler(
    func=lambda message:
        message.chat.type == "private"
        and message.text
        and not message.text.startswith("/")
)
def private_message_handler(message):
    """Gère les messages privés pour l'authentification."""

    logger.info(
        "🔐 21. private_message_handler ENTRÉ"
    )

    user_id = message.from_user.id
    text = message.text or ""

    logger.info(
        f"📩 22. Message privé reçu "
        f"de user_id={user_id}"
    )

    if not BOT_ACCESS_PASSWORD:

        bot.reply_to(
            message,
            "❌ Le système d'authentification "
            "n'est pas configuré."
        )

        return

    if is_user_authenticated(user_id):

        bot.reply_to(
            message,
            "✅ Vous êtes déjà authentifié.\n\n"
            "🔓 Accès privé autorisé."
        )

        return

    if text == BOT_ACCESS_PASSWORD:

        success = authenticate_user(
            user_id,
            text,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name
        )

        if success:

            bot.reply_to(
                message,
                "✅ Authentification réussie.\n\n"
                "🔓 Accès privé autorisé.\n\n"
                "Vous pouvez maintenant utiliser "
                "les commandes privées :\n"
                "/stats\n"
                "/save\n"
                "Envoi de fichiers .txt"
            )

            logger.info(
                f"✅ Utilisateur authentifié: "
                f"user_id={user_id}"
            )

        else:

            bot.reply_to(
                message,
                "❌ Erreur lors de l'authentification. "
                "Veuillez réessayer."
            )

    else:

        bot.reply_to(
            message,
            "❌ Mot de passe incorrect.\n\n"
            "Veuillez réessayer."
        )

        logger.warning(
            f"⚠️ Tentative d'authentification échouée: "
            f"user_id={user_id}"
        )


# ============================================================
# CALLBACKS
# ============================================================

@bot.callback_query_handler(
    func=lambda call: True
)
def callback_handler(call):

    logger.info(
        f"🔘 Callback reçu : {call.data}"
    )

    try:

        if call.data == "disabled":

            bot.answer_callback_query(
                call.id,
                "❌ Bouton désactivé"
            )

            return

        if call.data.startswith("page_"):

            # Le search_id contient désormais "|".
            # split("_", 2) garantit exactement
            # trois parties maximum:
            #
            # ["page", "chat_id|timestamp", "page"]
            #
            # Exemple:
            # page_123456789|1723456789_2

            parts = call.data.split("_", 2)

            if len(parts) == 3:

                search_id = parts[1]
                page_str = parts[2]

                try:

                    target_page = int(page_str)

                    cleanup_expired_states()

                    state = pagination_state.get(
                        search_id
                    )

                    if state is None:

                        bot.answer_callback_query(
                            call.id,
                            "⚠️ Recherche expirée "
                            "ou inexistante. "
                            "Relancez la recherche."
                        )

                        return

                    chat_id = state["chat_id"]

                    if (
                        chat_id
                        != call.message.chat.id
                    ):

                        bot.answer_callback_query(
                            call.id,
                            "⚠️ Cette recherche "
                            "n'est pas dans ce chat."
                        )

                        return

                    results = state["results"]
                    total_pages = state["total_pages"]

                    if (
                        target_page < 1
                        or
                        target_page > total_pages
                    ):

                        bot.answer_callback_query(
                            call.id,
                            "❌ Page invalide"
                        )

                        return

                    if "extra_message_ids" in state:

                        cleanup_extra_messages(
                            chat_id,
                            state[
                                "extra_message_ids"
                            ]
                        )

                        state[
                            "extra_message_ids"
                        ] = []

                    state["page"] = target_page

                    page_results = get_page_results(
                        results,
                        target_page
                    )

                    formatted_text = (
                        format_page_results(
                            page_results,
                            len(results),
                            target_page,
                            total_pages
                        )
                    )

                    markup = build_pagination_markup(
                        chat_id,
                        search_id,
                        target_page,
                        total_pages
                    )

                    if len(formatted_text) > 4000:

                        chunks = split_text_into_chunks(
                            formatted_text,
                            4000
                        )

                        try:

                            bot.edit_message_text(
                                chunks[0],
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup
                            )

                        except Exception as e:

                            logger.warning(
                                "⚠️ Erreur lors de "
                                f"l'édition du message : {e}"
                            )

                            new_msg = bot.send_message(
                                call.message.chat.id,
                                chunks[0],
                                reply_markup=markup
                            )

                            state[
                                "main_message_id"
                            ] = new_msg.message_id

                            try:

                                bot.delete_message(
                                    call.message.chat.id,
                                    call.message.message_id
                                )

                            except Exception:
                                pass

                        extra_ids = []

                        for chunk in chunks[1:]:

                            extra_msg = bot.send_message(
                                call.message.chat.id,
                                chunk
                            )

                            extra_ids.append(
                                extra_msg.message_id
                            )

                        state[
                            "extra_message_ids"
                        ] = extra_ids

                    else:

                        try:

                            bot.edit_message_text(
                                formatted_text,
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=markup
                            )

                        except Exception as e:

                            logger.warning(
                                "⚠️ Erreur lors de "
                                f"l'édition du message : {e}"
                            )

                            new_msg = bot.send_message(
                                call.message.chat.id,
                                formatted_text,
                                reply_markup=markup
                            )

                            state[
                                "main_message_id"
                            ] = new_msg.message_id

                            try:

                                bot.delete_message(
                                    call.message.chat.id,
                                    call.message.message_id
                                )

                            except Exception:
                                pass

                    bot.answer_callback_query(
                        call.id
                    )

                except ValueError:

                    bot.answer_callback_query(
                        call.id,
                        "❌ Erreur de pagination"
                    )

            else:

                bot.answer_callback_query(
                    call.id,
                    "❌ Format de pagination invalide"
                )

        else:

            bot.answer_callback_query(
                call.id
            )

    except Exception as e:

        logger.warning(
            f"⚠️ Erreur callback : {e}"
        )

        try:

            bot.answer_callback_query(
                call.id,
                "❌ Erreur lors du traitement"
            )

        except Exception:
            pass


# ============================================================
# MESSAGE INCONNU
# ============================================================

@bot.message_handler(
    func=lambda message: True,
    content_types=["text"]
)
def unknown_message_handler(message):

    logger.info(
        "❓ 23. unknown_message_handler "
        "ENTRÉ (catch-all)"
    )

    # Le contenu du message est masqué pour des raisons
    # de sécurité (éviter d'exposer le mot de passe).
    logger.info(
        f"📩 24. Message texte non reconnu "
        f"de user_id={message.from_user.id} "
        "(contenu masqué)"
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

@app.route("/", methods=["GET"])
def home():

    return (
        "Bot Telegram M3U OK",
        200
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route("/health", methods=["GET"])
def health():

    return {
        "status": "ok",
        "bot": "running",
        "mode": "webhook",
        "supabase_connected": (
            supabase is not None
        ),
        "webhook_configured": (
            WEBHOOK_CONFIGURED
        )
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

    logger.info(
        f"📨 Headers: {dict(request.headers)}"
    )

    try:

        if not request.is_json:

            logger.warning(
                "⚠️ Webhook reçu mais "
                "Content-Type n'est pas JSON. "
                f"Content-Type: {request.content_type}"
            )

            return (
                "Bad Request",
                400
            )

        json_string = request.get_data(
            as_text=True
        )

        if not json_string:

            logger.warning(
                "⚠️ Webhook vide."
            )

            return (
                "OK",
                200
            )

        logger.info(
            "📦 1. Update Telegram reçu"
        )

        logger.info(
            f"📦 2. Longueur: "
            f"{len(json_string)} caractères"
        )

        try:

            data = json.loads(
                json_string
            )

            logger.info(
                "📦 3. JSON parsé avec succès"
            )

        except json.JSONDecodeError as e:

            logger.exception(
                f"❌ JSON Telegram invalide : {e}"
            )

            return (
                "Bad Request",
                400
            )

        update_keys = list(
            data.keys()
        )

        logger.info(
            f"📋 4. Type d'update : "
            f"{update_keys}"
        )

        if "message" in data:

            msg = data["message"]
            user = msg.get(
                "from",
                {}
            )

            logger.info(
                "👤 5. Message Telegram : "
                f"user_id={user.get('id')} "
                f"username={user.get('username')} "
                f"chat_type="
                f"{msg.get('chat', {}).get('type')}"
            )

            # IMPORTANT:
            # Le contenu des messages privés est
            # volontairement masqué dans les logs
            # afin de ne jamais exposer le mot de passe.

            text = msg.get(
                "text",
                ""
            )

            if (
                text
                and
                not text.startswith("/")
            ):

                logger.info(
                    "📝 6. Message texte "
                    "non-commande reçu "
                    "(contenu masqué)"
                )

            elif (
                text
                and
                text.startswith("/")
            ):

                logger.info(
                    f"📝 6. Commande reçue: "
                    f"{text.split()[0]}"
                )

        elif "callback_query" in data:

            callback = data[
                "callback_query"
            ]

            logger.info(
                "🔘 5. Callback Telegram reçu : "
                f"{callback.get('data')}"
            )

        logger.info(
            "🔄 7. Tentative de conversion "
            "Update.de_json()"
        )

        update = telebot.types.Update.de_json(
            json_string
        )

        # Récupération robuste de l'update_id
        update_id = getattr(update, 'update_id', None)
        update_id_str = str(update_id) if update_id is not None else 'None'

        logger.info(
            f"📦 8. Update créé: "
            f"update_id={update_id_str}"
        )

        if update is None:

            logger.warning(
                "⚠️ 9. Update est None - "
                "Impossible de créer l'objet "
                "Update Telegram."
            )

            return (
                "OK",
                200
            )

        logger.info(
            "⚙️ 10. Traitement de l'update "
            f"id={update_id_str}"
        )

        logger.info(
            f"📋 11. Handlers message: "
            f"{len(bot.message_handlers)}"
        )

        logger.info(
            f"📋 12. Handlers callback: "
            f"{len(bot.callback_query_handlers)}"
        )

        logger.info(
            "➡️ 13. AVANT "
            "bot.process_new_updates()"
        )

        bot.process_new_updates(
            [update]
        )

        logger.info(
            "⬅️ 14. APRÈS "
            "bot.process_new_updates()"
        )

        logger.info(
            "✅ 15. Update Telegram "
            "traité avec succès."
        )

        return (
            "OK",
            200
        )

    except Exception as e:

        logger.exception(
            f"❌ ERREUR CRITIQUE WEBHOOK : {e}"
        )

        return (
            "Internal Server Error",
            500
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    # Ce bloc n'est exécuté qu'en développement local.
    #
    # En production avec Gunicorn:
    # gunicorn m3u_database_bot:app
    #
    # Gunicorn importe le module et le bloc
    # if __name__ == "__main__": n'est pas exécuté.

    logger.info(
        "🚀 Bot M3U Database démarré "
        "en mode développement"
    )

    # Le webhook est déjà configuré
    # au niveau du module.

    # Pour le développement local:
    # fallback polling uniquement si le webhook
    # n'a pas pu être configuré.

    if not WEBHOOK_CONFIGURED:

        logger.warning(
            "⚠️ Webhook non configuré, "
            "démarrage en mode polling"
        )

        bot.polling(
            non_stop=True
        )

    else:

        logger.info(
            "✅ Webhook configuré, "
            "démarrage du serveur Flask"
        )

        app.run(
            host="0.0.0.0",
            port=int(
                os.environ.get(
                    "PORT",
                    5000
                )
            )
        )


# ============================================================
# COMMANDE RENDER RECOMMANDÉE
# ============================================================
#
# gunicorn m3u_database_bot:app --workers=1 --threads=1 --timeout=120
#
# ============================================================
