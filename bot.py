#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import discord
from discord import app_commands
from discord.ui import Select, View
import requests
from bs4 import BeautifulSoup
import time
import os
import re
import urllib.parse
import io # To handle file downloads in memory
import asyncio # For async operations
import threading # To run Flask concurrently
from flask import Flask # For the web server binding

# --- Flask Setup (for Render deployment) ---
app = Flask(__name__)
PORT = int(os.environ.get('PORT', 10000)) # Render provides PORT env var

@app.route('/')
def home():
    # Simple endpoint to confirm the web server is running
    return f"Flask server running for Discord Bot. Bot status: {'Online' if client.is_ready() else 'Starting...'}"

def run_flask():
    # Run Flask app in a separate thread
    # Use '0.0.0.0' to bind to all interfaces, required by Render
    print(f"Starting Flask server on port {PORT}...")
    app.run(host='0.0.0.0', port=PORT)

# --- Configuration ---
LIBGEN_BASE_URL = "https://libgen.is" # Using .is as per original script
SEARCH_URLS = {
    'libgen': LIBGEN_BASE_URL + "/search.php",
    'fiction': LIBGEN_BASE_URL + "/fiction/",
    'scimag': LIBGEN_BASE_URL + "/scimag/",
    # 'magz' is handled differently - redirects user
}
HEADERS = { # Keep headers from original script
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
    'Accept-Language': 'en-US,en;q=0.9', 'Accept-Encoding': 'gzip, deflate, br', 'DNT': '1',
    'Upgrade-Insecure-Requests': '1', 'Sec-Fetch-Dest': 'document', 'Sec-Fetch-Mode': 'navigate',
    'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-User': '?1',
    'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="108", "Google Chrome";v="108"',
    'Sec-Ch-Ua-Mobile': '?0', 'Sec-Ch-Ua-Platform': '"Windows"'
}
MAX_RESULTS_TO_SHOW = 15 # Reduced for Discord UI
DISCORD_FILE_LIMIT_MB = 8 # Standard Discord limit
DISCORD_FILE_LIMIT_BYTES = DISCORD_FILE_LIMIT_MB * 1024 * 1024
BOT_CREATOR = "Geetansh Jangid"
# Load token securely from environment variable
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")

# --- Helper Functions ---
def safe_filename(filename):
    # Keep the original safe_filename function
    try: filename = urllib.parse.unquote(filename)
    except Exception: pass
    filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    filename = filename.replace('/', '_').replace('\\', '_')
    filename = re.sub(r'\.+', '.', filename)
    filename = filename.strip('. ')
    if not filename: filename = "downloaded_file"
    return filename[:200] # Keep length reasonable

# --- Async Scraping/Downloading Functions (Modified for Bot) ---

# Make session global for reuse within async context if desired, or create per request
http_session = requests.Session()
http_session.headers.update(HEADERS)

