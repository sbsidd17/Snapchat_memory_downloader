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
        """Parse Snapchat HTML file and extract memory download links with better parsing"""
        soup = BeautifulSoup(html_content, 'html.parser')
        memories = []
        
        # Find the memories table
        table = soup.find('table')
        if not table:
            logger.error("No table found in HTML")
            return memories
            
        # Find all table rows (skip header row)
        rows = table.find_all('tr')[1:]  # Skip header row
        
        for row in rows:
            try:
                cols = row.find_all('td')
                if len(cols) >= 4:
                    date = cols[0].get_text(strip=True)
                    media_type = cols[1].get_text(strip=True).lower()
                    location = cols[2].get_text(strip=True)
                    
                    # Find download link in the last column
                    download_span = cols[3].find('span', class_='require-js-enabled')
                    if download_span:
                        download_link = download_span.find('a', onclick=True)
                        if download_link:
                            # Extract URL from onclick attribute
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
                logger.error(f"Error parsing row: {e}")
                continue
        
        return memories

    def extract_year(self, date_str: str) -> int:
        """Extract year from date string"""
        try:
            # Handle different date formats
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
            'by_type': {}
        }
        
        for memory in memories:
            media_type = memory['media_type']
            year = memory['year']
            
            # Count by media type
            if 'image' in media_type:
                stats['images'] += 1
            elif 'video' in media_type:
                stats['videos'] += 1
            else:
                stats['other'] += 1
            
            # Count by year
            if year > 0:
                stats['years'][year] = stats['years'].get(year, 0) + 1
            
            # Count by specific type
            stats['by_type'][media_type] = stats['by_type'].get(media_type, 0) + 1
        
        return stats

    async def download_memory(self, session: aiohttp.ClientSession, memory: Dict, temp_dir: str) -> Optional[Dict]:
        """Download a single memory file with retry logic"""
        for attempt in range(3):
            try:
                url = memory['download_url']
                
                # Create filename based on date and media type
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
                        memory['filesize'] = len(content)
                        return memory
                    else:
                        logger.warning(f"Attempt {attempt + 1} failed for {memory['date']}: Status {response.status}")
                        if attempt < 2:
                            await asyncio.sleep(2)
                            continue
                        return None
                        
            except asyncio.TimeoutError:
                logger.warning(f"Attempt {attempt + 1} timeout for {memory['date']}")
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return None
            except Exception as e:
                logger.warning(f"Attempt {attempt + 1} error for {memory['date']}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
                    continue
                return None
        
        return None

    async def upload_to_telegram(self, memory: Dict, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Upload a single memory to Telegram with retry logic"""
        for attempt in range(3):
            try:
                caption = self.create_caption(memory)
                
                if 'video' in memory['media_type']:
                    with open(memory['filepath'], 'rb') as video_file:
                        await update.message.reply_video(
                            video=video_file,
                            caption=caption,
                            supports_streaming=True,
                            read_timeout=60,
                            write_timeout=60,
                            connect_timeout=60
                        )
                else:
                    with open(memory['filepath'], 'rb') as photo_file:
                        await update.message.reply_photo(
                            photo=photo_file,
                            caption=caption,
                            read_timeout=60,
                            write_timeout=60,
                            connect_timeout=60
                        )
                
                return True
                
            except TimedOut:
                logger.warning(f"Upload timeout for {memory['date']}, attempt {attempt + 1}")
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue
                return False
            except Exception as e:
                logger.warning(f"Upload error for {memory['date']}: {e}, attempt {attempt + 1}")
                if attempt < 2:
                    await asyncio.sleep(3)
                    continue
                return False
        
        return False

    def create_caption(self, memory: Dict) -> str:
        """Create caption for Telegram message"""
        caption_parts = [
            f"📅 {memory['date']}",
            f"📹 {memory['media_type'].title()}"
        ]
        
        location = memory['location']
        if location and '0.0, 0.0' not in location:
            if 'Latitude, Longitude:' in location:
                coords = location.replace('Latitude, Longitude:', '').strip()
                caption_parts.append(f"📍 {coords}")
            else:
                caption_parts.append(f"📍 {location}")
        
        return '\n'.join(caption_parts)

# Create Flask app first
app = Flask(__name__)

# Initialize bot application
BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

# Configure application with longer timeouts
builder = Application.builder().token(BOT_TOKEN)
builder.connect_timeout(60)
builder.read_timeout(60)
builder.write_timeout(60)
builder.pool_timeout(60)

application = builder.build()
processor = SnapchatMemoryProcessor()

def get_user_session(user_id: int) -> UserSession:
    """Get or create user session"""
    if user_id not in user_sessions:
        user_sessions[user_id] = UserSession(user_id)
    return user_sessions[user_id]

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send welcome message when command /start is issued."""
    welcome_text = """
🤖 *Snapchat Memories Bot*

I can help you backup your Snapchat memories to Telegram!

*How to use:*
1. Go to Snapchat app → Settings → My Data
2. Request your data and download the HTML file
3. Send that HTML file to me
4. I'll extract all your memories and upload them here

*Commands:*
/start - Show this welcome message
/help - Detailed instructions
/stop - Stop current upload process
/status - Check current progress

⚠️ *Note:* Download links expire after 7 days, so use fresh data exports.
    """
    await update.message.reply_text(welcome_text, parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send help message when command /help is issued."""
    help_text = """
📖 *Help Guide*

*Steps to get your Snapchat data:*
1. Open Snapchat → Settings (gear icon)
2. Scroll to "Privacy Controls" → "My Data"
3. Tap "Submit Request" → Choose "Memories"
4. Wait for email (usually takes few hours)
5. Download the HTML file from the email
6. Send that HTML file to this bot

*Features:*
✅ Parse Snapchat data export HTML
✅ Download all memories (photos & videos)  
✅ Upload to Telegram with metadata
✅ Progress tracking and statistics
✅ Stop/resume functionality
✅ Year-wise organization

*Privacy:* Files are processed temporarily and not stored.
    """
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop current processing"""
    user_session = get_user_session(update.effective_user.id)
    
    if not user_session.is_processing:
        await update.message.reply_text("ℹ️ No active process to stop.")
        return
    
    user_session.should_stop = True
    await update.message.reply_text("🛑 Stopping process... Please wait.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check current status"""
    user_session = get_user_session(update.effective_user.id)
    
    if not user_session.is_processing or user_session.processed_count == 0:
        await update.message.reply_text("ℹ️ No active process running.")
        return
    
    elapsed = time.time() - user_session.start_time
    progress = user_session.processed_count
    total = len(user_session.memories)
    success = user_session.success_count
    failed = user_session.failed_count
    percentage = (progress / total) * 100 if total > 0 else 0
    
    status_text = f"""
📊 *Current Status*

🔢 Progress: {progress}/{total} ({percentage:.1f}%)
✅ Successful: {success}
❌ Failed: {failed}
⏰ Elapsed: {int(elapsed)}s
📁 Current: {user_session.current_file or 'None'}

🛑 Use /stop to cancel
    """
    await update.message.reply_text(status_text, parse_mode='Markdown')

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle received document (HTML file)."""
    user_session = get_user_session(update.effective_user.id)
    
    if user_session.is_processing:
        await update.message.reply_text("⚠️ Please wait for current process to complete or use /stop to cancel.")
        return

    document = update.message.document
    
    # Check if it's an HTML file
    if not document.file_name.lower().endswith('.html'):
        await update.message.reply_text("❌ Please send an HTML file from Snapchat data export.")
        return

    # Download and process the file
    file = await context.bot.get_file(document.file_id)
    temp_path = None
    
    try:
        with tempfile.NamedTemporaryFile(mode='w+b', suffix='.html', delete=False) as temp_file:
            temp_path = temp_file.name
        
        await file.download_to_drive(temp_path)
        
        with open(temp_path, 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # Process the HTML file
        await process_snapchat_file(html_content, update, context, user_session)
        
    except Exception as e:
        logger.error(f"Error handling document: {e}")
        await update.message.reply_text(f"❌ Error processing file: {str(e)}")
    finally:
        # Clean up temp file
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages."""
    user_session = get_user_session(update.effective_user.id)
    
    # Check if this is a confirmation for large files
    if user_session.memories and len(user_session.memories) > 100:
        if update.message.text.lower() in ['yes', 'y', 'continue']:
            await start_upload_process(update, context, user_session)
            return
        elif update.message.text.lower() in ['no', 'n', 'stop', 'cancel']:
            await update.message.reply_text("❌ Process cancelled.")
            user_session.reset()
            return
    
    await update.message.reply_text(
        "Please send me the HTML file you received from Snapchat data export.\n"
        "Use /help for instructions on how to get your data."
    )

async def process_snapchat_file(html_content: str, update: Update, context: ContextTypes.DEFAULT_TYPE, user_session: UserSession):
    """Process the Snapchat HTML file and show statistics"""
    try:
        # Step 1: Parse HTML
        parsing_msg = await update.message.reply_text("🔍 Analyzing your Snapchat data file...")
        
        user_session.memories = processor.parse_html_file(html_content)
        
        if not user_session.memories:
            await parsing_msg.edit_text("❌ No memories found in the HTML file. Please make sure it's a valid Snapchat data export.")
            return
        
        # Step 2: Show statistics
        stats = processor.analyze_memories(user_session.memories)
        stats_text = format_statistics(stats)
        
        await parsing_msg.edit_text(stats_text)
        
        # Ask for confirmation for large files
        if stats['total'] > 100:
            confirm_msg = await update.message.reply_text(
                f"⚠️ Found {stats['total']} memories. This may take a while.\n"
                "Reply 'yes' to continue or 'no' to cancel."
            )
        else:
            # Start immediately for small files
            await start_upload_process(update, context, user_session)
        
    except Exception as e:
        logger.error(f"Error in process_snapchat_file: {e}")
        await update.message.reply_text(f"❌ Unexpected error: {str(e)}")

async def start_upload_process(update: Update, context: ContextTypes.DEFAULT_TYPE, user_session: UserSession):
    """Start the upload process"""
    user_session.is_processing = True
    user_session.should_stop = False
    user_session.start_time = time.time()
    
    try:
        # Step 3: Start processing
        progress_msg = await update.message.reply_text(
            "⏳ Starting upload process...\n\n"
            "🛑 Use /stop to cancel anytime\n"
            "📊 Use /status to check progress\n\n"
            "⏰ This may take a while for large collections..."
        )
        user_session.processing_message = progress_msg
        
        # Create temporary directory for downloads
        with tempfile.TemporaryDirectory() as temp_dir:
            # Process memories one by one
            await process_memories_sequential(update, context, user_session, temp_dir)
        
        # Final summary
        await send_final_summary(update, user_session)
        
    except Exception as e:
        logger.error(f"Error in start_upload_process: {e}")
        await update.message.reply_text(f"❌ Unexpected error: {str(e)}")
    finally:
        user_session.reset()

async def process_memories_sequential(update: Update, context: ContextTypes.DEFAULT_TYPE, user_session: UserSession, temp_dir: str):
    """Process memories one by one with proper tracking"""
    total_memories = len(user_session.memories)
    
    # Create a single HTTP session for all requests
    async with aiohttp.ClientSession() as session:
        for i, memory in enumerate(user_session.memories):
            if user_session.should_stop:
                await update.message.reply_text("🛑 Process stopped by user.")
                break
            
            user_session.current_index = i + 1
            user_session.current_file = f"{memory['date']} ({memory['media_type']})"
            
            # Update progress
            await update_progress_message(user_session, update)
            
            # Download memory
            downloaded_memory = await processor.download_memory(session, memory, temp_dir)
            
            if downloaded_memory:
                # Upload to Telegram
                success = await processor.upload_to_telegram(downloaded_memory, update, context)
                
                if success:
                    user_session.success_count += 1
                    # Update stats
                    if 'image' in memory['media_type']:
                        user_session.stats['images'] += 1
                    elif 'video' in memory['media_type']:
                        user_session.stats['videos'] += 1
                    else:
                        user_session.stats['other'] += 1
                else:
                    user_session.failed_count += 1
                    user_session.failed_memories.append(memory)
                    logger.error(f"Failed to upload: {memory['date']}")
            else:
                user_session.failed_count += 1
                user_session.failed_memories.append(memory)
                logger.error(f"Failed to download: {memory['date']}")
            
            user_session.processed_count += 1
            
            # Small delay to avoid rate limits (random between 1-3 seconds)
            await asyncio.sleep(random.uniform(1, 3))
            
            # Clean up downloaded file
            if downloaded_memory and 'filepath' in downloaded_memory:
                try:
                    os.unlink(downloaded_memory['filepath'])
                except:
                    pass

async def update_progress_message(user_session: UserSession, update: Update):
    """Update progress message"""
    if not user_session.processing_message:
        return
        
    current = user_session.current_index
    total = len(user_session.memories)
    success = user_session.success_count
    failed = user_session.failed_count
    percentage = (current / total) * 100 if total > 0 else 0
    
    progress_text = (
        f"📤 Uploading memories...\n\n"
        f"🔢 Progress: {current}/{total} ({percentage:.1f}%)\n"
        f"✅ Successful: {success}\n"
        f"❌ Failed: {failed}\n"
        f"📊 Images: {user_session.stats['images']} | Videos: {user_session.stats['videos']}\n"
        f"📁 Current: {user_session.current_file}\n\n"
        f"🛑 Use /stop to cancel\n"
        f"📊 Use /status for details"
    )
    
    try:
        await user_session.processing_message.edit_text(progress_text)
    except Exception as e:
        logger.warning(f"Could not update progress message: {e}")

def format_statistics(stats: Dict) -> str:
    """Format statistics for display"""
    years_text = ""
    if stats['years']:
        years_sorted = sorted(stats['years'].items(), key=lambda x: x[0])
        years_list = [f"{year}: {count}" for year, count in years_sorted]
        years_text = f"📅 Years: {', '.join(years_list)}\n"
    
    return f"""
✅ Found {stats['total']} memories!

📊 Breakdown:
• Images: {stats['images']}
• Videos: {stats['videos']}
• Other: {stats['other']}

{years_text}
🎯 Ready to start upload process?
    """

async def send_final_summary(update: Update, user_session: UserSession):
    """Send final summary after processing"""
    elapsed = time.time() - user_session.start_time
    total = len(user_session.memories)
    success = user_session.success_count
    failed = user_session.failed_count
    
    summary_text = f"""
🎉 *Backup Complete!*

📊 Final Statistics:
✅ Total memories: {total}
✅ Successfully uploaded: {success}
❌ Failed: {failed}
⏰ Time taken: {int(elapsed)} seconds

📁 Breakdown:
• Images: {user_session.stats['images']}
• Videos: {user_session.stats['videos']}
• Other: {user_session.stats['other']}

"""

    if user_session.failed_memories:
        summary_text += f"\n❌ Failed to upload {len(user_session.failed_memories)} memories."
        if len(user_session.failed_memories) <= 10:
            failed_list = "\n".join([f"• {m['date']} ({m['media_type']})" for m in user_session.failed_memories[:10]])
            summary_text += f"\n\nFailed items:\n{failed_list}"
    
    summary_text += "\n\n💡 All successful memories are now safely stored in this chat!"
    
    await update.message.reply_text(summary_text, parse_mode='Markdown')

# Add handlers to application
application.add_handler(CommandHandler("start", start_command))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("stop", stop_command))
application.add_handler(CommandHandler("status", status_command))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# Flask routes
@app.route('/')
def index():
    return "🤖 Snapchat Memories Bot is running! Send /start to your bot on Telegram."

@app.route('/webhook', methods=['POST'])
def webhook():
    """Webhook endpoint for Telegram"""
    json_str = request.get_data().decode('UTF-8')
    update = Update.de_json(json.loads(json_str), application.bot)
    application.update_queue.put(update)
    return 'OK'

@app.route('/health')
def health():
    return 'OK'

@app.route('/set_webhook', methods=['GET'])
def set_webhook():
    """Manually set webhook URL"""
    webhook_url = os.getenv('RENDER_EXTERNAL_URL')
    if webhook_url:
        # Run the async function in a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(application.bot.set_webhook(
            url=f"{webhook_url}/webhook",
            allowed_updates=Update.ALL_TYPES
        ))
        loop.close()
        return f"Webhook set to: {webhook_url}/webhook - Success: {result}"
    return "RENDER_EXTERNAL_URL not set"

# Initialize the bot when the app starts
@app.before_first_request
def initialize_bot():
    """Initialize the bot when the app starts"""
    try:
        webhook_url = os.getenv('RENDER_EXTERNAL_URL')
        if webhook_url:
            # Set webhook for production
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(application.bot.set_webhook(
                url=f"{webhook_url}/webhook",
                allowed_updates=Update.ALL_TYPES
            ))
            loop.close()
            logger.info(f"Webhook set successfully: {result}")
        else:
            logger.info("Running in development mode (no webhook set)")
    except Exception as e:
        logger.error(f"Failed to initialize bot: {e}")

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"🤖 Starting Snapchat Memories Bot on port {port}...")
    
    # Initialize the bot
    initialize_bot()
    
    # Run the Flask app
    app.run(host='0.0.0.0', port=port, debug=False)
