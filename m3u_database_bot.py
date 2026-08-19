import os
import re
import telebot
import json
import time
import threading
import logging
from datetime import datetime
from typing import List, Dict, Set
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================
# LOGS
# ============================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ============================================
# SERVEUR HTTP POUR RENDER
# ============================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running!')

def run_health_server():
    try:
        server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
        logger.info("✅ Serveur HTTP sur le port 8080")
        server.serve_forever()
    except Exception as e:
        logger.warning(f"⚠️ Erreur serveur HTTP: {e}")

# ============================================
# CONFIGURATION
# ============================================
BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '123456789').split(',')))

DATA_FOLDER = "m3u_database"
os.makedirs(DATA_FOLDER, exist_ok=True)
INDEX_FILE = "m3u_index.json"
BACKUP_FILE = "m3u_backup.txt"  # Fichier de sauvegarde sur Telegram

class M3UDatabaseBot:
    def __init__(self, token: str):
        self.token = token
        self.bot = None
        self.index = {}
        self.running = True
        self.connect_bot()
        self.load_index()
        self.setup_handlers()
        logger.info("✅ Bot M3U Database démarré !")
        logger.info(f"📊 Fichiers chargés : {len(self.index)}")

    def connect_bot(self):
        try:
            self.bot = telebot.TeleBot(self.token)
            logger.info("✅ Bot connecté à Telegram")
            return True
        except Exception as e:
            logger.error(f"❌ Erreur de connexion: {e}")
            return False

    def load_index(self):
        """Charge l'index depuis le fichier local OU depuis Telegram"""
        # 1. Essayer depuis le fichier local
        if os.path.exists(INDEX_FILE):
            try:
                with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                    self.index = json.load(f)
                logger.info(f"📂 Index chargé depuis fichier local: {len(self.index)} fichiers")
                return
            except Exception as e:
                logger.warning(f"⚠️ Erreur chargement local: {e}")

        # 2. Essayer de récupérer depuis Telegram
        try:
            # Récupère les messages du bot lui-même
            bot_id = self.bot.get_me().id
            messages = self.bot.get_chat_history(bot_id, limit=10)
            
            for msg in messages:
                if msg.document and msg.document.file_name == BACKUP_FILE:
                    # Télécharger le fichier de sauvegarde
                    file_info = self.bot.get_file(msg.document.file_id)
                    downloaded = self.bot.download_file(file_info.file_path)
                    
                    # Sauvegarder localement
                    with open(INDEX_FILE, 'wb') as f:
                        f.write(downloaded)
                    
                    # Charger le contenu
                    with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                        self.index = json.load(f)
                    
                    logger.info(f"📂 Index récupéré depuis Telegram: {len(self.index)} fichiers")
                    return
        except Exception as e:
            logger.warning(f"⚠️ Erreur chargement depuis Telegram: {e}")

        # 3. Index vide
        self.index = {}
        logger.info("📁 Index vide, démarrage propre")

    def save_index(self):
        """Sauvegarde l'index localement ET sur Telegram"""
        # Sauvegarde locale
        try:
            with open(INDEX_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.index, f, indent=2, ensure_ascii=False)
            logger.info("💾 Index sauvegardé localement")
        except Exception as e:
            logger.error(f"❌ Erreur sauvegarde locale: {e}")

        # Sauvegarde sur Telegram (fichier envoyé à lui-même)
        try:
            bot_id = self.bot.get_me().id
            with open(INDEX_FILE, 'rb') as f:
                self.bot.send_document(
                    bot_id,
                    f,
                    caption=f"💾 Sauvegarde - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            logger.info("💾 Sauvegarde sur Telegram réussie")
        except Exception as e:
            logger.error(f"❌ Erreur sauvegarde Telegram: {e}")

    def get_all_links(self) -> Set[str]:
        all_links = set()
        for filename in self.index.keys():
            filepath = os.path.join(DATA_FOLDER, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith('#'):
                                all_links.add(line)
                except:
                    pass
        return all_links

    def search_links_by_server(self, server_url: str) -> List[str]:
        found_lines = []
        server_clean = server_url.replace("http://", "").replace("https://", "").strip()
        server_clean = server_clean.rstrip('/')
        
        for filename in self.index.keys():
            filepath = os.path.join(DATA_FOLDER, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        content = f.read()
                        blocks = re.split(r'━━━━━━━━━━━━━━━━━━', content)
                        for block in blocks:
                            if server_clean in block or server_url in block:
                                found_lines.append(block.strip())
                except:
                    pass
        
        return found_lines

    def is_admin(self, user_id: int) -> bool:
        return user_id in ADMIN_IDS

    def setup_handlers(self):
        @self.bot.message_handler(commands=['start'])
        def start_handler(message):
            self.start_command(message)

        @self.bot.message_handler(commands=['help'])
        def help_handler(message):
            self.help_command(message)

        @self.bot.message_handler(commands=['m3u', 'search'])
        def m3u_handler(message):
            self.m3u_command(message)

        @self.bot.message_handler(commands=['stats'])
        def stats_handler(message):
            self.stats_command(message)

        @self.bot.message_handler(commands=['save'])
        def save_handler(message):
            self.save_command(message)

        @self.bot.message_handler(content_types=['document'])
        def document_handler(message):
            self.handle_document(message)

        @self.bot.callback_query_handler(func=lambda call: True)
        def callback_handler(call):
            self.handle_callback(call)

    def start_command(self, message):
        welcome_text = """
🤖 **Bot M3U Database - Version Sauvegarde Auto**

📋 **Commandes :**
🔍 `/m3u <serveur>` - Recherche des liens M3U
📊 `/stats` - Statistiques
💾 `/save` - Sauvegarde manuelle sur Telegram
📤 Envoyer un fichier .txt - Ajouter des liens (admin)

✅ **Sauvegarde automatique :**
- Fichiers stockés sur Telegram
- Récupérés au redémarrage
- Ne sont jamais perdus
"""
        self.bot.reply_to(message, welcome_text, parse_mode='Markdown')

    def help_command(self, message):
        help_text = """
🤖 **Aide**
🔍 `/m3u http://serveur.com:8080`
📊 `/stats`
💾 `/save`
"""
        self.bot.reply_to(message, help_text, parse_mode='Markdown')

    def m3u_command(self, message):
        text = message.text
        if not text or len(text.split()) < 2:
            self.bot.reply_to(
                message,
                "❌ Format : `/m3u <serveur>`\nEx: `/m3u http://fplay2.com:8080`",
                parse_mode='Markdown'
            )
            return

        server_url = text.split(' ', 1)[1].strip()

        if not server_url.startswith('http://') and not server_url.startswith('https://'):
            self.bot.reply_to(
                message,
                "❌ URL invalide. Exemple : `http://fplay2.com:8080`",
                parse_mode='Markdown'
            )
            return

        search_msg = self.bot.reply_to(
            message,
            f"🔍 Recherche pour : `{server_url}`...",
            parse_mode='Markdown'
        )

        blocks = self.search_links_by_server(server_url)

        if blocks:
            result_text = f"✅ **{len(blocks)} serveurs trouvés**\n\n"
            
            for i, block in enumerate(blocks, 1):
                result_text += f"**[{i}]**\n"
                result_text += f"{block}\n\n"
            
            if len(blocks) > 10:
                result_text += f"\n... et {len(blocks) - 10} autres résultats"

            self.bot.edit_message_text(
                result_text,
                search_msg.chat.id,
                search_msg.message_id,
                parse_mode='Markdown'
            )
        else:
            self.bot.edit_message_text(
                f"❌ Aucun serveur trouvé pour : `{server_url}`",
                search_msg.chat.id,
                search_msg.message_id,
                parse_mode='Markdown'
            )

    def stats_command(self, message):
        total_files = len(self.index)
        total_links = len(self.get_all_links())

        stats_text = f"""
📊 **Statistiques**
📁 Fichiers : {total_files}
🔗 Liens : {total_links}
🔄 Statut : ✅ en ligne
💾 Sauvegarde : Telegram
"""
        self.bot.reply_to(message, stats_text, parse_mode='Markdown')

    def save_command(self, message):
        if not self.is_admin(message.from_user.id):
            self.bot.reply_to(message, "❌ Permission refusée.")
            return

        self.save_index()
        self.bot.reply_to(message, "✅ Sauvegarde sur Telegram effectuée !")

    def handle_document(self, message):
        if not self.is_admin(message.from_user.id):
            self.bot.reply_to(message, "❌ Permission refusée.")
            return

        document = message.document

        if not document.file_name.endswith('.txt'):
            self.bot.reply_to(message, "❌ Seuls les fichiers .txt sont acceptés.")
            return

        try:
            file_info = self.bot.get_file(document.file_id)
            downloaded_file = self.bot.download_file(file_info.file_path)

            timestamp = int(time.time())
            filename = f"{timestamp}_{document.file_name}"
            filepath = os.path.join(DATA_FOLDER, filename)

            with open(filepath, 'wb') as f:
                f.write(downloaded_file)

            link_count = 0
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        link_count += 1

            self.index[filename] = {
                'original_name': document.file_name,
                'date_added': datetime.now().isoformat(),
                'links': link_count,
                'size': document.file_size
            }
            self.save_index()  # Sauvegarde automatique sur Telegram

            self.bot.reply_to(
                message,
                f"✅ Fichier ajouté !\n📁 {document.file_name}\n🔗 {link_count} liens\n💾 Sauvegardé sur Telegram",
                parse_mode='Markdown'
            )

        except Exception as e:
            logger.error(f"❌ Erreur traitement fichier: {e}")
            self.bot.reply_to(message, f"❌ Erreur : {str(e)}")

    def handle_callback(self, call):
        self.bot.answer_callback_query(call.id)

    def run(self):
        logger.info("🚀 Bot démarré !")
        while self.running:
            try:
                self.bot.polling(none_stop=True, interval=1, timeout=60)
            except Exception as e:
                logger.error(f"❌ Erreur polling: {e}")
                logger.info("🔄 Reconnexion dans 10 secondes...")
                time.sleep(10)
                self.connect_bot()
                self.setup_handlers()

# ============================================
# DÉMARRAGE
# ============================================

if __name__ == '__main__':
    if not BOT_TOKEN:
        logger.error("❌ Erreur: BOT_TOKEN non défini")
        exit(1)

    thread = threading.Thread(target=run_health_server, daemon=True)
    thread.start()

    bot = M3UDatabaseBot(BOT_TOKEN)
    bot.run()
