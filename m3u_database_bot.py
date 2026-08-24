import os
import re
import io
import json
import time
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import List, Set, Dict, Optional, Any

import telebot
from flask import Flask, request
from supabase import create_client, Client

# 'requests' est utilisé uniquement pour la vérification à la demande
# de la validité des liens (bouton "🔎 Vérifier les liens"). Si le
# paquet n'est pas installé, cette fonctionnalité se désactive
# proprement sans faire planter le reste du bot.
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


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
# TRAITEMENT ASYNCHRONE DES UPDATES TELEGRAM
# ============================================================
# Le webhook doit répondre très rapidement à Telegram. Une recherche
# /m3u peut être longue ; elle ne doit donc jamais bloquer la requête
# HTTP du webhook ni le worker Gunicorn.
WEBHOOK_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="telegram-update")
PROCESSED_UPDATE_IDS = set()
UPDATE_LOCK = threading.Lock()
MAX_PROCESSED_UPDATE_IDS = 5000


def _process_update_background(update, update_id):
    try:
        logger.info(f"⚙️ Traitement asynchrone update_id={update_id}")
        bot.process_new_updates([update])
        logger.info(f"✅ Update {update_id} traité en arrière-plan")
    except Exception as e:
        logger.exception(f"❌ Erreur traitement update {update_id}: {e}")


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

    server_clean = normalize_server(server_url)

    logger.info(
        f"🔎 Recherche serveur dans Supabase: {server_clean}"
    )

    # 1) Chemin rapide : recherche SQL côté Supabase sur l'index de
    # blocs (si la migration a été appliquée). Aucune donnée n'est
    # rapatriée/reparse en Python dans ce cas.
    rpc_results = search_blocks_via_rpc(server_clean)

    if rpc_results is not None:
        rpc_results = dedupe_blocks(rpc_results)

        logger.info(
            f"🔎 Résultats via index SQL (après dédoublonnage) : "
            f"{len(rpc_results)}"
        )
        return rpc_results

    # 2) Repli legacy : ancienne méthode (rapatrie et reparse tout
    # le contenu de tous les fichiers en Python). Toujours
    # fonctionnelle même sans migration SQL.
    found_lines = []

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

    found_lines = dedupe_blocks(found_lines)

    return found_lines


# ============================================================
# INDEXATION PAR BLOCS (recherche déportée côté Supabase)
# ============================================================
# But : au lieu de retélécharger et reparser TOUT le contenu de TOUS
# les fichiers à chaque /m3u (coûteux en CPU/RAM sur un serveur
# limité), on découpe chaque fichier en blocs UNE SEULE FOIS, à
# l'upload, et on les stocke dans une table dédiée `m3u_blocks`
# avec une colonne déjà normalisée. La recherche devient alors une
# requête SQL (idéalement via une fonction RPC utilisant un index
# trigram côté Postgres), exécutée par Supabase et non par ce
# process Python.
#
# Si la table/fonction SQL n'existe pas encore (migration non
# appliquée), tout retombe silencieusement sur l'ancienne méthode
# `search_links_in_supabase` : aucun comportement existant n'est
# cassé, la recherche est juste plus rapide une fois la migration
# faite.

BLOCK_SEPARATOR = "━━━━━━━━━━━━━━━━━━"

SEARCH_RPC_FUNCTION = "search_m3u_blocks"
BLOCKS_TABLE = "m3u_blocks"

# Bascule interne : si un appel RPC échoue une fois (table/fonction
# absente), on évite de retenter en boucle sur chaque recherche
# suivante et on repasse directement en mode legacy jusqu'au
# prochain redémarrage.
_rpc_search_available = True


def split_file_into_blocks(file_content: str) -> List[str]:
    """
    Découpe le contenu d'un fichier en blocs, exactement comme le
    fait déjà `search_links_in_supabase` (même séparateur), pour
    garantir un comportement de recherche identique une fois
    indexé en base.
    """
    return list(iter_blocks(file_content))


def iter_blocks(file_content: str):
    """
    Version "flux" du découpage en blocs : les fournit un par un
    (générateur) au lieu de construire toute la liste en mémoire
    d'un coup. Utilisée pour traiter les gros fichiers par petits
    paquets (voir process_upload_in_chunks) sans jamais dupliquer
    tout le contenu du fichier dans une deuxième liste complète.
    """
    if not file_content:
        return

    start = 0
    sep_len = len(BLOCK_SEPARATOR)

    while True:

        idx = file_content.find(BLOCK_SEPARATOR, start)

        if idx == -1:

            block = file_content[start:].strip()

            if block:
                yield block

            break

        block = file_content[start:idx].strip()

        if block:
            yield block

        start = idx + sep_len


def index_blocks_for_file(
    filename: str,
    file_content: str
) -> bool:
    """
    Construit l'index de recherche pour UN fichier : découpe en
    blocs, normalise chaque bloc, et les insère dans `m3u_blocks`.

    Appelée une seule fois par upload (et par /reindex pour les
    fichiers déjà existants) — jamais à chaque recherche. C'est ce
    qui déplace le coût CPU/mémoire de "à chaque /m3u" vers "à
    chaque ajout de fichier", bien plus rare.

    Idempotente : les anciens blocs du même filename sont
    supprimés avant réinsertion, donc rejouable sans dupliquer.
    """
    if not supabase:
        return False

    blocks_iter = iter_blocks(file_content)

    try:
        # Supprime les blocs existants de ce fichier avant de les
        # recréer, pour rester idempotent (utile pour /reindex).
        supabase.table(BLOCKS_TABLE).delete().eq(
            "filename", filename
        ).execute()

        # Construction ET envoi du lot suivant au fur et à mesure,
        # au lieu de construire toute la liste "rows" pour
        # l'ensemble du fichier avant de commencer l'envoi — évite
        # de dupliquer une deuxième fois tout le contenu du fichier
        # en mémoire (chaque ligne stocke le bloc 3 fois :
        # block_content, block_normalized, dedup_signature).
        batch_size = 500
        total_inserted = 0
        batch: List[Dict[str, str]] = []

        def flush_insert_batch(rows_batch: List[Dict[str, str]]) -> None:
            if rows_batch:
                supabase.table(BLOCKS_TABLE).insert(
                    rows_batch
                ).execute()

        for block in blocks_iter:

            batch.append({
                "filename": filename,
                "block_content": block,
                "block_normalized": normalize_server(block),
                "dedup_signature": normalize_block_for_dedup(block)
            })

            if len(batch) >= batch_size:
                flush_insert_batch(batch)
                total_inserted += len(batch)
                batch = []

        flush_insert_batch(batch)
        total_inserted += len(batch)

        if total_inserted == 0:
            return True

        logger.info(
            f"🧱 Indexation terminée pour {filename} : "
            f"{total_inserted} bloc(s)"
        )

        return True

    except Exception as e:
        logger.warning(
            "⚠️ Indexation par blocs impossible pour "
            f"{filename} (table '{BLOCKS_TABLE}' absente ? "
            f"migration SQL non appliquée ?) : {e}"
        )

        return False