async def search_libgen_async(query: str, search_topic: str = 'libgen'):
    """Searches LibGen asynchronously, parses results."""
    if search_topic not in SEARCH_URLS:
        print(f"Error: Topic '{search_topic}' not supported.")
        return []

    search_url = SEARCH_URLS[search_topic]
    query_param = 'q' if search_topic in ['fiction', 'scimag'] else 'req'
    print(f"Async Searching LibGen '{search_topic}' for: '{query}'...") # Log for server console

    params = { query_param: query }
    if search_topic == 'libgen': params.update({'lg_topic': 'libgen', 'open': 0, 'view': 'simple', 'res': MAX_RESULTS_TO_SHOW, 'phrase': 1, 'column': 'def'})
    elif search_topic == 'fiction': params['res'] = MAX_RESULTS_TO_SHOW
    elif search_topic == 'scimag': params['res'] = MAX_RESULTS_TO_SHOW

    try:
        # Use asyncio's loop to run synchronous requests code in a separate thread
        loop = asyncio.get_running_loop()
        # Update Referer header just before request
        current_headers = http_session.headers.copy()
        current_headers['Referer'] = LIBGEN_BASE_URL + '/'

        response = await loop.run_in_executor(
            None, # Default executor (ThreadPoolExecutor)
            lambda: http_session.get(search_url, params=params, timeout=30, headers=current_headers)
        )
        response.raise_for_status()
        print(f"  Search status: {response.status_code}") # Log
        await asyncio.sleep(1) # Be nice to the server
    except requests.exceptions.RequestException as e:
        print(f"Error during async search: {e}") # Log
        return []
    except Exception as e:
        print(f"Unexpected error during async search setup: {e}") # Log
        return []

    # --- Parsing logic (mostly identical to original, just watch for blocking) ---
    soup = BeautifulSoup(response.text, 'lxml')
    main_table = None
    table_class = 'catalog' if search_topic in ['fiction', 'scimag'] else 'c'
    results_tables = soup.find_all('table', class_=table_class)

    if not results_tables:
        print(f"Error: No table with class='{table_class}'.") # Log
        return []
    else:
        # Try finding the table with expected headers first
        for table in results_tables:
             header_row = table.find('tr')
             if header_row and header_row.find(['td', 'th'], string=re.compile(r'Author|Title|DOI|Journal|Series', re.I)):
                 main_table = table
                 print(f"Found results table (class='{table_class}') using header check.") # Log
                 break
        # Fallback: Use the first table with more than one row if header check fails
        if main_table is None:
            for table in results_tables:
                if len(table.find_all('tr')) > 1:
                    main_table = table
                    print(f"Found potential results table (class='{table_class}', fallback).") # Log
                    break

    if main_table is None:
        print("Error: Could not identify results table.") # Log
        return []

    rows = main_table.find_all('tr')
    if len(rows) <= 1:
        print("No results found in table.") # Log
        return []

    results = []
    print(f"Found {len(rows) - 1} results. Parsing...") # Log

    # Index mapping (same as original)
    idx = {'authors': 1, 'title': 2, 'publisher': 3, 'year': 4, 'pages': 5, 'language': 6, 'size': 7, 'extension': 8, 'mirror1': 9, 'mirror2': 10, 'mirrors': -1, 'file_info': -1}
    if search_topic == 'fiction': idx = {'authors': 0, 'series': 1, 'title': 2, 'language': 3, 'file_info': 4, 'mirrors': 5, 'publisher': -1, 'year': -1, 'pages': -1, 'extension': -1, 'size': -1, 'mirror1': -1, 'mirror2': -1}
    elif search_topic == 'scimag': idx = {'authors': 0, 'title': 1, 'journal': 2, 'size': 3, 'mirrors': 4, 'publisher': -1, 'year': -1, 'pages': -1, 'extension': -1, 'mirror1': -1, 'mirror2': -1}

    for row in rows[1:]:
        cells = row.find_all('td')
        # Ensure enough cells exist based on the maximum index we might access
        max_defined_index = max((idx.get(k, -1) for k in idx), default=-1)
        if len(cells) <= max_defined_index: continue

        try:
            # --- Variable extraction (mostly identical to original) ---
            title="N/A"; authors="N/A"; language="N/A"; size="N/A"; extension="N/A"; publisher=''; year=''; pages=''
            details_url=None; target_url_mirror1=None; target_url_mirror2=None

            title_idx = idx.get('title', -1)
            if title_idx >= 0 and len(cells) > title_idx: # Check cell exists
                 title_cell = cells[title_idx]; link_tag = title_cell.find('a', href=True)
                 if link_tag:
                      title = link_tag.get_text(separator=' ', strip=True); title = re.sub(r'\s*\[?\d{10,13}[X]?\]?\s*$', '', title).strip()
                      relative_url = link_tag['href']; details_url = urllib.parse.urljoin(search_url, relative_url)
                 else: title = title_cell.get_text(strip=True)
            else: continue # Skip row if no title found

            authors_idx = idx.get('authors', -1);
            if authors_idx >= 0 and len(cells) > authors_idx: authors = cells[authors_idx].get_text(strip=True)
            publisher_idx = idx.get('publisher', idx.get('journal', -1));
            if publisher_idx >= 0 and len(cells) > publisher_idx: publisher = cells[publisher_idx].get_text(strip=True)
            year_idx = idx.get('year', -1);
            if year_idx >= 0 and len(cells) > year_idx: year = cells[year_idx].get_text(strip=True)
            pages_idx = idx.get('pages', -1);
            if pages_idx >= 0 and len(cells) > pages_idx: pages = cells[pages_idx].get_text(strip=True).split('[')[0] # Clean pages
            language_idx = idx.get('language', -1);
            if language_idx >= 0 and len(cells) > language_idx: language = cells[language_idx].get_text(strip=True)
            size_idx = idx.get('size', -1);
            if size_idx >= 0 and len(cells) > size_idx: size = cells[size_idx].get_text(strip=True)
            extension_idx = idx.get('extension', -1);
            if extension_idx >= 0 and len(cells) > extension_idx: extension = cells[extension_idx].get_text(strip=True)
            file_info_idx = idx.get('file_info', -1);
            if file_info_idx >= 0 and len(cells) > file_info_idx:
                file_info_text = cells[file_info_idx].get_text(strip=True); parts = file_info_text.split('/')
                if len(parts) == 2: extension, size = parts[0].strip(), parts[1].strip()
                else: extension = file_info_text # Assume only extension if not split by '/'
            extension = extension.lower() if extension else 'n/a'

            mirror1_idx = idx.get('mirror1', -1); mirrors_idx = idx.get('mirrors', -1); mirror2_idx = idx.get('mirror2', -1)
            if mirror1_idx >= 0 and len(cells) > mirror1_idx:
                 mirror1_tag = cells[mirror1_idx].find('a'); url_val = mirror1_tag['href'] if mirror1_tag else None
                 if url_val and url_val.startswith('http'): target_url_mirror1 = url_val
            if mirror2_idx >= 0 and len(cells) > mirror2_idx:
                 mirror2_tag = cells[mirror2_idx].find('a'); url_val = mirror2_tag['href'] if mirror2_tag else None
                 if url_val and url_val.startswith('http'): target_url_mirror2 = url_val
            elif mirrors_idx >= 0 and len(cells) > mirrors_idx: # Fallback for combined mirrors column
                 mirrors_cell = cells[mirrors_idx]; mirror_links = mirrors_cell.find_all('a', href=True)
                 found_mirrors = [link['href'] for link in mirror_links if link.get('href', '').startswith('http')]
                 if len(found_mirrors) > 0: target_url_mirror1 = found_mirrors[0]
                 if len(found_mirrors) > 1: target_url_mirror2 = found_mirrors[1]

            # Assign details_url to mirror1 if no explicit mirror1 found (for get_libgen_download_link_async to process)
            if not target_url_mirror1 and details_url:
                 print(f"  No explicit Mirror 1 URL for '{title}'. Will use details page if selected.") # Log
                 target_url_mirror1 = details_url

            # Only add if there's a potential way to download (Mirror 1 URL assigned, even if it's details_url)
            if target_url_mirror1 or target_url_mirror2:
                results.append({
                    "title": title, "authors": authors, "publisher": publisher, "year": year,
                    "pages": pages, "language": language, "size": size, "extension": extension,
                    "mirror1_url": target_url_mirror1, "mirror2_url": target_url_mirror2,
                    "details_url": details_url # Keep details URL for context
                })
            else:
                print(f"Skipping '{title}' - No usable mirror or details URLs found.") # Log
                continue
        except Exception as e:
            print(f"Error parsing row for title potentially starting with '{title[:50]}...': {e}") # Log detailed error on server
            # import traceback; traceback.print_exc() # Uncomment for full traceback during debug
            continue # Skip malformed rows

    return results

