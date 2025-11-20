import os
import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TimedOut, NetworkError
import aiohttp
from bs4 import BeautifulSoup
import tempfile
import json
from pathlib import Path
from urllib.parse import urlparse
import re
from datetime import datetime
from typing import Dict, List, Optional
import time
import random
from flask import Flask, request
import threading

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global state for user sessions
user_sessions = {}

class UserSession:
    def __init__(self, user_id: int):
        self.user_id = user_id
        self.is_processing = False
        self.should_stop = False
        self.current_file = None
        self.memories = []
        self.processed_count = 0
        self.success_count = 0
        self.failed_count = 0
        self.start_time = None
        self.processing_message = None
        self.stats = {'images': 0, 'videos': 0, 'other': 0}
        self.failed_memories = []
        self.current_index = 0

    def reset(self):
        self.is_processing = False
        self.should_stop = False
        self.current_file = None
        self.processed_count = 0
        self.success_count = 0
        self.failed_count = 0
        self.start_time = None
        self.stats = {'images': 0, 'videos': 0, 'other': 0}
        self.failed_memories = []
        self.current_index = 0

class SnapchatMemoryProcessor:
    def __init__(self):
        self.timeout = aiohttp.ClientTimeout(total=60)
        
    def parse_html_file(self, html_content: str) -> List[Dict]:
        """Parse Snapchat HTML file and extract memory download links"""
        soup = BeautifulSoup(html_content, 'html.parser')
        memories = []
        
        table = soup.find('table')
        if not table:
            return memories
            
        rows = table.find_all('tr')[1:]
        
        for row in rows:
            try:
                cols = row.find_all('td')
                if len(cols) >= 4:
                    date = cols[0].get_text(strip=True)
                    media_type = cols[1].get_text(strip=True).lower()
                    location = cols[2].get_text(strip=True)
                    
                    download_span = cols[3].find('span', class_='require-js-enabled')
                    if download_span:
                        download_link = download_span.find('a', onclick=True)
                        if download_link:
                            onclick_js = download_link.get('onclick', '')
                            url_match = re.search(r"downloadMemories\('([^']+)'", onclick_js)
                            if url_match:
                                download_url = url_match.group(1)
                                memory = {
                                    'date': date,
                                    'media_type': media_type,
                                    'location': location,
                                    'download_url': download_url,
                                    'is_get_request': 'true' in onclick_js.lower(),
                                    'year': self.extract_year(date),
                                    'index': len(memories) + 1
                                }
                                memories.append(memory)
            except Exception as e:
                continue
        
        return memories

    def extract_year(self, date_str: str) -> int:
        """Extract year from date string"""
        try:
            if 'UTC' in date_str:
                date_str = date_str.replace(' UTC', '')
            dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
            return dt.year
        except:
            return 0

    def analyze_memories(self, memories: List[Dict]) -> Dict:
        """Analyze memories and return statistics"""
        stats = {
            'total': len(memories),
            'images': 0,
            'videos': 0,
            'other': 0,
            'years': {},
        }
        
        for memory in memories:
            media_type = memory['media_type']
            year = memory['year']
            
            if 'image' in media_type:
                stats['images'] += 1
            elif 'video' in media_type:
                stats['videos'] += 1
            else:
                stats['other'] += 1
            
            if year > 0:
                stats['years'][year] = stats['years'].get(year, 0) + 1
        
        return stats

    async def download_memory(self, session: aiohttp.ClientSession, memory: Dict, temp_dir: str) -> Optional[Dict]:
        """Download a single memory file"""
        for attempt in range(3):
            try:
                url = memory['download_url']
                
                safe_date = memory['date'].replace(':', '-').replace(' ', '_').replace(' UTC', '')
                extension = '.mp4' if 'video' in memory['media_type'] else '.jpg'
                filename = f"{safe_date}_{memory['media_type']}{extension}"
                filepath = os.path.join(temp_dir, filename)
                
                headers = {}
                if memory['is_get_request']:
                    headers['X-Snap-Route-Tag'] = 'mem-dmd'
                
                async with session.get(url, headers=headers, timeout=self.timeout) as response:
                    if response.status == 200:
                        content = await response.read()
                        with open(filepath, 'wb') as f:
                            f.write(content)
                        memory['filepath'] = filepath
                        return memory
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
        return None

    async def upload_to_telegram(self, memory: Dict, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Upload a single memory to Telegram"""
        for attempt in range(3):
            try:
                caption = f"📅 {memory['date']}\n📹 {memory['media_type'].title()}"
                
                location = memory['location']
                if location and '0.0, 0.0' not in location:
                    if 'Latitude, Longitude:' in location:
                        coords = location.replace('Latitude, Longitude:', '').strip()
                        caption += f"\n📍 {coords}"
                
                if 'video' in memory['media_type']:
                    with open(memory['filepath'], 'rb') as video_file:
                        await update.message.reply_video(
                            video=video_file,
                            caption=caption,
                            supports_streaming=True
                        )
                else:
                    with open(memory['filepath'], 'rb') as photo_file:
                        await update.message.reply_photo(
                            photo=photo_file,
                            caption=caption
                        )
                return True
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue
        return False

# Create Flask app
app = Flask(__name__)

# Initialize bot
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
application = None
processor = SnapchatMemoryProcessor()

if BOT_TOKEN:
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        print("✅ Bot application initialized")
    except Exception as e:
        print(f"❌ Failed to initialize bot: {e}")

def get_user_session(user_id: int) -> UserSession:
    """Get or create user session"""
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    return user_sessions[user_id]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Snapchat Memories Bot*\n\n"
        "Send me your Snapchat data export HTML file and I'll upload all your memories here!",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 *How to use:*\n"
        "1. Go to Snapchat → Settings → My Data\n"
        "2. Request your data and download HTML file\n"
        "3. Send that HTML file to me\n"
        "4. I'll upload all your memories here\n\n"
        "Commands: /start, /help, /stop, /status",
        parse_mode='Markdown'
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_session = get_user_session(update.effective_user.id)
    if user_session.is_processing:
        user_session.should_stop = True
        await update.message.reply_text("🛑 Stopping process...")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_session = get_user_session(update.effective_user.id)
    if user_session.is_processing:
        progress = f"Progress: {user_session.processed_count}/{len(user_session.memories)}"
        await update.message.reply_text(f"📊 {progress}\n✅ Success: {user_session.success_count}")
    else:
        await update.message.reply_text("ℹ️ No active process")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_session = get_user_session(update.effective_user.id)
    
    if user_session.is_processing:
        await update.message.reply_text("⚠️ Please wait for current process to complete")
        return

    document = update.message.document
    if not document.file_name.lower().endswith('.html'):
        await update.message.reply_text("❌ Please send an HTML file")
        return

    file = await context.bot.get_file(document.file_id)
    temp_path = None
    
    try:
        with tempfile.NamedTemporaryFile(mode='w+b', suffix='.html', delete=False) as temp_file:
            temp_path = temp_file.name
        
        await file.download_to_drive(temp_path)
        
        with open(temp_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        await process_snapchat_file(html_content, update, context, user_session)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_session = get_user_session(update.effective_user.id)
    
    if user_session.memories and len(user_session.memories) > 100:
        if update.message.text.lower() in ['yes', 'y', 'continue']:
            await start_upload_process(update, context, user_session)
            return
        elif update.message.text.lower() in ['no', 'n', 'stop']:
            await update.message.reply_text("❌ Cancelled")
            user_session.reset()
            return
    
    await update.message.reply_text("Please send me your Snapchat HTML file")

async def process_snapchat_file(html_content: str, update: Update, context: ContextTypes.DEFAULT_TYPE, user_session: UserSession):
    try:
        await update.message.reply_text("🔍 Analyzing file...")
        
        user_session.memories = processor.parse_html_file(html_content)
        
        if not user_session.memories:
            await update.message.reply_text("❌ No memories found")
            return
        
        stats = processor.analyze_memories(user_session.memories)
        stats_text = f"✅ Found {stats['total']} memories!\n📊 Images: {stats['images']}, Videos: {stats['videos']}"
        
        await update.message.reply_text(stats_text)
        
        if stats['total'] > 100:
            await update.message.reply_text(f"⚠️ {stats['total']} memories found. Reply 'yes' to continue")
        else:
            await start_upload_process(update, context, user_session)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")

async def start_upload_process(update: Update, context: ContextTypes.DEFAULT_TYPE, user_session: UserSession):
    user_session.is_processing = True
    user_session.should_stop = False
    user_session.start_time = time.time()
    
    try:
        progress_msg = await update.message.reply_text("⏳ Starting upload...")
        user_session.processing_message = progress_msg
        
        with tempfile.TemporaryDirectory() as temp_dir:
            await process_memories_sequential(update, context, user_session, temp_dir)
        
        await send_final_summary(update, user_session)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)}")
    finally:
        user_session.reset()

async def process_memories_sequential(update: Update, context: ContextTypes.DEFAULT_TYPE, user_session: UserSession, temp_dir: str):
    async with aiohttp.ClientSession() as session:
        for i, memory in enumerate(user_session.memories):
            if user_session.should_stop:
                await update.message.reply_text("🛑 Stopped")
                break
            
            user_session.current_index = i + 1
            
            # Update progress every 10 files
            if i % 10 == 0:
                progress = f"Progress: {i+1}/{len(user_session.memories)}"
                await user_session.processing_message.edit_text(f"📤 Uploading...\n{progress}")
            
            downloaded_memory = await processor.download_memory(session, memory, temp_dir)
            
            if downloaded_memory:
                success = await processor.upload_to_telegram(downloaded_memory, update, context)
                if success:
                    user_session.success_count += 1
                    if 'image' in memory['media_type']:
                        user_session.stats['images'] += 1
                    else:
                        user_session.stats['videos'] += 1
                else:
                    user_session.failed_count += 1
            else:
                user_session.failed_count += 1
            
            user_session.processed_count += 1
            await asyncio.sleep(1)  # Rate limiting
            
            # Cleanup
            if downloaded_memory and 'filepath' in downloaded_memory:
                try:
                    os.unlink(downloaded_memory['filepath'])
                except:
                    pass

async def send_final_summary(update: Update, user_session: UserSession):
    elapsed = time.time() - user_session.start_time
    summary = f"""
🎉 Backup Complete!

📊 Statistics:
✅ Total: {len(user_session.memories)}
✅ Success: {user_session.success_count}
❌ Failed: {user_session.failed_count}
⏰ Time: {int(elapsed)}s

📁 Breakdown:
• Images: {user_session.stats['images']}
• Videos: {user_session.stats['videos']}
"""
    await update.message.reply_text(summary)

# Add handlers
if application:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Flask routes
@app.route('/')
def index():
    bot_status = "❌ Not initialized"
    if application and application.bot:
        bot_status = "✅ Bot is running"
    
    return f"""
    <h1>Snapchat Memories Bot</h1>
    <p>Status: {bot_status}</p>
    <p><a href="/set_webhook">Set Webhook</a> | <a href="/health">Health</a></p>
    """

@app.route('/webhook', methods=['POST'])
def webhook():
    if not application:
        return "Bot not initialized", 500
        
    json_str = request.get_data().decode('UTF-8')
    update = Update.de_json(json.loads(json_str), application.bot)
    application.update_queue.put(update)
    return 'OK'

@app.route('/health')
def health():
    return 'OK'

@app.route('/set_webhook')
def set_webhook():
    if not application:
        return "Bot not initialized", 500
        
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    if not webhook_url:
        return "URL not set", 500
    
    # Run in background thread to avoid event loop issues
    def set_webhook_thread():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(application.bot.delete_webhook())
            result = loop.run_until_complete(application.bot.set_webhook(
                url=f"{webhook_url}/webhook",
                drop_pending_updates=True
            ))
            print(f"Webhook set: {result}")
        finally:
            loop.close()
    
    thread = threading.Thread(target=set_webhook_thread)
    thread.start()
    thread.join()
    
    return f"Webhook set to: {webhook_url}/webhook"

# Initialize webhook on startup
if application and os.getenv('RENDER_EXTERNAL_URL'):
    print("🚀 Setting up webhook...")
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    
    def init_webhook():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(application.bot.delete_webhook())
            result = loop.run_until_complete(application.bot.set_webhook(
                url=f"{webhook_url}/webhook",
                drop_pending_updates=True
            ))
            print(f"✅ Webhook set: {result}")
        except Exception as e:
            print(f"❌ Webhook setup failed: {e}")
        finally:
            loop.close()
    
    # Run in background
    import threading
    thread = threading.Thread(target=init_webhook)
    thread.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🤖 Starting bot on port {port}")
    app.run(host='0.0.0.0', port=port)
