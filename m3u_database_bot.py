import os
import re
import json
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
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
# TRAITEMENT WEBHOOK ASYNCHRONE / ANTI-DOUBLON
# ============================================================
# Le webhook doit répondre immédiatement à Telegram.
# Les recherches lourdes sont exécutées hors de la requête Flask.
WEBHOOK_EXECUTOR = ThreadPoolExecutor(max_workers=1)
UPDATE_LOCK = threading.Lock()
PROCESSED_UPDATE_IDS = set()
MAX_PROCESSED_UPDATE_IDS = 2000


# ============================================================
# SUPABASE OPERATIONS - VERSION MÉMOIRE OPTIMISÉE
# ============================================================

def get_all_files_from_supabase() -> List[Dict]:
    """Récupère uniquement les métadonnées des fichiers.

    IMPORTANT: ne récupère jamais file_content ici.
    """
    if not supabase:
        return []
    try:
        response = (
            supabase.table("m3u_files")
            .select("filename,original_name,date_added,links_count,file_size")
            .execute()
        )
        return response.data or []
    except Exception as e:
        logger.exception(f"❌ Erreur récupération métadonnées Supabase: {e}")
        return []


def get_file_from_supabase(filename: str) -> Optional[Dict]:
    """Récupère un fichier précis, y compris son contenu, seulement si nécessaire."""
    if not supabase:
        return None
    try:
        response = (
            supabase.table("m3u_files")
            .select("filename,original_name,date_added,links_count,file_size,file_content")
            .eq("filename", filename)
            .limit(1)
            .execute()
        )
        return response.data[0] if response.data else None
    except Exception as e:
        logger.exception(f"❌ Erreur récupération fichier {filename}: {e}")
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
        logger.exception(f"❌ Erreur sauvegarde fichier {filename}: {e}")
        return False


def delete_file_from_supabase(filename: str) -> bool:
    """Supprime un fichier de Supabase."""
    if not supabase:
        return False
    try:
        supabase.table("m3u_files").delete().eq("filename", filename).execute()
        return True
    except Exception as e:
        logger.exception(f"❌ Erreur suppression fichier {filename}: {e}")
        return False


def get_all_links_from_supabase() -> Set[str]:
    """Compatibilité legacy. Évite select=* mais peut rester coûteux;
    les statistiques utilisent désormais links_count directement."""
    if not supabase:
        return set()
    all_links = set()
    try:
        files = get_all_files_from_supabase()
        for meta in files:
            filename = meta.get("filename")
            if not filename:
                continue
            row = (
                supabase.table("m3u_files")
                .select("file_content")
                .eq("filename", filename)
                .limit(1)
                .execute()
            )
            if not row.data:
                continue
            content = row.data[0].get("file_content") or ""
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    all_links.add(line)
            del content
    except Exception as e:
        logger.exception(f"❌ Erreur récupération liens: {e}")
    return all_links


SEARCH_SEPARATOR = "━━━━━━━━━━━━━━━━━━"
RESULTS_PER_PAGE = 10
SEARCH_STATE_EXPIRY_SECONDS = 3600

def iter_blocks(text: str):
    """Parcourt les blocs sans créer une liste de tous les blocs."""
    start = 0
    sep = SEARCH_SEPARATOR
    while True:
        pos = text.find(sep, start)
        if pos == -1:
            block = text[start:].strip()
            if block:
                yield block
            break
        block = text[start:pos].strip()
        if block:
            yield block
        start = pos + len(sep)


def _get_file_metadata_for_search() -> List[Dict]:
    """Retourne seulement filename/file_size pour éviter select=* ."""
    if not supabase:
        return []
    response = (
        supabase.table("m3u_files")
        .select("filename,file_size")
        .execute()
    )
    return response.data or []


def _get_content_for_one_file(filename: str) -> str:
    """Charge un seul file_content à la fois."""
    response = (
        supabase.table("m3u_files")
        .select("file_content")
        .eq("filename", filename)
        .limit(1)
        .execute()
    )
    if not response.data:
        return ""
    return response.data[0].get("file_content") or ""