async def get_libgen_download_link_async(page_url: str):
    """Fetches mirror page async, finds direct 'GET' link or SciMag/alternative."""
    print(f"\nAsync Fetching details/mirror page: {page_url}") # Log
    if not page_url or not page_url.startswith('http'):
        print(f"Error: Invalid page URL: {page_url}") # Log
        return None

    try:
        parsed_uri = urllib.parse.urlparse(page_url)
        mirror_base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
        mirror_host = parsed_uri.netloc
    except ValueError:
        print(f"Error: Could not parse mirror page URL: {page_url}") # Log
        return None

    try:
        loop = asyncio.get_running_loop()
        # Update Referer for the specific mirror page request
        current_headers = http_session.headers.copy()
        referer = http_session.headers.get('Referer', LIBGEN_BASE_URL + '/') # Use last known referer or fallback
        current_headers['Referer'] = referer

        response = await loop.run_in_executor(
            None,
            lambda: http_session.get(page_url, timeout=40, headers=current_headers, allow_redirects=True) # Allow redirects here
        )
        response.raise_for_status()
        print(f"  Page status: {response.status_code}") # Log
        # Update session's referer for subsequent download request
        http_session.headers['Referer'] = response.url # Use the final URL after redirects
        await asyncio.sleep(0.5) # Small delay
    except requests.exceptions.RequestException as e:
        print(f"Error fetching page {page_url}: {e}") # Log
        return None
    except Exception as e:
        print(f"Unexpected error fetching page {page_url}: {e}") # Log
        return None

    # --- Parsing logic (same as original, safe for async) ---
    soup = BeautifulSoup(response.text, 'lxml')
    download_url = None
    download_link_tag = None
    print(f"  Analyzing page from host: {mirror_host}") # Log

    # Pattern 1 (libgen.li style - specific TD background - check relevance)
    get_td_li = soup.find('td', {'bgcolor': '#A9F5BC'})
    if get_td_li:
        get_link_tag_in_li_td = get_td_li.find('a', href=re.compile(r'get\.php\?md5=|/get\?'))
        if get_link_tag_in_li_td:
             link_text_content = get_link_tag_in_li_td.get_text(strip=True)
             if link_text_content.upper() == 'GET':
                 download_link_tag = get_link_tag_in_li_td
                 print("Found 'GET' link pattern 1 (specific TD).") # Log

    # Pattern 2 (Common style - GET link inside H2)
    if not download_link_tag:
        print("  Pattern 1 failed/NA. Checking GET link inside H2...") # Log
        h2_get_link = soup.select_one('h2 > a[href]')
        if h2_get_link and h2_get_link.get_text(strip=True).upper() == 'GET':
            download_link_tag = h2_get_link
            print("Found 'GET' link pattern 2 (inside H2).") # Log

    # Pattern 3 (General 'GET' link search - less specific)
    if not download_link_tag:
         print("  Pattern 2 failed/NA. Checking any prominent 'GET' link...") # Log
         all_links = soup.find_all('a', href=True)
         for link in all_links:
             href = link.get('href', '')
             text = link.get_text(strip=True).upper()
             # Check for GET text and common download path indicators
             if text == 'GET' and ('get.php' in href or '/get?' in href or 'download' in href):
                 link_parsed = urllib.parse.urlparse(href)
                 if not link_parsed.netloc or link_parsed.netloc == mirror_host:
                      download_link_tag = link
                      print(f"Found potential 'GET' link pattern 3 (general search): {href}") # Log
                      break # Take the first likely match

    # Pattern 4 (SciMag/Alternative Links - specific domains)
    if not download_link_tag:
        print("  Primary 'GET' failed. Looking for SciMag/alternative links...") # Log
        all_links = soup.find_all('a', href=True)
        possible_links = []
        for link in all_links:
            href = link['href']
            if any(domain in href for domain in ['library.lol', 'libgen.rs', 'books.ms', 'sci-hub']):
                 if '/scimag/' in href or '/get?' in href or 'sci-hub' in href:
                      print(f"Found potential direct SciMag/alternative mirror link: {href}") # Log
                      possible_links.append(href)
        if possible_links:
            download_link_tag = soup.new_tag('a', href=possible_links[0]) # Create a dummy tag to unify logic
            print("Using alternative SciMag/mirror link as primary.") # Log


    # Process found GET tag or fallbacks
    if download_link_tag and download_link_tag.get('href'):
        relative_or_absolute_url = download_link_tag['href']
        # Ensure the URL is absolute
        download_url = urllib.parse.urljoin(response.url, relative_or_absolute_url) # Use response.url as base
        print(f"  Extracted download URL: {download_url}") # Log
        return download_url
    else:
        # Fallback: If the original page_url itself looks like a direct file link
        if page_url.split('?')[0].lower().endswith(('.pdf', '.epub', '.mobi', '.zip', '.djvu')):
             print(f"Assuming page URL might be the download link: {page_url}") # Log
             return page_url # Risky, but a last resort

    print("Error: Could not find any usable download link on the page.") # Log
    return None