def search_blocks_via_rpc(server_clean: str) -> Optional[List[str]]:
    """
    Recherche les blocs correspondants via la fonction SQL
    `search_m3u_blocks`, exécutée côté Supabase (index trigram).

    Retourne None si le RPC n'est pas disponible (migration non
    faite), afin que l'appelant sache qu'il doit basculer sur
    l'ancienne méthode — jamais d'exception qui remonte.
    """
    global _rpc_search_available

    if not supabase or not _rpc_search_available:
        return None

    try:
        response = supabase.rpc(
            SEARCH_RPC_FUNCTION,
            {"search_term": server_clean}
        ).execute()

        return [
            row["block_content"]
            for row in (response.data or [])
        ]

    except Exception as e:
        logger.warning(
            "⚠️ Recherche via RPC Supabase indisponible "
            "(migration SQL probablement non appliquée) — "
            f"repli sur l'ancienne méthode. Détail : {e}"
        )

        # Évite de retenter le RPC à chaque recherche suivante tant
        # que le process tourne : on sait déjà qu'il échoue.
        _rpc_search_available = False

        return None


def reindex_all_files() -> Dict[str, int]:
    """
    Reconstruit l'index de blocs pour TOUS les fichiers déjà
    présents dans `m3u_files`. À exécuter une fois après avoir
    appliqué la migration SQL, pour les fichiers uploadés avant
    la mise en place de l'indexation.
    """
    global _rpc_search_available

    files = get_all_files_from_supabase()

    success_count = 0
    fail_count = 0

    for file_data in files:
        filename = file_data.get("filename")
        file_content = file_data.get("file_content", "")

        if not filename:
            continue

        if index_blocks_for_file(filename, file_content):
            success_count += 1
        else:
            fail_count += 1

    if success_count > 0:
        # Si au moins un fichier a pu être indexé, la table existe
        # bien : on réautorise les tentatives de recherche RPC.
        _rpc_search_available = True

    return {
        "total": len(files),
        "success": success_count,
        "failed": fail_count
    }


# ============================================================
# DÉDOUBLONNAGE
# ============================================================
# Deux mécanismes complémentaires :
#  1) À L'UPLOAD : avant d'enregistrer un nouveau fichier, on
#     retire les blocs déjà identiques à un bloc existant ailleurs
#     dans la base, pour ne plus jamais grossir avec des doublons.
#  2) À L'AFFICHAGE : juste avant d'envoyer les résultats d'une
#     recherche, on retire les doublons de la liste retournée —
#     ceci corrige aussi immédiatement les doublons déjà présents
#     dans les fichiers uploadés AVANT cette mise à jour.

def normalize_block_for_dedup(block: str) -> str:
    """
    Calcule une signature de dédoublonnage pour un bloc : espaces
    multiples réduits, casse ignorée, puis condensé en empreinte
    SHA-256 (64 caractères fixes).

    Pourquoi une empreinte et pas le texte normalisé tel quel :
    certains blocs peuvent être anormalement longs (lignes mal
    formatées, collées sans séparateur), et Postgres refuse
    d'indexer une valeur texte trop grande ("index row size
    exceeds btree maximum" — rencontré en production sur un bloc
    inhabituellement gros). Une empreinte de taille fixe élimine
    complètement ce risque, quelle que soit la taille du bloc
    d'origine, tout en restant tout aussi fiable pour détecter les
    doublons (deux blocs identiques donnent toujours la même
    empreinte).
    """
    lines = [
        line.strip()
        for line in block.strip().split("\n")
        if line.strip()
    ]

    normalized_text = "\n".join(lines).lower()

    return hashlib.sha256(
        normalized_text.encode("utf-8")
    ).hexdigest()


def dedupe_blocks(blocks: List[str]) -> List[str]:
    """
    Retire les doublons d'une liste de blocs en conservant l'ordre
    d'apparition (le premier exemplaire rencontré est gardé).
    Utilisé juste avant d'afficher les résultats d'une recherche.
    """
    seen: Set[str] = set()
    result: List[str] = []

    for block in blocks:
        signature = normalize_block_for_dedup(block)

        if signature in seen:
            continue

        seen.add(signature)
        result.append(block)

    return result


def get_existing_block_signatures() -> Set[str]:
    """
    Récupère les signatures (contenu normalisé) de tous les blocs
    déjà stockés dans la base, pour détecter les doublons lors d'un
    nouvel upload.

    Chemin rapide : lit directement la table m3u_blocks si elle
    existe (migration appliquée). Repli : reparse tous les fichiers
    m3u_files si la table n'existe pas encore. Dans les deux cas,
    ceci n'est appelé qu'à l'upload d'un nouveau fichier — jamais à
    chaque recherche.
    """
    if not supabase:
        return set()

    try:
        response = supabase.table(BLOCKS_TABLE).select(
            "block_content"
        ).execute()

        return {
            normalize_block_for_dedup(row["block_content"])
            for row in (response.data or [])
            if row.get("block_content")
        }

    except Exception as e:
        logger.warning(
            "⚠️ Lecture de m3u_blocks impossible pour le "
            f"dédoublonnage (repli sur reparsing complet) : {e}"
        )

        signatures: Set[str] = set()

        for file_data in get_all_files_from_supabase():
            file_content = file_data.get("file_content", "")

            for block in split_file_into_blocks(file_content):
                signatures.add(normalize_block_for_dedup(block))

        return signatures


