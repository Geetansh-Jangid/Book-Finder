# ==============================================================================
#                 Discord Book Finder Bot using discord.py v2.0+
# ==============================================================================
# This version includes a Flask web server for compatibility with hosting
# services and uses the latest recommended methods for all libraries.
# ==============================================================================
#
# REQUIRED LIBRARIES: discord.py, python-dotenv, requests, beautifulsoup4, arxiv, Flask
# INSTALL THEM WITH: pip install discord.py python-dotenv requests beautifulsoup4 arxiv Flask
#
# ==============================================================================

import discord
from discord import app_commands
from discord.ui import Button, View, Select
import os
from dotenv import load_dotenv
import asyncio

import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlencode
import arxiv

from flask import Flask
from threading import Thread

# --- ASYNCHRONOUS UTILITY AND SCRAPER FUNCTIONS ---

def _blocking_get_download_link(mirror_url):
    """Synchronous part of get_download_link to be run in a thread."""
    if mirror_url == "N/A": return None
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'}
        mirror_response = requests.get(mirror_url, headers=headers, timeout=20)
        mirror_response.raise_for_status()
        mirror_soup = BeautifulSoup(mirror_response.content, 'html.parser')
        link_tag = mirror_soup.find('a', href=lambda href: href and 'get.php?md5=' in href)
        if link_tag: return urljoin(mirror_url, link_tag['href'])
        return None
    except Exception as e:
        print(f"[DOWNLOADER_ERROR] An exception occurred: {e}")
        return None

async def get_download_link(mirror_url):
    """Runs the blocking download link fetcher in a separate thread."""
    return await asyncio.to_thread(_blocking_get_download_link, mirror_url)


def _blocking_search_books(query, preferred_format, page):
    """Synchronous part of search_books to be run in a thread."""
    base_url = "https://libgen.li/index.php"
    params = {'req': query, 'page': page, 'res': 100} 
    search_url = f"{base_url}?{urlencode(params)}"
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36'}
        response = requests.get(search_url, headers=headers, timeout=20)
        response.raise_for_status()
    except Exception as e:
        print(f"[SCRAPER_ERROR] Failed to fetch search results: {e}")
        return []
        
    soup = BeautifulSoup(response.content, 'html.parser')
    results_table = soup.find('table', id='tablelibgen')
    if not results_table: return []
    
    book_rows = results_table.find('tbody').find_all('tr')
    books_found = []
    
    for row in book_rows:
        cells = row.find_all('td')
        if len(cells) >= 9:
            title_text = "N/A"
            title_b_tag = cells[0].find('b')
            if title_b_tag: title_text = title_b_tag.get_text(' ', strip=True)
            else:
                title_a_tag = cells[0].find('a')
                if title_a_tag: title_text = title_a_tag.get_text(strip=True)

            mirror_links, mirror_page_url, final_link_url = cells[8].find_all('a'), "N/A", None
            for link in mirror_links:
                href = link.get('href', '')
                if 'get.php' in href:
                    final_link_url = urljoin(base_url, href)
                    break
            if not final_link_url and mirror_links:
                mirror_page_url = urljoin(base_url, mirror_links[0]['href'])
            
            books_found.append({
                "Title": title_text, "Author": cells[1].get_text(strip=True),
                "Size": cells[6].get_text(strip=True), "Extension": cells[7].get_text(strip=True).lower(),
                "Mirror_Page": mirror_page_url, "Final_Link": final_link_url,
            })
    
    if preferred_format: books_found.sort(key=lambda book: book['Extension'] == preferred_format, reverse=True)
    return books_found

async def search_books(query, preferred_format=None, page=1):
    """Runs the blocking book search in a separate thread."""
    return await asyncio.to_thread(_blocking_search_books, query, preferred_format, page)


# --- DISCORD UI CLASSES ---

