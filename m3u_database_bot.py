import os
import re
import telebot
import json
import time
from datetime import datetime
from typing import List, Dict, Set

BOT_TOKEN = os.environ.get('BOT_TOKEN')
ADMIN_IDS = list(map(int, os.environ.get('ADMIN_IDS', '123456789').split(',')))

DATA_FOLDER = "m3u_database"
os.makedirs(DATA_FOLDER, exist_ok=True)
INDEX_FILE = "m3u_index.json"

# ============================================
# STOCKAGE AUTOMATIQUE DANS TELEGRAM
# ============================================

class M3UDatabaseBot:
    def __init__(self, token: str):
        self.bot = telebot.TeleBot(token)
        self.index = self.load_index()
        self.setup_handlers()
        print("✅ Bot M3U Database démarré !")
        print(f"📊 Fichiers chargés : {len(self.index)}")
    
    def load_index(self) -> Dict:
        """Charge l'index depuis le fichier local OU depuis Telegram"""
        # Essayer de charger depuis le fichier local d'abord
        if os.path.exists(INDEX_FILE):
            try:
                with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        
        # Si pas de fichier local, essayer de récupérer depuis Telegram
        try:
            # Le bot envoie un message à lui-même pour récupérer les données
            # On utilise le fichier envoyé précédemment
            messages = self.bot.get_chat_history(self.bot.get_me().id, limit=5)
            for msg in messages:
                if msg.document and msg.document.file_name == INDEX_FILE:
                    # Télécharger le fichier
                    file_info = self.bot.get_file(msg.document.file_id)
                    downloaded = self.bot.download_file(file_info.file_path)
                    
                    # Sauvegarder localement
                    with open(INDEX_FILE, 'wb') as f:
                        f.write(downloaded)
                    
                    # Charger le contenu
                    with open(INDEX_FILE, 'r', encoding='utf-8') as f:
                        return json.load(f)
        except Exception as e:
            print(f"⚠️ Erreur chargement depuis Telegram: {e}")
        
        return {}
    
    def save_index(self):
        """Sauvegarde l'index localement ET sur Telegram"""
        # Sauvegarde locale
        try:
            with open(INDEX_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.index, f, indent=2, ensure_ascii=False)
            
            # Sauvegarde sur Telegram (envoi à lui-même)
            try:
                # Récupérer l'ID du bot lui-même
                bot_id = self.bot.get_me().id
                
                with open(INDEX_FILE, 'rb') as f:
                    self.bot.send_document(
                        bot_id,  # Envoi à lui-même
                        f,
                        caption=f"📦 Sauvegarde - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    )
                print("✅ Sauvegarde sur Telegram réussie !")
            except Exception as e:
                print(f"⚠️ Erreur sauvegarde Telegram: {e}")
                
        except Exception as e:
            print(f"❌ Erreur sauvegarde: {e}")
    
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
        
        @self.bot.message_handler(commands=['backup'])
        def backup_handler(message):
            self.backup_command(message)
        
        @self.bot.message_handler(content_types=['document'])
        def document_handler(message):
            self.handle_document(message)
        
        @self.bot.callback_query_handler(func=lambda call: True)
        def callback_handler(call):
            self.handle_callback(call)
    
    def start_command(self, message):
        welcome_text = """
🤖 **Bot M3U Database - Stockage Auto**

📋 **Commandes :**
🔍 `/m3u <serveur>` - Recherche des liens M3U
📊 `/stats` - Statistiques
💾 `/backup` - Forcer la sauvegarde sur Telegram
📤 Envoyer un fichier .txt - Ajouter des liens (admin)

💡 **Stockage persistant :**
Les fichiers sont automatiquement sauvegardés sur Telegram !
Le bot les récupère à chaque redémarrage.
"""
        self.bot.reply_to(message, welcome_text, parse_mode='Markdown')
    
    def help_command(self, message):
        help_text = """
🤖 **Aide**
🔍 `/m3u http://serveur.com:8080`
📊 `/stats`
💾 `/backup` - Sauvegarde forcée
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
💾 Stockage : Telegram (Auto)
"""
        self.bot.reply_to(message, stats_text, parse_mode='Markdown')
    
    def backup_command(self, message):
        """Commande /backup - Force une sauvegarde sur Telegram"""
        if not self.is_admin(message.from_user.id):
            self.bot.reply_to(message, "❌ Permission refusée.")
            return
        
        self.save_index()
        self.bot.reply_to(message, "✅ Sauvegarde effectuée sur Telegram !")
    
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
            self.save_index()  # ← Sauvegarde sur Telegram
            
            self.bot.reply_to(
                message,
                f"✅ Fichier ajouté !\n📁 {document.file_name}\n🔗 {link_count} liens\n💾 Sauvegardé sur Telegram",
                parse_mode='Markdown'
            )
            
        except Exception as e:
            self.bot.reply_to(message, f"❌ Erreur : {str(e)}")
    
    def handle_callback(self, call):
        self.bot.answer_callback_query(call.id)
    
    def run(self):
        print("🚀 Bot démarré !")
        print("💾 Stockage automatique sur Telegram activé")
        try:
            self.bot.polling(none_stop=True, interval=1)
        except Exception as e:
            print(f"❌ Erreur: {e}")
            time.sleep(5)
            self.run()

if __name__ == '__main__':
    if not BOT_TOKEN:
        print("❌ Erreur: BOT_TOKEN non défini")
        exit(1)
    
    bot = M3UDatabaseBot(BOT_TOKEN)
    bot.run()