def filter_new_blocks(
    blocks: List[str],
    existing_signatures: Set[str]
) -> Dict[str, Any]:
    """
    Sépare les blocs d'un fichier fraîchement uploadé en deux
    groupes : ceux qui sont vraiment nouveaux (à conserver) et ceux
    qui sont des doublons (déjà présents ailleurs, ou répétés
    plusieurs fois dans le même fichier).

    Retourne un dict avec :
      - "kept": liste des blocs à garder, dans l'ordre d'origine
      - "duplicates_count": nombre de blocs ignorés
    """
    seen_in_this_file: Set[str] = set(existing_signatures)
    kept: List[str] = []
    duplicates_count = 0

    for block in blocks:
        signature = normalize_block_for_dedup(block)

        if signature in seen_in_this_file:
            duplicates_count += 1
            continue

        seen_in_this_file.add(signature)
        kept.append(block)

    return {
        "kept": kept,
        "duplicates_count": duplicates_count
    }


# --- Dédoublonnage déporté vers Supabase (comme /m3u et /stats) ---
# Au lieu de rapatrier TOUT le contenu existant vers Render pour
# comparer en Python (get_existing_block_signatures), on envoie
# uniquement les signatures (courtes chaînes) du fichier uploadé à
# une fonction SQL, qui répond directement quelles signatures sont
# déjà connues. Render ne reçoit alors qu'un petit résultat filtré,
# jamais le contenu complet de la base. Repli automatique sur
# l'ancienne méthode si la migration SQL n'est pas encore faite.

DEDUP_RPC_FUNCTION = "filter_new_dedup_signatures"

_rpc_dedup_available = True


def filter_new_signatures_via_rpc(
    signatures: List[str]
) -> Optional[Set[str]]:
    """
    Envoie une liste de signatures à la fonction SQL
    `filter_new_dedup_signatures`, qui renvoie uniquement celles
    qui n'existent PAS encore dans `m3u_blocks`.

    Retourne None si le RPC n'est pas disponible (migration non
    appliquée), pour que l'appelant sache qu'il doit basculer sur
    l'ancienne méthode — jamais d'exception qui remonte.
    """
    global _rpc_dedup_available

    if not supabase or not _rpc_dedup_available or not signatures:
        return None

    try:
        response = supabase.rpc(
            DEDUP_RPC_FUNCTION,
            {"candidate_signatures": signatures}
        ).execute()

        return set(response.data or [])

    except Exception as e:
        logger.warning(
            "⚠️ Dédoublonnage via RPC Supabase indisponible "
            "(migration SQL probablement non appliquée) — "
            f"repli sur l'ancienne méthode. Détail : {e}"
        )

        _rpc_dedup_available = False

        return None


# ============================================================
# VÉRIFICATION DE LA VALIDITÉ DES LIENS (À LA DEMANDE)
# ============================================================
# Teste uniquement les liens d'UNE page de résultats affichée
# (10 liens maximum), jamais toute la base d'un coup — pour rester
# rapide et ne jamais bloquer le bot. Déclenché par le bouton
# "🔎 Vérifier les liens" sous les résultats de recherche.

LINK_CHECK_TIMEOUT_SECONDS = 6
LINK_CHECK_MAX_WORKERS = 10

# --- Paramètres spécifiques aux vérifications EN MASSE ---
# (upload d'un fichier, /verifierbase) : concurrence volontairement
# très faible et pauses entre paquets, car le serveur d'hébergement
# tourne avec des ressources extrêmement limitées (0.1 CPU, 512 Mo
# de RAM). Le bouton "Vérifier cette page" (peu de liens à la fois)
# n'a pas besoin de ces précautions et garde ses propres réglages
# plus rapides ci-dessus.
BULK_CHECK_MAX_WORKERS = 3
BULK_CHECK_BATCH_SIZE = 15
BULK_CHECK_BATCH_PAUSE_SECONDS = 1.5

# Nombre maximum de liens uniques testés en une seule fois, pour
# ne jamais bloquer un worker du bot trop longtemps ni saturer le
# serveur avec un fichier ou une base trop volumineuse.
MAX_LINKS_CHECK_UPLOAD = 150
MAX_LINKS_CHECK_VERIFYBASE = 500


def extract_first_url(block: str) -> Optional[str]:
    """Extrait la première URL http(s) trouvée dans un bloc."""
    match = re.search(r"https?://[^\s]+", block)
    return match.group(0) if match else None


def check_link_alive(url: str) -> bool:
    """
    Teste si un lien répond encore. Retourne False au moindre doute
    (timeout, erreur de connexion, code d'erreur HTTP) — mieux vaut
    un léger risque de faux "expiré" qu'un faux "actif".
    """
    if not REQUESTS_AVAILABLE:
        return True  # pas de vérification possible, on n'affirme rien de faux

    try:
        response = requests.get(
            url,
            timeout=LINK_CHECK_TIMEOUT_SECONDS,
            stream=True,
            allow_redirects=True
        )

        alive = response.status_code < 400
        response.close()

        return alive

    except Exception:
        return False


def check_urls_status(
    urls: List[str],
    max_workers: int = LINK_CHECK_MAX_WORKERS,
    batch_size: Optional[int] = None,
    batch_pause_seconds: float = 0
) -> Dict[str, bool]:
    """
    Teste une liste d'URLs et retourne {url: True/False}.

    batch_size + batch_pause_seconds permettent de traiter les URLs
    par petits paquets avec une pause entre chaque paquet, pour ne
    jamais saturer un serveur aux ressources très limitées.
    Utilisé pour les vérifications EN MASSE (upload, /verifierbase),
    contrairement au bouton "Vérifier cette page" qui teste peu de
    liens d'un coup et n'a pas besoin de ce ménagement.
    """
    if not urls:
        return {}

    results: Dict[str, bool] = {}

    batches = (
        [urls[i:i + batch_size] for i in range(0, len(urls), batch_size)]
        if batch_size
        else [urls]
    )

    for batch_index, batch in enumerate(batches):

        with ThreadPoolExecutor(
            max_workers=min(max_workers, len(batch))
        ) as executor:

            future_to_url = {
                executor.submit(check_link_alive, url): url
                for url in batch
            }

            for future in future_to_url:
                url = future_to_url[future]
                try:
                    results[url] = future.result()
                except Exception:
                    results[url] = False

        # Pause entre chaque paquet : laisse le CPU respirer avant
        # d'attaquer le paquet suivant (important sur un serveur à
        # ressources très limitées).
        if batch_pause_seconds and batch_index < len(batches) - 1:
            time.sleep(batch_pause_seconds)

    return results