class BookSearchView(View):
    def __init__(self, query, preferred_format, author):
        super().__init__(timeout=300)
        self.query, self.preferred_format, self.author = query, preferred_format, author
        self.current_page, self.page_size, self.books = 1, 5, []
        self.has_more_results = True

    async def create_embed(self):
        embed = discord.Embed(title=f"Book Results for '{self.query}'", description=f"Showing page {self.current_page}.", color=discord.Color.blue())
        start_index, end_index = (self.current_page - 1) * self.page_size, self.current_page * self.page_size
        
        while len(self.books) < end_index and self.has_more_results:
            scraper_page = (len(self.books) // 100) + 1
            new_books = await search_books(self.query, self.preferred_format, page=scraper_page)
            if not new_books or len(new_books) < 100: self.has_more_results = False
            if new_books: self.books.extend(new_books)
            else: break
            
        current_page_books = self.books[start_index:end_index]
        if not current_page_books:
            embed.description, self.select_menu.disabled, self.next_button.disabled = "No more results found.", True, True
            return embed
            
        self.select_menu.options = [discord.SelectOption(label=f"{start_index + i + 1}. {book['Title'][:80]}", description=f"{book['Author'][:50]} [{book['Extension']}, {book['Size']}]", value=str(start_index + i)) for i, book in enumerate(current_page_books)]
        self.next_button.disabled = len(self.books) <= end_index and not self.has_more_results
        return embed

    @discord.ui.select(placeholder="Choose a book to get its download link...")
    async def select_menu(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user != self.author: return await interaction.response.send_message("This is not your search menu!", ephemeral=True)
        await interaction.response.defer()
        
        book = self.books[int(select.values[0])]
        final_link = book.get('Final_Link') or await get_download_link(book['Mirror_Page'])
        safe_title = discord.utils.escape_markdown(book['Title'])
        
        if final_link: await interaction.followup.send(f"✅ Here is the link for **{safe_title}**:\n[{safe_title}]({final_link})")
        else: await interaction.followup.send(f"❌ Could not find a valid download link for **{safe_title}**.")

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.grey, disabled=True)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return
        if self.current_page > 1: self.current_page -= 1
        button.disabled = self.current_page == 1
        await interaction.response.edit_message(embed=await self.create_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return
        self.current_page += 1
        self.prev_button.disabled = False
        await interaction.response.edit_message(embed=await self.create_embed(), view=self)

class PaperSearchView(View):
    def __init__(self, query, author):
        super().__init__(timeout=300)
        self.query, self.author = query, author
        self.current_page, self.page_size, self.papers = 1, 5, []
        self.search_done = False

    # FIX: Use the new Client.results() method to avoid deprecation warning
    def _blocking_search_papers(self):
        """Synchronous arXiv search to be run in a thread."""
        client = arxiv.Client()
        search = arxiv.Search(
            query=self.query,
            max_results=50
        )
        results_generator = client.results(search)
        return list(results_generator)

    async def create_embed(self):
        embed = discord.Embed(title=f"arXiv Paper Results for '{self.query}'", description=f"Showing page {self.current_page}.", color=discord.Color.orange())
        if not self.search_done:
            self.papers = await asyncio.to_thread(self._blocking_search_papers)
            self.search_done = True

        start_index, end_index = (self.current_page - 1) * self.page_size, self.current_page * self.page_size
        current_page_papers = self.papers[start_index:end_index]

        if not current_page_papers:
            embed.description, self.select_menu.disabled, self.next_button.disabled = "No more results found.", True, True
            return embed
            
        def format_authors(authors):
            names = [author.name for author in authors]
            return f"{names[0]}, et al." if len(names) > 1 else names[0]

        self.select_menu.options = [discord.SelectOption(label=f"{start_index + i + 1}. {paper.title[:80]}", description=f"by {format_authors(paper.authors)}", value=str(start_index + i)) for i, paper in enumerate(current_page_papers)]
        self.next_button.disabled = len(self.papers) <= end_index
        return embed

    @discord.ui.select(placeholder="Choose a paper to get its PDF link...")
    async def select_menu(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user != self.author: return await interaction.response.send_message("This is not your search menu!", ephemeral=True)
        await interaction.response.defer()
        paper = self.papers[int(select.values[0])]
        safe_title = discord.utils.escape_markdown(paper.title)
        await interaction.followup.send(f"✅ Here is the PDF link for **{safe_title}**:\n{paper.pdf_url}")

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.grey, disabled=True)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return
        if self.current_page > 1: self.current_page -= 1
        button.disabled = self.current_page == 1
        await interaction.response.edit_message(embed=await self.create_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.primary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.author: return
        self.current_page += 1
        self.prev_button.disabled = False
        await interaction.response.edit_message(embed=await self.create_embed(), view=self)

# --- BOT SETUP AND COMMANDS ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN: raise ValueError("DISCORD_TOKEN not found in .env file!")

class BookFinderBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
    async def setup_hook(self):
        await self.tree.sync()

intents = discord.Intents.default()
client = BookFinderBot(intents=intents)

@client.event
async def on_ready():
    print(f"--- Logged in as {client.user} ---")

@client.tree.command(name="help", description="Shows information about the bot's commands.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot Help & Commands", color=discord.Color.from_rgb(70, 130, 180))
    embed.add_field(name="/findbook `query` `[preferred_format]`", value="Searches the digital library for a book by title.", inline=False)
    embed.add_field(name="/findpapers `query`", value="Searches arXiv.org for academic papers.", inline=False)
    embed.set_footer(text="Bot made by Geetansh Jangid")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.tree.command(name="findbook", description="Search for a book from the digital library.")
async def findbook(interaction: discord.Interaction, query: str, preferred_format: str = None):
    await interaction.response.defer()
    view = BookSearchView(query=query, preferred_format=preferred_format.lower().strip() if preferred_format else None, author=interaction.user)
    embed = await view.create_embed()
    if not view.books: await interaction.followup.send("Sorry, no results found for your book query.")
    else: await interaction.followup.send(embed=embed, view=view)

@client.tree.command(name="findpapers", description="Search for an academic paper on arXiv.org.")
async def findpapers(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    try:
        view = PaperSearchView(query=query, author=interaction.user)
        embed = await view.create_embed()
        if not view.papers: await interaction.followup.send("Sorry, no results found for your paper query on arXiv.")
        else: await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        print(f"[COMMAND_ERROR] An error occurred during /findpapers: {e}")
        await interaction.followup.send("An error occurred while trying to search for papers.")

# --- FLASK WEB SERVER FOR HOSTING ---
app = Flask(__name__)
@app.route('/')
def home():
    return "The bot is running and ready to find books and papers!"

def run_web_server():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

# --- Run the bot and the web server ---
if __name__ == "__main__":
    try:
        server_thread = Thread(target=run_web_server)
        server_thread.daemon = True
        server_thread.start()
        client.run(TOKEN)
    except Exception as e:
        print(f"[BOT_ERROR] An unexpected error occurred while running the bot: {e}")