def search_page_and_count_in_supabase(server_url: str, page: int = 1):
    """Recherche avec faible empreinte mémoire.

    Les métadonnées sont chargées en premier, puis un seul fichier à la fois.
    Seuls les 10 blocs de la page demandée sont conservés.
    """
    if not supabase:
        return [], 0

    server_clean = normalize_server(server_url)
    start_idx = (page - 1) * RESULTS_PER_PAGE
    end_idx = start_idx + RESULTS_PER_PAGE
    page_results = []
    total = 0

    logger.info(f"🔎 Recherche mémoire optimisée: {server_clean} | page={page}")

    try:
        files = _get_file_metadata_for_search()
        logger.info(f"📁 {len(files)} fichier(s) à examiner, contenus chargés un par un")

        for meta in files:
            filename = meta.get("filename")
            if not filename:
                continue

            try:
                content = _get_content_for_one_file(filename)
                if not content:
                    continue

                for block in iter_blocks(content):
                    block_lower = block.lower()
                    if server_clean in block_lower:
                        if start_idx <= total < end_idx:
                            page_results.append(block)
                        total += 1

                # Libération explicite avant de passer au fichier suivant.
                del content
            except Exception as e:
                logger.warning(f"⚠️ Erreur recherche dans {filename}: {e}")

        logger.info(f"🔎 Recherche terminée: {total} résultat(s), page={page}")
        return page_results, total
    except Exception as e:
        logger.exception(f"❌ Erreur recherche Supabase: {e}")
        return [], 0


def search_page_only_in_supabase(server_url: str, page: int):
    """Récupère une page sans conserver tous les résultats en RAM."""
    if not supabase:
        return []
    server_clean = normalize_server(server_url)
    start_idx = (page - 1) * RESULTS_PER_PAGE
    end_idx = start_idx + RESULTS_PER_PAGE
    results = []
    seen = 0
    try:
        for meta in _get_file_metadata_for_search():
            filename = meta.get("filename")
            if not filename:
                continue
            content = _get_content_for_one_file(filename)
            if not content:
                continue
            for block in iter_blocks(content):
                if server_clean in block.lower():
                    if start_idx <= seen < end_idx:
                        results.append(block)
                    seen += 1
                    if seen >= end_idx:
                        del content
                        return results
            del content
    except Exception as e:
        logger.exception(f"❌ Erreur récupération page Supabase: {e}")
    return results


def search_links_in_supabase(server_url: str) -> List[str]:
    """Compatibilité avec l'ancien code: retourne les résultats, mais bornés."""
    page, total = search_page_and_count_in_supabase(server_url, 1)
    if total <= RESULTS_PER_PAGE:
        return page
    # Ne jamais reconstruire une énorme liste en mémoire.
    logger.warning(f"⚠️ search_links_in_supabase legacy limité à {RESULTS_PER_PAGE} résultats")
    return page


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
            .select("user_id,username,first_name,last_name,authenticated_at,last_activity")
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
            .select("user_id,username,first_name,last_name,authenticated_at,last_activity")
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
    reply_to_message_id: Optional[int] = None,
    total_results_override: Optional[int] = None
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
        total_results_override if total_results_override is not None else len(results),
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
                        f"⚠️ {webhook_info.pending_update_count} update(s) en attente"
                    )

                if webhook_info.last_error_message:
                    logger.warning(
                        "⚠️ Dernière erreur Telegram (informatif): "
                        f"{webhook_info.last_error_message}"
                    )

                # Le critère fiable ici est que Telegram pointe bien vers
                # notre URL. Une ancienne erreur 502 ne doit pas empêcher
                # le démarrage du bot.
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

