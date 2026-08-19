import os
import re
import telebot
import json
import time
import threading
from datetime import datetime
from typing import List, Dict, Set
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============================================
# SERVEUR HTTP FACTICE POUR RENDER
# ============================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b'Bot is running!')

def run_health_server():
    """Lance un petit serveur HTTP sur le port 8080 pour Render"""
    try:
        server = HTTPServer(('0.0.0.0', 8080), HealthHandler)
        print("✅ Serveur HTTP sur le port 8080 (pour Render)")
        server.serve_forever()
    except Exception as e:
        print(f"⚠️ Erreur serveur HTTP: {e}")

# ============================================
# CONFIGURATION
# ============================================

BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '123456789').split(',')))

DATA_FOLDER = "m3u_database"
os.makedirs(DATA_FOLDER, exist_ok=True)
INDEX_FILE = "m3u_index.json"

class M3UDatabaseBot:
    def __init__(self, token: str):
        self.bot = telebot.TeleBot(token)
        self.index = self.load_index()
        self.setup_handlers()
        print("✅ Bot M3U Database démarré !")
        print(f"📊 Fichiers chargés : {len(self.index)}")
    
    def load_index(self) -> Dict:
        if os.path.exists(INDEX_FILE):
            try:
                with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return {}
    
    def save_index(self):
        try:
            with open(INDEX_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.index, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"❌ Erreur: {e}")
    
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
        found_links = []
        server_clean = server_url.replace("http://", "").replace("https://", "").strip()
        server_clean = server_clean.rstrip('/')
        all_links = self.get_all_links()
        for link in all_links:
            if server_clean in link or server_url in link:
                found_links.append(link)
        return found_links
    
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
        
        @self.bot.message_handler(content_types=['document'])
        def document_handler(message):
            self.handle_document(message)
        
        @self.bot.callback_query_handler(func=lambda call: True)
        def callback_handler(call):
            self.handle_callback(call)
    
    def start_command(self, message):
        welcome_text = """
🤖 **Bot M3U Database**

📋 **Commandes :**
🔍 `/m3u <serveur>` - Recherche des liens M3U
📊 `/stats` - Statistiques
📤 Envoyer un fichier .txt - Ajouter des liens (admin)
"""
        self.bot.reply_to(message, welcome_text, parse_mode='Markdown')
    
    def help_command(self, message):
        help_text = """
🤖 **Aide**
🔍 `/m3u http://serveur.com:8080`
📊 `/stats`
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
        
        links = self.search_links_by_server(server_url)
        
        if links:
            result_text = f"✅ **{len(links)} liens trouvés**\n\n"
            
            if len(links) <= 30:
                for i, link in enumerate(links, 1):
                    result_text += f"{i}. `{link}`\n"
                
                self.bot.edit_message_text(
                    result_text,
                    search_msg.chat.id,
                    search_msg.message_id,
                    parse_mode='Markdown'
                )
            else:
                filename = f"m3u_{int(time.time())}.txt"
                with open(filename, 'w', encoding='utf-8') as f:
                    for link in links:
                        f.write(f"{link}\n")
                
                result_text += f"📁 {len(links)} liens trouvés"
                
                with open(filename, 'rb') as f:
                    self.bot.send_document(
                        message.chat.id,
                        f,
                        caption=result_text,
                        parse_mode='Markdown'
                    )
                
                os.remove(filename)
                self.bot.delete_message(search_msg.chat.id, search_msg.message_id)
        else:
            self.bot.edit_message_text(
                f"❌ Aucun lien trouvé pour : `{server_url}`",
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
"""
        self.bot.reply_to(message, stats_text, parse_mode='Markdown')
    
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
            self.save_index()
            
            self.bot.reply_to(
                message,
                f"✅ Fichier ajouté !\n📁 {document.file_name}\n🔗 {link_count} liens",
                parse_mode='Markdown'
            )
            
        except Exception as e:
            self.bot.reply_to(message, f"❌ Erreur : {str(e)}")
    
    def handle_callback(self, call):
        self.bot.answer_callback_query(call.id)
    
    def run(self):
        print("🚀 Bot démarré !")
        try:
            self.bot.polling(none_stop=True, interval=1)
        except Exception as e:
            print(f"❌ Erreur: {e}")
            time.sleep(5)
            self.run()


# ============================================
# DÉMARRAGE
# ============================================

if __name__ == '__main__':
    if not BOT_TOKEN:
        print("❌ Erreur: BOT_TOKEN non défini")
        exit(1)
    
    # 1. Lancer le serveur HTTP pour Render
    thread = threading.Thread(target=run_health_server, daemon=True)
    thread.start()
    
    # 2. Lancer le bot
    bot = M3UDatabaseBot(BOT_TOKEN)
    bot.run()
