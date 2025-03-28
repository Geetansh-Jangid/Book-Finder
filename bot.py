# --- START OF FILE bot.py ---

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput # Removed Select as it wasn't used
import os
import asyncio
import logging
from dotenv import load_dotenv
import math # For calculating pages
import urllib.parse

# --- Web Server Imports for Render ---
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
# --- End Web Server Imports ---

# Import our adapted LibGen functions
import glib_adapter as glib
import requests # Need requests to create session

# --- Bot Setup ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise ValueError("DISCORD_TOKEN not found in .env file!")

# Define intents
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# Configure logging
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)
log_formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')

# File handler
file_handler = logging.FileHandler(filename='discord_bot.log', encoding='utf-8', mode='w')
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

# Console handler (optional, good for seeing logs during development/deployment)
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)


# Cache setup
search_results_cache = {}
MAX_CACHE_SIZE = 100
RESULTS_PER_PAGE = glib.MAX_RESULTS_TO_SHOW_DISCORD

# --- Web Server for Render ---
# A simple handler that just returns 200 OK
class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running")

def run_http_server():
    port = int(os.getenv("PORT", 10000)) # Render provides PORT env var
    server_address = ('', port)
    try:
        httpd = HTTPServer(server_address, SimpleHandler)
        logger.info(f"Starting simple HTTP server on port {port}...")
        httpd.serve_forever()
    except Exception as e:
        logger.error(f"HTTP server failed: {e}", exc_info=True)
# --- End Web Server Setup ---


# --- Helper Functions ---
def cleanup_cache():
    while len(search_results_cache) > MAX_CACHE_SIZE:
        try:
            oldest_key = next(iter(search_results_cache))
            del search_results_cache[oldest_key]
            logger.info(f"Cache cleanup: Removed oldest entry {oldest_key}")
        except StopIteration:
            break

def create_results_embed(results_page: list, query: str, category_name: str, page_num: int, total_pages: int, total_results: int):
    embed = discord.Embed(
        title=f"Search Results for '{query}' ({category_name})",
        description=f"Showing results {page_num * RESULTS_PER_PAGE + 1} - {min((page_num + 1) * RESULTS_PER_PAGE, total_results)} of {total_results} found.",
        color=discord.Color.blue()
    )
    footer_text = "Disclaimer: Download responsibly."
    if total_pages > 1:
        footer_text = f"Page {page_num + 1}/{total_pages} | {footer_text}"
    embed.set_footer(text=footer_text)

    for i, item in enumerate(results_page):
        display_index = page_num * RESULTS_PER_PAGE + i + 1
        title = item.get('title', 'N/A')
        authors = item.get('authors', 'N/A')
        year = f" ({item.get('year', 'N/A')})" if item.get('year', 'N/A') != 'N/A' else ""
        ext = item.get('extension', 'N/A')
        size = item.get('size', 'N/A')
        lang = item.get('language', 'N/A')
        publisher = item.get('publisher') or item.get('journal', 'N/A')

        field_value = f"**Author(s):** {authors}{year}\n"
        field_value += f"**Format:** {ext} | **Size:** {size} | **Lang:** {lang}\n"
        if publisher != 'N/A':
            field_value += f"**Publisher/Journal:** {publisher}\n"
        if item.get('doi', 'N/A') != 'N/A':
             field_value += f"**DOI:** {item['doi']}\n"

        embed.add_field(name=f"{display_index}. {title}", value=field_value, inline=False)

    return embed