def check_links_status(blocks: List[str]) -> Dict[str, bool]:
    """
    Teste les liens uniques trouvés dans une liste de blocs.
    Utilisé par le bouton "Vérifier cette page" (peu de liens,
    pas besoin de traitement par paquets).
    """
    urls = []

    for block in blocks:
        url = extract_first_url(block)
        if url and url not in urls:
            urls.append(url)

    return check_urls_status(urls, max_workers=LINK_CHECK_MAX_WORKERS)


def annotate_blocks_with_status(
    blocks: List[str],
    status: Dict[str, bool]
) -> List[str]:
    """
    Ajoute ✅/❌ en tête de chaque bloc selon le statut de son lien.
    Les blocs sans URL détectable ne sont pas annotés.
    """
    annotated = []

    for block in blocks:
        url = extract_first_url(block)

        if url and url in status:
            prefix = "✅ " if status[url] else "❌ "
            annotated.append(prefix + block)
        else:
            annotated.append(block)

    return annotated


STATS_RPC_FUNCTION = "count_m3u_stats"

_rpc_stats_available = True


def get_stats_via_rpc() -> Optional[Dict[str, int]]:
    """
    Calcule le nombre de fichiers et de liens directement via une
    fonction SQL côté Supabase, sans rapatrier le contenu de tous
    les fichiers en Python.

    Retourne None si le RPC n'est pas disponible (migration non
    appliquée), pour que l'appelant sache qu'il doit basculer sur
    l'ancienne méthode — jamais d'exception qui remonte.
    """
    global _rpc_stats_available

    if not supabase or not _rpc_stats_available:
        return None

    try:
        response = supabase.rpc(
            STATS_RPC_FUNCTION,
            {}
        ).execute()

        if not response.data:
            return None

        row = response.data[0]

        return {
            "total_files": row.get("total_files", 0),
            "total_links": row.get("total_links", 0)
        }

    except Exception as e:
        logger.warning(
            "⚠️ Calcul des stats via RPC Supabase indisponible "
            "(migration SQL probablement non appliquée) — "
            f"repli sur l'ancienne méthode. Détail : {e}"
        )

        _rpc_stats_available = False

        return None


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

            # Silence volontaire dans un groupe.
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

    if REQUESTS_AVAILABLE:

        markup.row(
            InlineKeyboardButton(
                "🔎 Vérifier les liens de cette page",
                callback_data=(
                    f"verify_{search_id}_{current_page}"
                )
            )
        )

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

    if message.chat.type != "private":

        # Silence volontaire dans un groupe : /start ne doit
        # apparaître que dans la conversation privée avec le bot.
        return

    welcome_text = """
😎 𝙱𝙾𝚃 𝙶𝙾𝙻𝙳𝙴𝙽 𝚂𝙷𝙴𝙴𝙿

👁️ Commandes publiques autorisé :

🧐 /m3u <serveur>
Recherche des liens M3U (accessible à tous)
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

    if message.chat.type != "private":

        # Silence volontaire dans un groupe : /help ne doit
        # apparaître que dans la conversation privée avec le bot.
        return

    help_text = """
🤖 Aide

🔍 /m3u http://serveur.com:8080

📊 /stats (authentification requise)

🧱 /reindex (authentification requise, admin)

🧹 /verifierbase (authentification requise, admin)

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

        # Silence volontaire dans un groupe : aucune réponse,
        # pour ne pas révéler l'existence de cette commande
        # admin/privée aux membres du groupe.
        return

    try:

        # 1) Chemin rapide : calcul SQL côté Supabase, sans
        # rapatrier le contenu de tous les fichiers en Python.
        stats = get_stats_via_rpc()

        if stats is not None:
            total_files = stats["total_files"]
            total_links = stats["total_links"]

        else:
            # 2) Repli legacy : rapatrie et reparse tout le
            # contenu en Python. Toujours fonctionnel même sans
            # migration SQL.
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
# /REINDEX - PROTÉGÉ PAR AUTHENTIFICATION ET ADMIN_IDS
# ============================================================
# Reconstruit l'index de blocs (table m3u_blocks) pour tous les
# fichiers déjà présents. À exécuter UNE FOIS après avoir appliqué
# la migration SQL, pour couvrir les fichiers uploadés avant la
# mise en place de l'indexation. Les nouveaux uploads sont indexés
# automatiquement (voir document_handler), donc /reindex n'a pas
# besoin d'être relancé ensuite, sauf en cas de doute.