@bot.message_handler(commands=["m3u", "search"])
def m3u_handler(message):
    logger.info(f"📩 Commande recherche reçue : {message.text}")
    try:
        text = message.text or ""
        parts = text.split(" ", 1)
        if len(parts) < 2:
            bot.reply_to(message, "❌ Format incorrect.\n\nUtilise :\n/m3u http://serveur.com:8080")
            return

        server_url = parts[1].strip()
        if not (server_url.startswith("http://") or server_url.startswith("https://")):
            bot.reply_to(message, "❌ URL invalide.\n\nExemple :\nhttp://serveur.com:8080")
            return

        chat_id = message.chat.id
        search_msg = bot.reply_to(message, f"🔍 Recherche pour :\n{server_url}")

        # Un seul état léger: aucun résultat massif n'est conservé en RAM.
        cleanup_expired_states()
        for key in list(pagination_state.keys()):
            state = pagination_state.get(key, {})
            if state.get("chat_id") == chat_id:
                try:
                    cleanup_extra_messages(chat_id, state.get("extra_message_ids", []))
                except Exception:
                    pass
                pagination_state.pop(key, None)

        page_results, total_results = search_page_and_count_in_supabase(server_url, 1)
        total_pages = get_total_pages(total_results)
        search_id = generate_search_id(chat_id, int(time.time() * 1000))

        if total_results:
            result = send_paginated_message(
                chat_id, search_id, page_results, 1, total_pages, search_msg.message_id,
                total_results_override=total_results
            )
            pagination_state[search_id] = {
                "chat_id": chat_id,
                "server_url": server_url,
                "page": 1,
                "total_results": total_results,
                "total_pages": total_pages,
                "main_message_id": result["main_message_id"],
                "extra_message_ids": result["extra_message_ids"],
                "timestamp": time.time()
            }
            try:
                bot.delete_message(chat_id, search_msg.message_id)
            except Exception:
                pass
        else:
            bot.edit_message_text(
                f"❌ Aucun résultat trouvé pour :\n{server_url}",
                chat_id, search_msg.message_id
            )
    except Exception as e:
        logger.exception(f"❌ Erreur commande M3U : {e}")
        try:
            bot.reply_to(message, "❌ Une erreur est survenue pendant la recherche.")
        except Exception:
            pass


# ============================================================
# /STATS - PROTÉGÉ PAR AUTHENTIFICATION
# ============================================================

@bot.message_handler(commands=["stats"])
def stats_handler(message):
    logger.info(f"📩 /stats reçu de user_id={message.from_user.id}")
    user_id = message.from_user.id
    if message.chat.type != "private":
        bot.reply_to(message, "🔐 Cette commande est réservée aux utilisateurs authentifiés.\n\nVeuillez utiliser cette commande en conversation privée avec le bot.")
        return
    if not is_user_authenticated(user_id):
        bot.reply_to(message, "🔐 Accès protégé.\n\nVeuillez vous authentifier en envoyant le mot de passe en message privé.")
        return
    try:
        if not supabase:
            bot.reply_to(message, "❌ Supabase n'est pas configuré.")
            return

        # Seulement les colonnes numériques: aucun file_content n'est chargé.
        rows = (
            supabase.table("m3u_files")
            .select("links_count,file_size")
            .execute()
        ).data or []
        total_files = len(rows)
        total_links = sum(int(r.get("links_count") or 0) for r in rows)
        total_size = sum(int(r.get("file_size") or 0) for r in rows)
        total_mb = total_size / (1024 * 1024)
        del rows

        stats_text = (
            "📊 Statistiques\n\n"
            f"📁 Fichiers : {total_files}\n\n"
            f"🔗 Liens enregistrés : {total_links}\n\n"
            f"💾 Taille totale : {total_mb:.2f} MB\n\n"
            "🔄 Statut : ✅ en ligne\n\n"
            "🌐 Mode : Webhook"
        )
        bot.reply_to(message, stats_text)
    except Exception as e:
        logger.exception(f"❌ Erreur /stats : {e}")
        try:
            bot.reply_to(message, "❌ Impossible de récupérer les statistiques.")
        except Exception:
            pass


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
# CALLBACKS - PAGINATION SANS STOCKAGE DES RÉSULTATS
# ============================================================

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        if call.data == "disabled":
            bot.answer_callback_query(call.id, "❌ Bouton désactivé")
            return

        if not call.data.startswith("page_"):
            bot.answer_callback_query(call.id)
            return

        parts = call.data.split("_", 2)
        if len(parts) != 3:
            bot.answer_callback_query(call.id, "❌ Format de pagination invalide")
            return

        search_id = parts[1]
        target_page = int(parts[2])
        cleanup_expired_states()
        state = pagination_state.get(search_id)

        if not state:
            bot.answer_callback_query(call.id, "⚠️ Recherche expirée. Relancez /m3u.")
            return
        if state.get("chat_id") != call.message.chat.id:
            bot.answer_callback_query(call.id, "⚠️ Cette recherche n'est pas dans ce chat.")
            return

        total_pages = int(state.get("total_pages", 1))
        if target_page < 1 or target_page > total_pages:
            bot.answer_callback_query(call.id, "❌ Page invalide")
            return

        page_results = search_page_only_in_supabase(state["server_url"], target_page)
        total_results = int(state.get("total_results", 0))

        cleanup_extra_messages(call.message.chat.id, state.get("extra_message_ids", []))
        state["extra_message_ids"] = []
        state["page"] = target_page
        state["timestamp"] = time.time()

        formatted_text = format_page_results(page_results, total_results, target_page, total_pages)
        markup = build_pagination_markup(
            call.message.chat.id, search_id, target_page, total_pages
        )

        if len(formatted_text) <= 4000:
            try:
                bot.edit_message_text(
                    formatted_text,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=markup
                )
                state["main_message_id"] = call.message.message_id
            except Exception as e:
                logger.warning(f"⚠️ Erreur édition pagination: {e}")
                new_msg = bot.send_message(
                    call.message.chat.id, formatted_text, reply_markup=markup
                )
                state["main_message_id"] = new_msg.message_id
        else:
            chunks = split_text_into_chunks(formatted_text, 4000)
            try:
                bot.edit_message_text(
                    chunks[0], call.message.chat.id, call.message.message_id, reply_markup=markup
                )
                state["main_message_id"] = call.message.message_id
            except Exception:
                new_msg = bot.send_message(
                    call.message.chat.id, chunks[0], reply_markup=markup
                )
                state["main_message_id"] = new_msg.message_id
            extra_ids = []
            for chunk in chunks[1:]:
                extra_ids.append(bot.send_message(call.message.chat.id, chunk).message_id)
            state["extra_message_ids"] = extra_ids

        bot.answer_callback_query(call.id)
    except (ValueError, KeyError) as e:
        logger.warning(f"⚠️ Erreur pagination: {e}")
        try:
            bot.answer_callback_query(call.id, "❌ Erreur de pagination")
        except Exception:
            pass
    except Exception as e:
        logger.exception(f"❌ Erreur callback : {e}")
        try:
            bot.answer_callback_query(call.id, "❌ Erreur lors du traitement")
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
# WEBHOOK TELEGRAM - RÉPONSE IMMÉDIATE
# ============================================================