async def download_book_to_discord(download_url: str, title: str, extension: str):
    """Downloads file async into memory, returns discord.File or None/error string."""
    if not download_url:
        return "No download URL provided."

    print(f"\nAsync Attempting to download '{title}' from {download_url}") # Log
    filename_base = safe_filename(title)
    final_extension = extension.lower() if extension and extension != 'n/a' else 'bin' # Default to .bin if unknown
    filename = f"{filename_base}.{final_extension}"

    try:
        loop = asyncio.get_running_loop()
        # Use the session's current Referer set by get_libgen_download_link_async
        current_headers = http_session.headers.copy()
        print(f"  Using Referer: {current_headers.get('Referer')}") # Log

        # First, make a HEAD request to check Content-Length and Content-Type if possible
        head_response = None
        try:
            head_response = await loop.run_in_executor(
                None,
                lambda: http_session.head(download_url, timeout=30, allow_redirects=True, headers=current_headers)
            )
            head_response.raise_for_status()
            content_length = head_response.headers.get('content-length')
            content_type = head_response.headers.get('content-type', '').lower()
            final_url_from_head = head_response.url # URL after redirects from HEAD

            print(f"  HEAD response: Status={head_response.status_code}, Type={content_type}, Length={content_length}") # Log

            if content_length and int(content_length) > DISCORD_FILE_LIMIT_BYTES:
                print(f"  File size ({int(content_length)} bytes) exceeds Discord limit ({DISCORD_FILE_LIMIT_BYTES} bytes).") # Log
                return f"File is too large ({int(content_length)/(1024*1024):.2f} MB). Max size: {DISCORD_FILE_LIMIT_MB} MB."

            # Don't immediately fail on text/html from HEAD, GET might redirect differently
            # if 'text/html' in content_type:
            #      print("  HEAD indicates HTML content, likely an error page.")

        except requests.exceptions.RequestException as head_err:
            print(f"  HEAD request failed: {head_err}. Proceeding with GET.") # Log

        # Now perform the GET request to download
        response = await loop.run_in_executor(
            None,
            lambda: http_session.get(download_url, stream=True, timeout=180, allow_redirects=True, headers=current_headers)
        )
        print(f"  Download GET status: {response.status_code}") # Log

        if response.status_code in [502, 503, 504, 404, 403]:
             print(f"  Download failed: Server returned status {response.status_code}.") # Log
             return f"Download failed (Server Status: {response.status_code}). Try the other mirror if available."

        response.raise_for_status() # Raise for other client/server errors

        final_url = response.url
        content_type = response.headers.get('content-type', '').lower()

        # Check for HTML response *after* GET request
        if 'text/html' in content_type:
            try:
                text_snippet = ""
                # Use response.content directly if not streaming heavily or for small check
                # If streaming, need to handle chunks carefully
                peek_bytes = await loop.run_in_executor(None, lambda: response.raw.peek(1024))
                text_snippet = peek_bytes.decode('utf-8', errors='ignore')

                if 'sci-hub.' in str(final_url) and ('captcha' in text_snippet.lower() or '<form' in text_snippet.lower()):
                    print(">>> CAPTCHA BLOCK DETECTED <<<") # Log
                    response.close() # Close connection
                    return f"Download failed: CAPTCHA required at {final_url}. Please visit the link manually."
            except Exception as snippet_err:
                 print(f"Could not read HTML snippet: {snippet_err}")

            print(">>> DOWNLOAD FAILED: Received HTML instead of a file. <<<") # Log
            response.close() # Close connection
            return "Download failed: Link led to an HTML page (possibly an error or login page)."


        # Refine filename/extension based on final URL and headers
        content_disposition = response.headers.get('content-disposition')
        valid_extensions = ['pdf', 'epub', 'mobi', 'azw3', 'djvu', 'zip', 'rar', 'txt', 'chm']
        if content_disposition:
            match = re.findall('filename="?([^"]+)"?', content_disposition)
            if match:
                try:
                    header_filename = urllib.parse.unquote(match[0], errors='replace')
                    header_base, header_ext = os.path.splitext(header_filename)
                    header_ext = header_ext.lower().lstrip('.')
                    if header_ext in valid_extensions:
                        print(f"  Using filename from header: {header_filename}") # Log
                        filename_base = safe_filename(header_base)
                        final_extension = header_ext
                    elif extension == 'n/a' or extension == 'bin': # Only override if original was unknown
                         final_extension = header_ext if header_ext else 'bin'
                         print(f"  Using extension '{final_extension}' from header.") # Log
                except Exception as e:
                     print(f"Error parsing content-disposition: {e}")

        # Fallback extension detection if needed
        if final_extension == 'bin' or final_extension == 'n/a':
             # Check common types
             type_map = {
                 'application/pdf': 'pdf', 'application/epub+zip': 'epub',
                 'application/x-mobipocket-ebook': 'mobi', 'application/vnd.amazon.ebook': 'azw3',
                 'image/vnd.djvu': 'djvu', 'application/zip': 'zip',
                 'application/vnd.rar': 'rar', 'application/x-rar-compressed': 'rar',
                 'text/plain': 'txt', 'application/vnd.ms-htmlhelp': 'chm'
             }
             for mime, ext in type_map.items():
                 if mime in content_type:
                     final_extension = ext
                     print(f"  Deduced extension '{ext}' from Content-Type '{content_type}'")
                     break
             if final_extension == 'bin': # Still unknown after check
                print(f"  Warning: Could not determine extension from Content-Type '{content_type}', using .bin") # Log

        filename = f"{filename_base}.{final_extension}"
        print(f"  Final filename: {filename}") # Log

        # Download into memory (BytesIO buffer)
        file_buffer = io.BytesIO()
        downloaded_size = 0
        limit_exceeded = False
        # Use iter_content for streaming download into memory
        for chunk in response.iter_content(chunk_size=8192):
            if chunk: # filter out keep-alive new chunks
                downloaded_size += len(chunk)
                if downloaded_size > DISCORD_FILE_LIMIT_BYTES:
                    limit_exceeded = True
                    print(f"  Download aborted: File size exceeds {DISCORD_FILE_LIMIT_MB} MB limit.") # Log
                    response.close() # Crucial: stop the download
                    break
                file_buffer.write(chunk)

        if limit_exceeded:
            # Ensure buffer is closed if needed, though GC should handle BytesIO
            file_buffer.close()
            return f"File is too large ({downloaded_size/(1024*1024):.2f} MB+). Max size: {DISCORD_FILE_LIMIT_MB} MB."

        # If download completed within limits
        file_buffer.seek(0) # Reset buffer position to the beginning
        print(f"  Downloaded {downloaded_size} bytes into memory.") # Log
        discord_file = discord.File(fp=file_buffer, filename=filename)
        # fp will be closed by discord.File upon sending
        return discord_file

    except requests.exceptions.Timeout:
        print(f"\nError: Download timed out for {download_url}")
        return "Download timed out. The server might be slow."
    except requests.exceptions.RequestException as e:
        print(f"\nError during download: {e}") # Log
        if hasattr(e, 'response') and e.response is not None:
            print(f"Status Code: {e.response.status_code}") # Log
        return f"An error occurred during download: {e}"
    except Exception as e:
        print(f"\nUnexpected error during download: {e}") # Log
        import traceback; traceback.print_exc() # Log full traceback
        return f"An unexpected error occurred: {e}"
    finally:
        # Ensure the response connection is closed if it exists and wasn't closed earlier
        if 'response' in locals() and hasattr(response, 'close') and not response.raw.closed:
            response.close()