@bot.message_handler(commands=["reindex"])
def reindex_handler(message):

    logger.info(
        f"📩 /reindex reçu de user_id="
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

        # Silence volontaire dans un groupe : aucune réponse,
        # pour ne pas révéler l'existence de cette commande
        # admin/privée aux membres du groupe.
        return

    try:

        if not is_admin(user_id):

            bot.reply_to(
                message,
                "❌ Permission refusée. "
                "Vous n'êtes pas administrateur."
            )

            return

        if not supabase:

            bot.reply_to(
                message,
                "❌ Supabase n'est pas configuré."
            )

            return

        progress_msg = bot.reply_to(
            message,
            "🧱 Reconstruction de l'index en cours..."
        )

        result = reindex_all_files()

        bot.edit_message_text(
            "✅ Réindexation terminée.\n\n"
            f"📁 Fichiers traités : {result['total']}\n"
            f"🧱 Indexés avec succès : {result['success']}\n"
            f"⚠️ Échecs : {result['failed']}\n\n"
            + (
                "La table 'm3u_blocks' semble introuvable — "
                "as-tu bien exécuté la migration SQL dans "
                "Supabase ?"
                if result["success"] == 0 and result["total"] > 0
                else "La recherche /m3u utilisera désormais "
                "l'index SQL rapide."
            ),
            progress_msg.chat.id,
            progress_msg.message_id
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur /reindex : {e}"
        )

        try:

            bot.reply_to(
                message,
                f"❌ Erreur : {e}"
            )

        except Exception:
            pass


# ============================================================
# /VERIFIERBASE - VÉRIFICATION EN MASSE DES LIENS (ADMIN)
# ============================================================
# Scanne tous les fichiers de la base, teste chaque lien unique,
# puis propose une suppression manuelle (jamais automatique) des
# liens morts trouvés. Concurrence volontairement très faible et
# traitement par paquets (BULK_CHECK_*) pour ménager un serveur
# aux ressources très limitées.

cleanup_state: Dict[str, Dict] = {}
CLEANUP_STATE_EXPIRY_SECONDS = 3600


def get_all_blocks_for_scan() -> List[Dict]:
    """
    Retourne tous les blocs de tous les fichiers, sous la forme
    [{"filename": ..., "block": ...}, ...].

    Chemin rapide : lit directement la table m3u_blocks, déjà
    découpée à l'upload — aucun reparsing nécessaire côté Render,
    et on ne rapatrie que le texte des blocs (pas les fichiers
    entiers, ni les colonnes inutiles). Même principe que pour
    /m3u et /stats.

    Repli automatique : si la table n'existe pas encore (migration
    non appliquée), reparse tous les fichiers m3u_files comme
    avant — comportement identique, juste plus lent.
    """
    if supabase:
        try:
            response = supabase.table(BLOCKS_TABLE).select(
                "filename, block_content"
            ).execute()

            return [
                {
                    "filename": row["filename"],
                    "block": row["block_content"]
                }
                for row in (response.data or [])
                if row.get("block_content")
            ]

        except Exception as e:
            logger.warning(
                "⚠️ Lecture directe de m3u_blocks impossible pour "
                "/verifierbase (migration SQL probablement non "
                f"appliquée) — repli sur reparsing complet. "
                f"Détail : {e}"
            )

    blocks: List[Dict] = []

    for file_data in get_all_files_from_supabase():

        filename = file_data.get("filename")
        file_content = file_data.get("file_content", "")

        if not filename or not file_content:
            continue

        for block in split_file_into_blocks(file_content):
            blocks.append({"filename": filename, "block": block})

    return blocks


@bot.message_handler(commands=["verifierbase"])
def verifierbase_handler(message):

    logger.info(
        f"📩 /verifierbase reçu de user_id="
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

        # Silence volontaire dans un groupe : commande admin,
        # jamais révélée ni utilisable en dehors du privé.
        return

    try:

        if not is_admin(user_id):

            bot.reply_to(
                message,
                "❌ Permission refusée. "
                "Vous n'êtes pas administrateur."
            )

            return

        if not supabase:

            bot.reply_to(
                message,
                "❌ Supabase n'est pas configuré."
            )

            return

        if not REQUESTS_AVAILABLE:

            bot.reply_to(
                message,
                "❌ Vérification indisponible sur ce serveur "
                "(module 'requests' manquant)."
            )

            return

        progress_msg = bot.reply_to(
            message,
            "🔎 Vérification de la base en cours...\n\n"
            "⚠️ Traitement volontairement lent pour ménager "
            "les ressources du serveur — cela peut prendre "
            "plusieurs minutes, merci de patienter."
        )

        all_blocks = get_all_blocks_for_scan()

        # Associe chaque URL unique à tous les blocs/fichiers où
        # elle apparaît, pour pouvoir cibler la suppression plus
        # tard sans tout re-scanner. original_name n'est pas suivi
        # ici : il sera relu (pour les seuls fichiers réellement
        # modifiés) au moment de la suppression confirmée.
        url_to_occurrences: Dict[str, List[Dict]] = {}
        total_blocks_scanned = 0

        for entry in all_blocks:

            filename = entry["filename"]
            block = entry["block"]

            url = extract_first_url(block)

            if not url:
                continue

            total_blocks_scanned += 1

            url_to_occurrences.setdefault(
                url, []
            ).append({
                "filename": filename,
                "block": block
            })

        urls_uniques = list(url_to_occurrences.keys())

        limite_atteinte = (
            len(urls_uniques) > MAX_LINKS_CHECK_VERIFYBASE
        )

        urls_a_tester = urls_uniques[:MAX_LINKS_CHECK_VERIFYBASE]

        statut_liens = check_urls_status(
            urls_a_tester,
            max_workers=BULK_CHECK_MAX_WORKERS,
            batch_size=BULK_CHECK_BATCH_SIZE,
            batch_pause_seconds=BULK_CHECK_BATCH_PAUSE_SECONDS
        )

        urls_mortes = [
            url
            for url, vivant in statut_liens.items()
            if not vivant
        ]

        rapport = (
            "🔎 Vérification terminée\n\n"
            f"📦 {total_blocks_scanned} lien(s) scanné(s) au total\n"
            f"🔗 {len(urls_uniques)} lien(s) unique(s)\n"
            f"🧪 {len(urls_a_tester)} lien(s) unique(s) testé(s)\n"
            f"✅ {len(urls_a_tester) - len(urls_mortes)} actif(s)\n"
            f"❌ {len(urls_mortes)} mort(s)\n"
        )

        if limite_atteinte:
            rapport += (
                f"\nℹ️ Limite de {MAX_LINKS_CHECK_VERIFYBASE} "
                "liens testés par exécution atteinte (protection "
                "des ressources) — relance /verifierbase plus "
                "tard pour continuer sur le reste.\n"
            )

        if not urls_mortes:

            rapport += "\n✨ Aucun lien mort trouvé."

            bot.edit_message_text(
                rapport,
                progress_msg.chat.id,
                progress_msg.message_id
            )

            return

        # Prépare l'état de nettoyage — la suppression réelle
        # n'aura lieu qu'après confirmation manuelle via le bouton.
        cleanup_id = f"{message.chat.id}|{int(time.time())}"

        dead_by_file: Dict[str, List[str]] = {}

        for url in urls_mortes:
            for occ in url_to_occurrences.get(url, []):

                fname = occ["filename"]

                dead_by_file.setdefault(fname, [])
                dead_by_file[fname].append(occ["block"])

        cleanup_state[cleanup_id] = {
            "chat_id": message.chat.id,
            "dead_by_file": dead_by_file,
            "dead_links_count": len(urls_mortes),
            "timestamp": time.time()
        }

        from telebot.types import (
            InlineKeyboardMarkup,
            InlineKeyboardButton
        )

        markup = InlineKeyboardMarkup()

        markup.row(
            InlineKeyboardButton(
                f"🗑 Supprimer les {len(urls_mortes)} lien(s) mort(s)",
                callback_data=f"cleandead_{cleanup_id}"
            )
        )

        markup.row(
            InlineKeyboardButton(
                "❌ Ne rien supprimer",
                callback_data=f"cancelclean_{cleanup_id}"
            )
        )

        rapport += (
            "\nSouhaites-tu supprimer ces liens morts de la base ?"
        )

        bot.edit_message_text(
            rapport,
            progress_msg.chat.id,
            progress_msg.message_id,
            reply_markup=markup
        )

    except Exception as e:

        logger.exception(
            f"❌ Erreur /verifierbase : {e}"
        )

        try:

            bot.reply_to(
                message,
                f"❌ Erreur : {e}"
            )

        except Exception:
            pass


# ============================================================
# TRAITEMENT D'UPLOAD PAR PETITS PAQUETS (ANTI-OOM)
# ============================================================
# Suite à un plantage constaté sur un fichier de 15 Mo (le
# processus dépassait la RAM disponible sur Render et mourait
# silencieusement en plein traitement), l'upload est désormais
# traité PAR PAQUETS de UPLOAD_CHUNK_SIZE blocs : dédoublonnage,
# vérification des liens et accumulation des résultats se font au
# fur et à mesure, sans jamais garder tout le fichier découpé en
# mémoire d'un coup. Rien à changer côté utilisateur — un seul
# envoi suffit, même pour un gros fichier.

UPLOAD_CHUNK_SIZE = 300


def process_upload_in_chunks(file_content: str) -> Dict[str, Any]:
    """
    Traite un fichier uploadé par petits paquets plutôt que tout
    garder en mémoire d'un coup.

    Pour chaque paquet de UPLOAD_CHUNK_SIZE blocs, dans l'ordre :
      1. Dédoublonnage (dans le paquet + contre ce qui a déjà été
         vu plus tôt dans CE fichier + contre la base, via RPC).
      2. Vérification de validité des liens du paquet (tant que le
         plafond global MAX_LINKS_CHECK_UPLOAD n'est pas atteint).
      3. Les blocs survivants du paquet sont écrits directement
         dans un buffer texte (io.StringIO) — jamais accumulés
         dans une liste Python séparée, pour éviter qu'une liste
         ET un texte joint ET une liste de lignes (pour compter
         les liens) coexistent tous en mémoire en même temps.

    Retourne un dict avec "file_content" (déjà prêt à sauvegarder),
    "link_count", "duplicates_count", "dead_count" et
    "verification_desactivee".
    """
    seen_signatures: Set[str] = set()

    content_buffer = io.StringIO()
    first_block_written = False

    duplicates_count = 0
    dead_count = 0
    link_count = 0
    links_tested_so_far = 0
    verification_desactivee = False

    # Repli legacy : si le RPC de dédoublonnage n'est pas
    # disponible, on rapatrie les signatures existantes UNE SEULE
    # FOIS pour tout le fichier (pas à chaque paquet), pour ne pas
    # refaire ce travail lourd des dizaines de fois de suite.
    legacy_existing_signatures: Optional[Set[str]] = None

    def write_kept_block(block: str) -> None:
        """
        Écrit un bloc survivant directement dans le buffer texte
        final, et compte ses liens au passage — sans jamais
        reparcourir tout le fichier une deuxième fois pour ça.
        """
        nonlocal first_block_written, link_count

        if first_block_written:
            content_buffer.write("\n" + BLOCK_SEPARATOR + "\n")

        content_buffer.write(block)
        first_block_written = True

        for line in block.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                link_count += 1

    def flush_chunk(chunk_blocks: List[str]) -> None:

        nonlocal duplicates_count, dead_count
        nonlocal verification_desactivee, links_tested_so_far
        nonlocal legacy_existing_signatures

        if not chunk_blocks:
            return

        # --- Dédoublonnage ---
        chunk_unique_blocks: List[str] = []
        chunk_unique_sigs: List[str] = []

        for block in chunk_blocks:

            sig = normalize_block_for_dedup(block)

            if sig in seen_signatures:
                duplicates_count += 1
                continue

            seen_signatures.add(sig)
            chunk_unique_blocks.append(block)
            chunk_unique_sigs.append(sig)

        if not chunk_unique_blocks:
            return

        new_sigs = filter_new_signatures_via_rpc(chunk_unique_sigs)

        if new_sigs is None:

            if legacy_existing_signatures is None:
                legacy_existing_signatures = (
                    get_existing_block_signatures()
                )

            new_sigs = {
                sig for sig in chunk_unique_sigs
                if sig not in legacy_existing_signatures
            }

            legacy_existing_signatures |= set(chunk_unique_sigs)

        chunk_new_blocks = [
            block
            for block, sig in zip(
                chunk_unique_blocks, chunk_unique_sigs
            )
            if sig in new_sigs
        ]

        duplicates_count += (
            len(chunk_unique_blocks) - len(chunk_new_blocks)
        )

        if not chunk_new_blocks:
            return

        # --- Vérification de validité (si sous le plafond) ---
        if REQUESTS_AVAILABLE and not verification_desactivee:

            url_par_bloc = {}
            urls_a_tester = []

            for block in chunk_new_blocks:
                url = extract_first_url(block)
                if url:
                    url_par_bloc[block] = url
                    if url not in urls_a_tester:
                        urls_a_tester.append(url)

            if (
                links_tested_so_far + len(urls_a_tester)
                > MAX_LINKS_CHECK_UPLOAD
            ):

                verification_desactivee = True

            elif urls_a_tester:

                links_tested_so_far += len(urls_a_tester)

                statut = check_urls_status(
                    urls_a_tester,
                    max_workers=BULK_CHECK_MAX_WORKERS,
                    batch_size=BULK_CHECK_BATCH_SIZE,
                    batch_pause_seconds=(
                        BULK_CHECK_BATCH_PAUSE_SECONDS
                    )
                )

                for block in chunk_new_blocks:

                    url = url_par_bloc.get(block)

                    if url and statut.get(url) is False:
                        dead_count += 1
                    else:
                        write_kept_block(block)

                return

        # Vérification désactivée (plafond atteint) ou aucun lien
        # détecté dans ce paquet : on garde les blocs tels quels.
        for block in chunk_new_blocks:
            write_kept_block(block)

    chunk: List[str] = []

    for block in iter_blocks(file_content):

        chunk.append(block)

        if len(chunk) >= UPLOAD_CHUNK_SIZE:
            flush_chunk(chunk)
            chunk = []

    flush_chunk(chunk)

    return {
        "file_content": content_buffer.getvalue(),
        "link_count": link_count,
        "duplicates_count": duplicates_count,
        "dead_count": dead_count,
        "verification_desactivee": verification_desactivee
    }


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

        file_content_brut = downloaded_file.decode(
            "utf-8",
            errors="ignore"
        )

        # Libère les octets bruts dès qu'on a le texte décodé —
        # inutile de garder les deux versions en mémoire pendant
        # tout le traitement qui suit.
        del downloaded_file

        # --- Traitement par petits paquets (anti-OOM) ---
        # Dédoublonnage + vérification des liens morts, effectués
        # UPLOAD_CHUNK_SIZE blocs à la fois plutôt que tout garder
        # en mémoire d'un coup. Le contenu final et le comptage des
        # liens sont déjà prêts en sortie — aucune reconstruction
        # ni recomptage supplémentaire nécessaire ici (voir
        # process_upload_in_chunks pour le détail).
        resultat = process_upload_in_chunks(file_content_brut)

        # Le texte brut original n'est plus nécessaire une fois le
        # traitement terminé — seul resultat["file_content"] (déjà
        # nettoyé) est gardé pour la suite.
        del file_content_brut

        file_content = resultat["file_content"]
        link_count = resultat["link_count"]
        doublons_ignores = resultat["duplicates_count"]
        liens_morts_ignores = resultat["dead_count"]
        verification_desactivee = resultat["verification_desactivee"]

        if not file_content:

            bot.reply_to(
                message,
                "⚠️ Aucun lien nouveau et valide dans ce fichier.\n\n"
                f"🔁 {doublons_ignores} doublon(s) déjà présent(s)\n"
                f"❌ {liens_morts_ignores} lien(s) mort(s) ignoré(s)\n\n"
                "Rien n'a été ajouté."
            )

            return

        success = save_file_to_supabase(
            filename=filename,
            original_name=document.file_name,
            file_content=file_content,
            links_count=link_count,
            file_size=len(file_content.encode("utf-8"))
        )

        if success:

            # Indexation par blocs pour une recherche rapide côté
            # Supabase. Ne bloque jamais l'upload : si la migration
            # SQL n'a pas encore été appliquée, cette étape échoue
            # proprement et search_links_in_supabase retombera sur
            # l'ancienne méthode.
            index_blocks_for_file(filename, file_content)

            message_confirmation = (
                "✅ Fichier ajouté dans Supabase !\n\n"
                f"📁 {document.file_name}\n\n"
                f"🔗 {link_count} liens uniques et valides ajoutés\n"
            )

            if doublons_ignores > 0:
                message_confirmation += (
                    f"🔁 {doublons_ignores} doublon(s) ignoré(s)\n"
                )

            if liens_morts_ignores > 0:
                message_confirmation += (
                    f"❌ {liens_morts_ignores} lien(s) mort(s) ignoré(s)\n"
                )

            if verification_desactivee:
                message_confirmation += (
                    "ℹ️ Vérification de validité désactivée pour "
                    f"cet upload (plus de {MAX_LINKS_CHECK_UPLOAD} "
                    "liens uniques — protège les ressources du "
                    "serveur)\n"
                )

            message_confirmation += "\n💾 Stockage cloud activé."

            bot.reply_to(
                message,
                message_confirmation
            )

            logger.info(
                f"✅ Fichier sauvegardé dans Supabase : "
                f"{filename} ({link_count} liens valides, "
                f"{doublons_ignores} doublons ignorés, "
                f"{liens_morts_ignores} liens morts ignorés)"
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
                "/reindex\n"
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

        elif call.data.startswith("verify_"):

            # Format attendu : verify_{chat_id}|{timestamp}_{page}
            # Même découpage que pour "page_" (le search_id contient "|").
            parts = call.data.split("_", 2)

            if len(parts) == 3:

                search_id = parts[1]
                page_str = parts[2]

                try:
                    target_page = int(page_str)

                    if search_id not in pagination_state:
                        bot.answer_callback_query(
                            call.id,
                            "❌ Résultats expirés, relance une recherche."
                        )
                        return

                    if not REQUESTS_AVAILABLE:
                        bot.answer_callback_query(
                            call.id,
                            "❌ Vérification indisponible sur ce serveur."
                        )
                        return

                    bot.answer_callback_query(
                        call.id,
                        "🔎 Vérification en cours..."
                    )

                    state = pagination_state[search_id]
                    all_results = state.get("results", [])

                    page_results = get_page_results(
                        all_results,
                        target_page
                    )

                    # Le test des liens (requêtes HTTP) se fait en
                    # arrière-plan : le webhook Telegram a déjà reçu
                    # sa réponse "OK" bien avant, donc pas de risque
                    # de timeout côté Telegram.
                    status = check_links_status(page_results)

                    annotated_results = annotate_blocks_with_status(
                        page_results,
                        status
                    )

                    total_pages = state.get("total_pages", 1)

                    formatted_text = format_page_results(
                        annotated_results,
                        len(all_results),
                        target_page,
                        total_pages
                    )

                    markup = build_pagination_markup(
                        call.message.chat.id,
                        search_id,
                        target_page,
                        total_pages
                    )

                    try:
                        bot.edit_message_text(
                            formatted_text,
                            call.message.chat.id,
                            call.message.message_id,
                            reply_markup=markup
                        )
                    except Exception as e:
                        logger.warning(
                            "⚠️ Erreur lors de l'affichage "
                            f"des résultats vérifiés : {e}"
                        )

                except ValueError:

                    bot.answer_callback_query(
                        call.id,
                        "❌ Erreur de vérification"
                    )

            else:

                bot.answer_callback_query(
                    call.id,
                    "❌ Format de vérification invalide"
                )

        elif call.data.startswith("cleandead_"):

            cleanup_id = call.data[len("cleandead_"):]

            if not is_admin(call.from_user.id):

                bot.answer_callback_query(
                    call.id,
                    "❌ Réservé aux administrateurs."
                )

                return

            state = cleanup_state.get(cleanup_id)

            if (
                not state
                or time.time() - state.get("timestamp", 0)
                > CLEANUP_STATE_EXPIRY_SECONDS
            ):

                cleanup_state.pop(cleanup_id, None)

                bot.answer_callback_query(
                    call.id,
                    "❌ Cette demande a expiré, relance /verifierbase."
                )

                return

            bot.answer_callback_query(
                call.id,
                "🧹 Suppression en cours..."
            )

            cleanup_state.pop(cleanup_id, None)

            dead_by_file = state["dead_by_file"]

            fichiers_modifies = 0
            liens_supprimes = 0

            for target_filename, dead_blocks in dead_by_file.items():

                current_file = get_file_from_supabase(
                    target_filename
                )

                if not current_file:
                    continue

                current_content = current_file.get(
                    "file_content", ""
                )
                current_blocks = split_file_into_blocks(
                    current_content
                )

                dead_signatures = {
                    normalize_block_for_dedup(b)
                    for b in dead_blocks
                }

                remaining_blocks = [
                    b for b in current_blocks
                    if normalize_block_for_dedup(b)
                    not in dead_signatures
                ]

                removed_count = (
                    len(current_blocks) - len(remaining_blocks)
                )

                if removed_count == 0:
                    continue

                new_content = (
                    ("\n" + BLOCK_SEPARATOR + "\n").join(
                        remaining_blocks
                    )
                    if remaining_blocks
                    else ""
                )

                new_link_count = sum(
                    1
                    for line in new_content.split("\n")
                    if line.strip()
                    and not line.strip().startswith("#")
                )

                # original_name relu directement depuis le fichier
                # existant (récupéré juste au-dessus) — pas besoin
                # de l'avoir suivi depuis le scan initial.
                original_name = current_file.get(
                    "original_name", target_filename
                )

                delete_file_from_supabase(target_filename)

                save_file_to_supabase(
                    filename=target_filename,
                    original_name=original_name,
                    file_content=new_content,
                    links_count=new_link_count,
                    file_size=len(new_content.encode("utf-8"))
                )

                index_blocks_for_file(
                    target_filename, new_content
                )

                fichiers_modifies += 1
                liens_supprimes += removed_count

            try:

                bot.edit_message_text(
                    "✅ Nettoyage terminé.\n\n"
                    f"🗑 {liens_supprimes} lien(s) mort(s) supprimé(s)\n"
                    f"📁 {fichiers_modifies} fichier(s) modifié(s)",
                    call.message.chat.id,
                    call.message.message_id
                )

            except Exception as e:

                logger.warning(
                    "⚠️ Erreur affichage résultat nettoyage : "
                    f"{e}"
                )

        elif call.data.startswith("cancelclean_"):

            cleanup_id = call.data[len("cancelclean_"):]
            cleanup_state.pop(cleanup_id, None)

            bot.answer_callback_query(
                call.id,
                "Annulé."
            )

            try:

                bot.edit_message_text(
                    "❌ Suppression annulée. Aucun lien n'a été "
                    "retiré de la base.",
                    call.message.chat.id,
                    call.message.message_id
                )

            except Exception:
                pass

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
# Ne réagit qu'en conversation privée avec le bot. Dans un groupe,
# le bot reste totalement silencieux pour tout ce qui n'est pas
# une commande explicitement gérée (comme /m3u) — un membre qui
# colle un lien, écrit un message normal, ou tape une commande
# inconnue ne provoque plus aucune réponse du bot.

@bot.message_handler(
    func=lambda message:
        message.chat.type == "private",
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

@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Reçoit un update Telegram et accuse réception immédiatement.

    Le traitement réel est délégué à un ThreadPoolExecutor afin qu'une
    recherche longue ne bloque jamais le webhook Telegram/Gunicorn.
    """
    try:
        if not request.is_json:
            logger.warning(
                "⚠️ Webhook reçu avec un Content-Type non JSON: %s",
                request.content_type
            )
            return "Bad Request", 400

        data = request.get_json(silent=True)
        if not data:
            logger.warning("⚠️ Webhook JSON vide")
            return "OK", 200

        update_id = data.get("update_id")
        if update_id is None:
            logger.warning("⚠️ Webhook sans update_id")
            return "OK", 200

        # Déduplication : Telegram peut renvoyer un update si la réponse
        # HTTP arrive trop tard. Ici la réponse est immédiate, mais on garde
        # cette protection pour éviter un double traitement.
        with UPDATE_LOCK:
            if update_id in PROCESSED_UPDATE_IDS:
                logger.info(f"♻️ Update déjà reçu, ignoré: {update_id}")
                return "OK", 200

            PROCESSED_UPDATE_IDS.add(update_id)

            if len(PROCESSED_UPDATE_IDS) > MAX_PROCESSED_UPDATE_IDS:
                # Les update_id Telegram sont croissants : supprimer les
                # plus anciens garde la mémoire bornée.
                for old_id in sorted(PROCESSED_UPDATE_IDS)[:1000]:
                    PROCESSED_UPDATE_IDS.discard(old_id)

        update = telebot.types.Update.de_json(json.dumps(data))
        if update is None:
            logger.warning(f"⚠️ Impossible de convertir update_id={update_id}")
            return "OK", 200

        logger.info(
            f"📨 Webhook accepté immédiatement: update_id={update_id}"
        )

        WEBHOOK_EXECUTOR.submit(
            _process_update_background,
            update,
            update_id
        )

        # IMPORTANT : répondre immédiatement à Telegram.
        return "OK", 200

    except Exception as e:
        logger.exception(f"❌ ERREUR WEBHOOK : {e}")
        # Même en cas d'erreur interne, répondre 200 évite une boucle de
        # retransmissions Telegram qui pourrait aggraver la situation.
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