def _process_update_background(update, update_id):
    try:
        logger.info(f"⚙️ Traitement asynchrone update_id={update_id}")
        bot.process_new_updates([update])
        logger.info(f"✅ Update {update_id} traité en arrière-plan")
    except Exception as e:
        logger.exception(f"❌ Erreur traitement update {update_id}: {e}")
    finally:
        # On garde l'ID dans PROCESSED_UPDATE_IDS pour empêcher un retraitement.
        pass


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Accuse réception immédiatement puis traite Telegram hors requête HTTP.

    Cela évite que Telegram obtienne un 502 si /m3u prend longtemps ou si
    la recherche est interrompue par Render.
    """
    try:
        if not request.is_json:
            return "Bad Request", 400

        data = request.get_json(silent=True)
        if not data:
            return "OK", 200

        update_id = data.get("update_id")
        if update_id is None:
            return "OK", 200

        with UPDATE_LOCK:
            if update_id in PROCESSED_UPDATE_IDS:
                logger.info(f"♻️ Update déjà reçu, ignoré: {update_id}")
                return "OK", 200
            PROCESSED_UPDATE_IDS.add(update_id)
            if len(PROCESSED_UPDATE_IDS) > MAX_PROCESSED_UPDATE_IDS:
                # Nettoyage simple et borné. Les IDs Telegram sont croissants.
                oldest = sorted(PROCESSED_UPDATE_IDS)[:500]
                for old_id in oldest:
                    PROCESSED_UPDATE_IDS.discard(old_id)

        update = telebot.types.Update.de_json(json.dumps(data))
        if update is None:
            return "OK", 200

        logger.info(f"📨 Webhook accepté immédiatement: update_id={update_id}")
        WEBHOOK_EXECUTOR.submit(_process_update_background, update, update_id)
        return "OK", 200

    except Exception as e:
        logger.exception(f"❌ ERREUR WEBHOOK : {e}")
        # Pour une erreur de traitement interne, on évite de provoquer une
        # boucle de retries Telegram qui pourrait aggraver la charge.
        return "OK", 200


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