# --- Discord Bot Setup ---
intents = discord.Intents.default()
# intents.message_content = True # Only needed for message content reading, not slash commands

class BookFinderBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync commands globally. Use guild=discord.Object(id=YOUR_GUILD_ID) for faster testing.
        await self.tree.sync()
        print(f'Synced commands for {self.user}.')

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

client = BookFinderBot(intents=intents)

# --- Discord UI Components ---

class SearchResultSelectView(View):
    def __init__(self, search_results, original_interaction: discord.Interaction):
        super().__init__(timeout=300) # 5 minute timeout for selection
        self.search_results = search_results
        self.original_interaction = original_interaction
        # Ensure options list does not exceed 25 items (Discord limit)
        options_limited = search_results[:25]
        self.select_menu = Select(
            placeholder="Select a book/article to download (Up to 25 shown)...",
            options=[
                discord.SelectOption(
                    label=f"{i+1}. {item['title'][:95]}" + ("..." if len(item['title']) > 95 else ""), # Limit label length
                    description=f"{item['authors'][:35]} ({item.get('year', 'N/A')}) | {item.get('extension', 'N/A')} | {item.get('size', 'N/A')}"[:100], # Adjust lengths, ensure max 100 chars
                    value=str(i) # Value is the index in the original list
                ) for i, item in enumerate(options_limited) # Iterate over limited list for options
            ]
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def select_callback(self, interaction: discord.Interaction):
        # Ensure only the original user can interact
        if interaction.user != self.original_interaction.user:
             await interaction.response.send_message("You didn't initiate this search.", ephemeral=True)
             return

        selected_index = int(self.select_menu.values[0])
        # Ensure index is valid even if results > 25 but only 25 shown
        if selected_index >= len(self.search_results):
            await interaction.response.send_message("Invalid selection index.", ephemeral=True)
            return

        selected_item = self.search_results[selected_index]

        # Disable the dropdown after selection
        self.select_menu.disabled = True
        self.select_menu.placeholder = f"Selected: {selected_item['title'][:50]}..." # Update placeholder
        # Ensure view update happens before deferring
        await interaction.response.edit_message(view=self)

        # Defer the followup response for download processing
        await interaction.followup.send(f"Attempting to download '{selected_item['title']}'...", ephemeral=False, wait=True) # Indicate download start

        # --- Attempt Download (Mirror 1) ---
        download_result = None
        error_message = "Download failed. Unknown reason." # Default error

        mirror1_url = selected_item.get('mirror1_url')
        if mirror1_url:
            print(f"Attempting Mirror 1: {mirror1_url}") # Log
            direct_download_link = await get_libgen_download_link_async(mirror1_url)

            if direct_download_link:
                download_result = await download_book_to_discord(
                    direct_download_link,
                    selected_item['title'],
                    selected_item['extension']
                )
            else:
                error_message = "Could not find a direct download link from the first mirror page."
                print(error_message) # Log
        else:
            error_message = "Mirror 1 URL was not found in the search results (might be details page only)."
            print(error_message) # Log

        # --- Process Download Result ---
        if isinstance(download_result, discord.File):
            try:
                 # Edit the "Attempting to download..." message
                 await interaction.edit_original_response(content=f"✅ Download successful for **{selected_item['title']}**!", attachments=[download_result], view=self) # Send file by editing original
            except discord.HTTPException as e:
                 print(f"Discord API error sending file: {e}") # Log
                 # Try sending as a new message if edit fails
                 try:
                     await interaction.followup.send(f"✅ Download successful for **{selected_item['title']}**!", file=download_result)
                 except discord.HTTPException as e2:
                     print(f"Discord API error sending file (followup): {e2}") # Log
                     await interaction.followup.send(f"⚠️ Could not send the file via Discord (Error: {e2.code}). It might be slightly too large or another issue occurred.")
                     # Still offer Mirror 2
                     mirror2_url = selected_item.get('mirror2_url')
                     if mirror2_url:
                         await interaction.followup.send(
                             f"You can try downloading manually using Mirror 2:\n"
                             f"<{mirror2_url}>\n"
                             f"(Remember to click 'GET' on the page, possibly twice due to pop-ups)."
                         )
            except Exception as ex:
                 print(f"Unexpected error sending file: {ex}")
                 await interaction.followup.send(f"An unexpected error occurred while sending the file.")


        else: # Download failed or file too large
            # If download_result contains an error message string
            if isinstance(download_result, str):
                 error_message = download_result # Use the specific error

            print(f"Mirror 1 failed for '{selected_item['title']}'. Reason: {error_message}") # Log
            fallback_message = f"❌ Download via Mirror 1 failed: {error_message}\n"

            # Offer Mirror 2 as fallback
            mirror2_url = selected_item.get('mirror2_url')
            if mirror2_url:
                fallback_message += (
                    f"\nYou can try **Mirror 2** manually in your browser:\n"
                    f"<{mirror2_url}>\n"
                    f"*(On that page, click the **GET** button. You might need to click once to close an ad, then click GET again)*"
                )
            elif selected_item.get('details_url') and selected_item.get('details_url') != mirror1_url: # Offer details if different from M1 attempt
                 fallback_message += f"\nYou can visit the details page for more options: <{selected_item.get('details_url')}>"
            else:
                fallback_message += "\nNo alternative Mirror 2 URL was found for this item."

            # Edit the "Attempting to download..." message with the error
            await interaction.edit_original_response(content=fallback_message, attachments=[], view=self) # Clear attachments if edit fails

    async def on_timeout(self):
         # Edit the original message to indicate timeout and disable view
        if self.original_interaction:
            try:
                # Check if response already edited (e.g. successful download)
                msg = await self.original_interaction.original_response()
                if msg.content.startswith("✅") or msg.content.startswith("❌"): # Check if download attempt finished
                    return # Don't overwrite success/failure message

                for item in self.children:
                    if isinstance(item, Select):
                         item.disabled = True
                         item.placeholder = "Selection timed out."
                await self.original_interaction.edit_original_response(content="Selection timed out.", view=self)
            except discord.NotFound:
                print("Original interaction message not found on timeout.")
            except discord.HTTPException as e:
                # Ignore errors if the message was already edited or deleted
                if e.code == 50027: # Invalid Webhook Token (often means interaction expired/deleted)
                    print("Interaction expired or was deleted before timeout edit.")
                else:
                    print(f"Error updating view on timeout: {e}") # Log other errors
            except Exception as e:
                print(f"Unexpected error updating view on timeout: {e}") # Log other errors

# --- Slash Commands ---

@client.tree.command(name="help", description="Shows information about the Book Finder bot.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True) # ADDED
@app_commands.user_install() # ADDED
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Book Finder Bot Help",
        description="This bot helps you search for books, articles, and fiction on Library Genesis.",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="How to Use",
        value=(
            "1. Use the `/findbook` command.\n" # UPDATED command name here
            "2. Enter your search query (title, author, ISBN, etc.).\n"
            "3. Choose a category (Non-Fiction, Fiction, Articles).\n"
            "4. If results are found, a dropdown menu will appear.\n"
            "5. Select an item from the dropdown to attempt download.\n"
            f"   *(Note: Files larger than {DISCORD_FILE_LIMIT_MB}MB cannot be sent directly via Discord)*"
        ),
        inline=False
    )
    embed.add_field(
        name="⚠️ Copyright Warning",
        value=(
            "Please use this bot responsibly and respect copyright laws. "
            "Downloading copyrighted material without permission may be illegal in your country. "
            "This tool is provided for informational purposes only."
        ),
        inline=False
    )
    embed.add_field(
        name="💡 Search Tips",
        value=(
            "- Be specific: Include author names if you know them.\n"
            "- Try variations: Use ISBN or ASIN if the title yields too many results.\n"
            "- Check categories: If you don't find a book in 'Non-Fiction', try 'Fiction' (or vice-versa if applicable).\n"
            "- Use keywords: For articles, try searching by DOI or keywords from the title."
        ),
        inline=False
    )
    embed.set_footer(text=f"Bot created by {BOT_CREATOR}")

    await interaction.response.send_message(embed=embed, ephemeral=True) # Help is ephemeral


@client.tree.command(name="findbook", description="Search for books, fiction, or articles.") # RENAMED command
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True) # ADDED
@app_commands.user_install() # ADDED
@app_commands.describe(
    query="What to search for (title, author, ISBN, DOI, etc.)",
    category="Select the search category"
)
@app_commands.choices(category=[
    app_commands.Choice(name="Non-Fiction / Sci-Tech", value="libgen"),
    app_commands.Choice(name="Fiction", value="fiction"),
    app_commands.Choice(name="Scientific Articles", value="scimag"),
    app_commands.Choice(name="Magazines (Link Only)", value="magz"),
])
# RENAMED function for consistency
async def findbook_command(interaction: discord.Interaction, query: str, category: app_commands.Choice[str]):
    search_topic = category.value
    search_topic_name = category.name

    await interaction.response.defer(thinking=True, ephemeral=False) # Acknowledge command publicly

    if search_topic == 'magz':
        magz_url = f"http://magzdb.org/makelist?t={urllib.parse.quote_plus(query)}"
        await interaction.followup.send(
            f"Magazine searches are handled by MagzDB.\n"
            f"Please visit this link to see results for '{query}':\n<{magz_url}>"
        )
        return

    # Perform the async search
    results = await search_libgen_async(query, search_topic=search_topic)

    if not results:
        await interaction.followup.send(f"No results found for '{query}' in '{search_topic_name}'. Try different keywords or categories.")
        return

    # Format results for Discord Embed
    embed = discord.Embed(
        title=f"Search Results for '{query}' ({search_topic_name})",
        description=f"Found {len(results)} results. Select one below to attempt download (up to 25 shown).", # Updated description
        color=discord.Color.green()
    )

    # Add limited fields for overview, full details in dropdown description
    results_to_preview = results[:5] # Show first 5 in embed for preview
    for i, item in enumerate(results_to_preview):
         embed.add_field(
             name=f"{i+1}. {item['title'][:100]}" + ("..." if len(item['title']) > 100 else ""), # Limit title length
             value=f"Author(s): {item.get('authors', 'N/A')[:100]}\n"
                   f"Format: {item.get('extension', 'N/A')} | Size: {item.get('size', 'N/A')} | Lang: {item.get('language', 'N/A')}",
             inline=False
         )

    footer_text = f"Total results: {len(results)}."
    if len(results) > 25:
        footer_text += " Showing the first 25 in the dropdown."
    elif len(results) > 5:
         footer_text += " Use dropdown for all results."
    embed.set_footer(text=footer_text)


    # Create the View with the Select dropdown
    view = SearchResultSelectView(results, interaction)

    # Send the embed and the view
    await interaction.followup.send(embed=embed, view=view)


# --- Run the Bot ---
if __name__ == "__main__":
    if BOT_TOKEN is None:
        print("ERROR: Discord Bot Token not found.")
        print("Please set the DISCORD_BOT_TOKEN environment variable.")
    else:
        # Start Flask in a background thread
        flask_thread = threading.Thread(target=run_flask, daemon=True) # Use daemon=True so it exits when main thread exits
        flask_thread.start()

        try:
            # Start the Discord bot (blocking call)
            client.run(BOT_TOKEN)
        except discord.LoginFailure:
            print("ERROR: Invalid Discord Bot Token. Please check your token.")
        except Exception as e:
            print(f"An error occurred while running the bot: {e}")
            # Consider more robust error handling or logging here 