# --- UI Components ---
class ResultsView(View):
    def __init__(self, full_results: list, query: str, category_name: str, current_page_index: int = 0, message_id: int = None):
        super().__init__(timeout=600)
        self.full_results = full_results
        self.query = query
        self.category_name = category_name
        self.current_page_index = current_page_index
        self.message_id = message_id
        self.message = None
        self.total_results = len(full_results)
        self.total_pages = math.ceil(self.total_results / RESULTS_PER_PAGE)

        start_index = self.current_page_index * RESULTS_PER_PAGE
        end_index = start_index + RESULTS_PER_PAGE
        self.current_page_results = self.full_results[start_index:end_index]

        # Add download buttons
        for i, _ in enumerate(self.current_page_results):
            absolute_index = start_index + i
            button = Button(
                label=f"Download {absolute_index + 1}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"download_{absolute_index}"
            )
            button.callback = self.download_callback
            self.add_item(button)

        # Add pagination buttons
        if self.current_page_index > 0:
            prev_button = Button(label="⬅️ Previous", style=discord.ButtonStyle.primary, custom_id="prev_page")
            prev_button.callback = self.change_page_callback
            self.add_item(prev_button)
        if end_index < self.total_results:
            next_button = Button(label="More Options ➡️", style=discord.ButtonStyle.primary, custom_id="next_page")
            next_button.callback = self.change_page_callback
            self.add_item(next_button)

    async def change_page_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        next_page_index = self.current_page_index + 1 if custom_id == "next_page" else self.current_page_index - 1

        if 0 <= next_page_index < self.total_pages:
            cached_results = search_results_cache.get(interaction.message.id)
            if not cached_results:
                 await interaction.response.send_message("Sorry, the original search data has expired. Please search again.", ephemeral=True)
                 try: await interaction.message.edit(view=None)
                 except Exception: pass
                 return

            new_page_results = cached_results[next_page_index * RESULTS_PER_PAGE : (next_page_index + 1) * RESULTS_PER_PAGE]
            new_embed = create_results_embed(new_page_results, self.query, self.category_name, next_page_index, self.total_pages, self.total_results)
            new_view = ResultsView(cached_results, self.query, self.category_name, next_page_index, interaction.message.id)
            new_view.message = interaction.message # Pass message context

            await interaction.response.edit_message(embed=new_embed, view=new_view)
        else:
            await interaction.response.send_message("Invalid page requested.", ephemeral=True)


    async def download_callback(self, interaction: discord.Interaction):
        custom_id = interaction.data['custom_id']
        try:
            absolute_index = int(custom_id.split('_')[1])
            cached_results = search_results_cache.get(interaction.message.id)
            if not cached_results:
                await interaction.response.send_message("Sorry, the original search data has expired. Please search again.", ephemeral=True)
                try: await interaction.message.edit(view=None)
                except Exception: pass
                return

            if 0 <= absolute_index < len(cached_results):
                selected_item = cached_results[absolute_index]
            else:
                await interaction.response.send_message("Invalid selection index.", ephemeral=True)
                return

        except (ValueError, IndexError, TypeError) as e:
            logger.error(f"Error processing download selection: custom_id={custom_id}, message_id={interaction.message.id}, Error: {e}", exc_info=True)
            await interaction.response.send_message("Error processing selection.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=False)
        session = glib.create_session()

        try:
            target_url = selected_item.get('target_url')
            title = selected_item.get('title', 'Unknown Title')
            extension = selected_item.get('extension', 'bin')

            if not target_url:
                await interaction.followup.send("❌ Error: No target URL found for this item.", ephemeral=True)
                return

            logger.info(f"Getting download link for '{title}' from {target_url}")
            direct_download_url, error_msg = await asyncio.to_thread(
                glib.get_libgen_download_link, session, target_url
            )

            if error_msg:
                await interaction.followup.send(f"❌ Error getting download link: {error_msg}", ephemeral=False)
                return
            if not direct_download_url:
                 await interaction.followup.send(f"❌ Could not find a valid download link for '{title}'.", ephemeral=False)
                 return

            logger.info(f"Attempting download of '{title}' from {direct_download_url}")
            filepath, error_msg = await asyncio.to_thread(
                glib.download_book, session, direct_download_url, title, extension
            )

            if error_msg:
                 if "File is too large" in error_msg:
                      await interaction.followup.send(f"⚠️ {error_msg}", ephemeral=False)
                 else:
                      await interaction.followup.send(f"❌ Error downloading: {error_msg}", ephemeral=False)
                 return

            if filepath and os.path.exists(filepath):
                try:
                    logger.info(f"Download successful, sending file: {filepath}")
                    discord_filename = os.path.basename(filepath)
                    if len(discord_filename) > 80:
                        base, ext = os.path.splitext(discord_filename)
                        discord_filename = base[:75] + '...' + ext

                    await interaction.followup.send(f"✅ Download complete for **'{title}'**!", file=discord.File(filepath, filename=discord_filename), ephemeral=False)
                except discord.HTTPException as e:
                     logger.error(f"Failed to upload file to Discord: {e}")
                     await interaction.followup.send(f"⚠️ Could not upload the file to Discord (it might be slightly too large or another issue occurred).\n"
                                                      f"Try downloading directly: {direct_download_url}", ephemeral=False)
                finally:
                    await asyncio.to_thread(glib._cleanup_incomplete_file, filepath)
            else:
                 await interaction.followup.send("❌ Download seemed complete, but the file was not found.", ephemeral=False)

        except Exception as e:
            logger.error(f"Unexpected error during download callback for '{selected_item.get('title', 'N/A')}': {e}", exc_info=True)
            await interaction.followup.send("An unexpected error occurred during the download process.", ephemeral=True)
        finally:
            session.close()

    async def on_timeout(self):
        if self.message_id and self.message:
             try:
                 await self.message.edit(view=None)
                 logger.info(f"ResultsView timed out for message {self.message_id}. Buttons removed.")
             except discord.NotFound: logger.warning(f"Could not find original message {self.message_id} to disable timed-out view.")
             except discord.Forbidden: logger.warning(f"Missing permissions to edit message {self.message_id} on view timeout.")
             except Exception as e: logger.error(f"Error editing message on view timeout: {e}", exc_info=True)
        elif self.message_id: logger.warning(f"Message context not available for timeout handling of message {self.message_id}. Cannot remove buttons.")

        if self.message_id in search_results_cache:
            try: del search_results_cache[self.message_id]; logger.info(f"Removed cached results for timed-out message {self.message_id}")
            except KeyError: pass

# --- Slash Command Definitions ---

@bot.event
async def on_ready():
    logger.info(f'Logged in as {bot.user.name} ({bot.user.id})')
    try:
        synced = await bot.tree.sync()
        logger.info(f"Synced {len(synced)} command(s)")
        os.makedirs(glib.DOWNLOAD_FOLDER, exist_ok=True)
    except Exception as e:
        logger.error(f"Error syncing commands: {e}")
    print("--- Book Finder Bot Ready ---")

# --- /help Command ---
@bot.tree.command(name="help", description="Shows information about how to use the bot.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True) # Allow in DMs, Guilds, Private Channels
@app_commands.user_install() # Allow users to install for themselves
async def help_command(interaction: discord.Interaction):
    """Displays the help message."""
    embed = discord.Embed(
        title="📚 Book Finder Bot Help",
        description="This bot helps you search for books, articles, and fiction on Library Genesis.",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="How to Use",
        value=(
            f"1. Use the `/search` command.\n" # Changed from /findbook
            f"2. Select a `category` (Non-Fiction, Fiction, Articles, Magazines).\n"
            f"3. Enter your `query` (book title, author, DOI, etc.).\n"
            f"4. The bot will show results. If you see your item, click the corresponding `Download X` button.\n"
            f"5. If there are many results, use the `More Options ➡️` and `⬅️ Previous` buttons to navigate pages.\n"
            f"6. For Magazines, the bot provides a link to search results on [magzdb.org](http://magzdb.org/)."
        ), inline=False
    )
    embed.add_field(
        name="🔍 Search Tips",
        value=(
            "- **Be specific:** Use full titles or ISBNs if possible.\n"
            "- **Try Author:** If title search fails, try searching just the author's last name.\n"
            "- **Check Category:** Ensure you're searching in the correct category.\n"
            "- **Use DOI:** For scientific articles, searching by DOI is often most reliable.\n"
            "- **Simplify:** If a complex title doesn't work, try using only the main keywords.\n"
            "- **Check Spelling:** Double-check your spelling and punctuation."
        ), inline=False
    )
    embed.add_field(
        name="⚠️ Important Warning: Copyright",
        value=(
            "**Please use this bot responsibly.** Downloading copyrighted materials without permission may be illegal in your country. "
            "Respect copyright laws and the rights of creators and publishers. This tool is provided for informational purposes; "
            "the developers are not responsible for how it is used."
        ), inline=False
    )
    embed.add_field(name="Credits", value="This bot was created by **Geetansh Jangid**.", inline=False)
    embed.set_footer(text="Happy searching!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- /search Command (Renamed from /findbook) ---
@bot.tree.command(name="search", description="Search for books or articles on Library Genesis.") # Renamed command
@app_commands.describe(
    category="Select the search category",
    query="Enter your search query (book title, author, DOI, etc.)"
)
@app_commands.choices(category=[
    app_commands.Choice(name="📚 Non-Fiction / Sci-Tech", value="libgen"),
    app_commands.Choice(name="📖 Fiction", value="fiction"),
    app_commands.Choice(name="🔬 Scientific Articles", value="scimag"),
    app_commands.Choice(name="📰 Magazines (Link Only)", value="magz"),
])
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True) # Allow in DMs, Guilds, Private Channels
@app_commands.user_install() # Allow users to install for themselves
async def search_command(interaction: discord.Interaction, category: app_commands.Choice[str], query: str): # Renamed function
    """Handles the /search command."""

    search_topic = category.value
    category_name = category.name

    # Handle magazines separately
    if search_topic == 'magz':
        if not query:
             await interaction.response.send_message("Please provide a search query for magazines.", ephemeral=True)
             return
        magz_search_url = f"http://magzdb.org/makelist?t={urllib.parse.quote_plus(query)}"
        embed = discord.Embed(
            title="Magazine Search (magzdb.org)",
            description=f"Magazine searches are handled by [magzdb.org]({magz_search_url}).\n"
                        f"Click the link above to view results for **'{query}'**.",
            color=discord.Color.orange()
        )
        embed.set_footer(text="This bot does not directly scrape or download from magzdb.org.")
        await interaction.response.send_message(embed=embed, ephemeral=False)
        return

    # Validate query
    if not query:
        await interaction.response.send_message(f"Please provide a search query for {category_name}.", ephemeral=True)
        return

    # Defer response
    await interaction.response.defer(thinking=True, ephemeral=False)
    session = glib.create_session()

    try:
        # Perform search
        all_results, error_msg = await asyncio.to_thread(
            glib.search_libgen, session, query, search_topic
        )

        # Handle search errors or no results
        if error_msg:
            await interaction.followup.send(f"❌ Error searching: {error_msg}", ephemeral=True)
            return
        if not all_results:
            await interaction.followup.send(f" A search for **'{query}'** in '{category_name}' yielded no results.", ephemeral=False)
            return

        # Prepare results for display
        total_results_found = len(all_results)
        total_pages = math.ceil(total_results_found / RESULTS_PER_PAGE)
        current_page_index = 0
        results_to_show = all_results[:RESULTS_PER_PAGE]

        # Create embed and view
        embed = create_results_embed(results_to_show, query, category_name, current_page_index, total_pages, total_results_found)
        result_view = ResultsView(all_results, query, category_name, current_page_index, message_id=None)

        # Send the message
        followup_message = await interaction.followup.send(embed=embed, view=result_view, ephemeral=False)

        # Cache results and update view with message context
        if followup_message:
             search_results_cache[followup_message.id] = all_results
             cleanup_cache()
             result_view.message_id = followup_message.id
             result_view.message = followup_message

    except Exception as e:
        logger.error(f"Error during /search command execution: {e}", exc_info=True)
        # Use followup.send if interaction is still valid, otherwise try channel.send
        try:
            if not interaction.response.is_done():
                await interaction.followup.send("An unexpected error occurred while processing your search.", ephemeral=True)
            else:
                 await interaction.channel.send("An unexpected error occurred while processing your search command.")
        except Exception as followup_error:
            logger.error(f"Failed to send error message after initial error: {followup_error}")
    finally:
        session.close()


# --- Run the Bot ---
if __name__ == "__main__":
    # Start the HTTP server in a background thread
    http_thread = threading.Thread(target=run_http_server, daemon=True)
    http_thread.start()

    # Start the Discord bot
    logger.info("Starting Discord bot...")
    try:
        bot.run(TOKEN)
    except Exception as e:
        logger.critical(f"Bot failed to run: {e}", exc_info=True)

# --- END OF FILE bot.py ---
