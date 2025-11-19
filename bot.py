import os
import asyncio
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Bot
import aiohttp
from bs4 import BeautifulSoup
import html
import tempfile
import json
from pathlib import Path
from urllib.parse import urlparse
import re
import time

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
REQUEST_TIMEOUT = 60  # Increase timeout to 60 seconds
MAX_RETRIES = 3
RETRY_DELAY = 5

class SnapchatMemoryProcessor:
    def __init__(self):
        self.downloaded_files = set()
        
    def parse_html_file(self, html_content):
        """Parse Snapchat HTML file and extract memory download links"""
        soup = BeautifulSoup(html_content, 'html.parser')
        memories = []
        
        # Find all table rows (skip header row)
        rows = soup.find_all('tr')[1:]  # Skip header row
        
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 4:
                date = cols[0].get_text(strip=True)
                media_type = cols[1].get_text(strip=True)
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
                            memories.append({
                                'date': date,
                                'media_type': media_type,
                                'location': location,
                                'download_url': download_url,
                                'is_get_request': 'true' in onclick_js.lower()
                            })
        
        return memories

    async def download_memory(self, session, memory, temp_dir):
        """Download a single memory file with retry logic"""
        for attempt in range(MAX_RETRIES):
            try:
                url = memory['download_url']
                
                # Create filename based on date and media type
                safe_date = memory['date'].replace(':', '-').replace(' ', '_')
                extension = '.mp4' if memory['media_type'].lower() == 'video' else '.jpg'
                filename = f"{safe_date}_{memory['media_type']}{extension}"
                filepath = os.path.join(temp_dir, filename)
                
                headers = {}
                if memory['is_get_request']:
                    headers['X-Snap-Route-Tag'] = 'mem-dmd'
                
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    if response.status == 200:
                        content = await response.read()
                        
                        with open(filepath, 'wb') as f:
                            f.write(content)
                        
                        memory['filepath'] = filepath
                        memory['filesize'] = len(content)
                        return memory
                    else:
                        logger.error(f"Attempt {attempt + 1} failed to download {url}: Status {response.status}")
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(RETRY_DELAY)
                            continue
                        return None
                        
            except asyncio.TimeoutError:
                logger.error(f"Attempt {attempt + 1} timeout downloading {memory['date']}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return None
            except Exception as e:
                logger.error(f"Attempt {attempt + 1} error downloading memory: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return None

    async def upload_to_telegram(self, memory, update, context):
        """Upload a single memory to Telegram with retry logic"""
        for attempt in range(MAX_RETRIES):
            try:
                caption = f"📅 {memory['date']}\n📹 {memory['media_type']}\n📍 {memory['location']}"
                
                if memory['media_type'].lower() == 'video':
                    with open(memory['filepath'], 'rb') as video_file:
                        await update.message.reply_video(
                            video=video_file,
                            caption=caption,
                            supports_streaming=True,
                            read_timeout=REQUEST_TIMEOUT,
                            write_timeout=REQUEST_TIMEOUT,
                            connect_timeout=REQUEST_TIMEOUT
                        )
                else:
                    with open(memory['filepath'], 'rb') as photo_file:
                        await update.message.reply_photo(
                            photo=photo_file,
                            caption=caption,
                            read_timeout=REQUEST_TIMEOUT,
                            write_timeout=REQUEST_TIMEOUT,
                            connect_timeout=REQUEST_TIMEOUT
                        )
                
                return True
                
            except asyncio.TimeoutError:
                logger.error(f"Attempt {attempt + 1} timeout uploading {memory['date']}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                return False
            except Exception as e:
                logger.error(f"Attempt {attempt + 1} error uploading to Telegram: {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY)
                    continue
                await update.message.reply_text(f"❌ Failed to upload {memory['date']}: {str(e)}")
                return False

class SnapchatBot:
    def __init__(self, token):
        self.token = token
        
        # Configure application with longer timeouts
        builder = Application.builder().token(token)
        
        # Set timeouts for the bot
        builder.connect_timeout(REQUEST_TIMEOUT)
        builder.read_timeout(REQUEST_TIMEOUT)
        builder.write_timeout(REQUEST_TIMEOUT)
        builder.pool_timeout(REQUEST_TIMEOUT)
        
        self.application = builder.build()
        self.processor = SnapchatMemoryProcessor()
        
        # Add handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(MessageHandler(filters.Document.ALL, self.handle_document))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send welcome message when command /start is issued."""
        welcome_text = """
🤖 *Snapchat Memories Bot*

I can help you backup your Snapchat memories to Telegram!

*How to use:*
1. Go to Snapchat app → Settings → My Data
2. Request your data and download the HTML file
3. Send that HTML file to me
4. I'll extract all your memories and upload them here

⚠️ *Note:* Download links expire after 7 days, so make sure to use fresh data exports.

Use /help for more information.
        """
        await update.message.reply_text(welcome_text, parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Send help message when command /help is issued."""
        help_text = """
📖 *Help Guide*

*Steps to get your Snapchat data:*
1. Open Snapchat → Settings (gear icon)
2. Scroll down to "Privacy Controls"
3. Tap "My Data"
4. Select "Submit Request" and choose "Memories"
5. Wait for email (usually takes few hours)
6. Download the HTML file from the email
7. Send that HTML file to this bot

*What I do:*
- Parse your Snapchat data export HTML
- Download all your memories (photos & videos)
- Upload them to this Telegram chat with dates and locations

*Privacy:* Your files are processed temporarily and not stored anywhere.
        """
        await update.message.reply_text(help_text, parse_mode='Markdown')

    async def handle_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle received document (HTML file)."""
        document = update.message.document
        
        # Check if it's an HTML file
        if not document.file_name.lower().endswith('.html'):
            await update.message.reply_text("❌ Please send an HTML file from Snapchat data export.")
            return

        # Download the file
        file = await context.bot.get_file(document.file_id)
        
        with tempfile.NamedTemporaryFile(mode='w+b', suffix='.html', delete=False) as temp_file:
            temp_path = temp_file.name
            
        try:
            await file.download_to_drive(temp_path)
            
            # Read and process the HTML file
            with open(temp_path, 'r', encoding='utf-8') as f:
                html_content = f.read()
            
            # Process the HTML file
            await self.process_snapchat_file(html_content, update, context)
            
        except Exception as e:
            logger.error(f"Error handling document: {e}")
            await update.message.reply_text(f"❌ Error processing file: {str(e)}")
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle text messages."""
        await update.message.reply_text(
            "Please send me the HTML file you received from Snapchat data export. "
            "Use /help for instructions on how to get your data."
        )

    async def process_snapchat_file(self, html_content, update, context):
        """Process the Snapchat HTML file and upload memories."""
        processing_msg = await update.message.reply_text("🔍 Processing your Snapchat data file...")
        
        try:
            # Parse HTML to get memories
            memories = self.processor.parse_html_file(html_content)
            
            if not memories:
                await processing_msg.edit_text("❌ No memories found in the HTML file. Please make sure it's a valid Snapchat data export.")
                return
            
            await processing_msg.edit_text(f"📦 Found {len(memories)} memories! Starting download...")
            
            # Create temporary directory for downloads
            with tempfile.TemporaryDirectory() as temp_dir:
                # Download all memories
                downloaded_memories = []
                connector = aiohttp.TCPConnector(limit=10)  # Limit concurrent connections
                timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                
                async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
                    tasks = []
                    for memory in memories:
                        task = self.processor.download_memory(session, memory, temp_dir)
                        tasks.append(task)
                    
                    downloaded = await asyncio.gather(*tasks)
                    downloaded_memories = [m for m in downloaded if m is not None]
                
                await processing_msg.edit_text(f"✅ Downloaded {len(downloaded_memories)}/{len(memories)} files. Now uploading to Telegram...")
                
                # Upload to Telegram
                success_count = 0
                for i, memory in enumerate(downloaded_memories, 1):
                    status_msg = await update.message.reply_text(f"📤 Uploading {i}/{len(downloaded_memories)}...")
                    
                    try:
                        if await self.processor.upload_to_telegram(memory, update, context):
                            success_count += 1
                        
                        # Delete status message to reduce clutter
                        await status_msg.delete()
                        
                        # Small delay to avoid rate limits
                        await asyncio.sleep(2)
                        
                    except Exception as e:
                        logger.error(f"Error in upload process: {e}")
                        await status_msg.edit_text(f"❌ Upload failed for {memory['date']}")
                
                # Final summary
                summary = f"""
🎉 *Backup Complete!*

✅ Successfully uploaded: {success_count}/{len(memories)}
📊 Total processed: {len(downloaded_memories)}
⏰ All memories are now safely stored in this chat!

💡 *Tip:* You can search for specific dates using Telegram's search feature.
                """
                await update.message.reply_text(summary, parse_mode='Markdown')
                
        except Exception as e:
            logger.error(f"Error processing file: {e}")
            await processing_msg.edit_text(f"❌ Error processing file: {str(e)}")

    def run(self):
        """Start the bot with retry logic."""
        print("Starting bot with improved timeout configuration...")
        
        # Add retry logic for connection issues
        max_retries = 3
        for attempt in range(max_retries):
            try:
                print(f"Attempt {attempt + 1} to start bot...")
                self.application.run_polling(
                    allowed_updates=Update.ALL_TYPES,
                    drop_pending_updates=True,
                    close_loop=False
                )
                break
            except Exception as e:
                print(f"Attempt {attempt + 1} failed: {e}")
                if attempt < max_retries - 1:
                    print(f"Retrying in 10 seconds...")
                    time.sleep(10)
                else:
                    print("All retry attempts failed. Exiting.")
                    raise

# Main execution
if __name__ == '__main__':
    # Get bot token from environment variable
    BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
    
    if not BOT_TOKEN:
        print("Error: Please set TELEGRAM_BOT_TOKEN environment variable")
        print("Get your token from @BotFather on Telegram")
        exit(1)
    
    bot = SnapchatBot(BOT_TOKEN)
    bot.run()
