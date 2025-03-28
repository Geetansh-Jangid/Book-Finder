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
    # Parsing is CPU-bound, so running it directly in async function is okay
    # unless it's extremely heavy, but BS4 is usually fast enough.
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
    # Removed warning message for bot interface

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
            if title_idx >= 0:
                 title_cell = cells[title_idx]; link_tag = title_cell.find('a', href=True)
                 if link_tag:
                      # Clean ISBN/ASIN from title text
                      title = link_tag.get_text(separator=' ', strip=True); title = re.sub(r'\s*\[?\d{10,13}[X]?\]?\s*$', '', title).strip()
                      relative_url = link_tag['href']; details_url = urllib.parse.urljoin(search_url, relative_url)
                 else: title = title_cell.get_text(strip=True)
            else: continue # Skip row if no title found

            # Safely get other fields
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
            # Handle combined file info for fiction/scimag
            file_info_idx = idx.get('file_info', -1);
            if file_info_idx >= 0 and len(cells) > file_info_idx:
                file_info_text = cells[file_info_idx].get_text(strip=True); parts = file_info_text.split('/')
                if len(parts) == 2: extension, size = parts[0].strip(), parts[1].strip()
                else: extension = file_info_text # Assume only extension if not split by '/'
            extension = extension.lower() if extension else 'n/a'

            # Extract mirror URLs (logic same as original)
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

            # Fallback using details URL if no explicit mirror found (less reliable for direct downloads)
            if not target_url_mirror1 and details_url:
                # Let's NOT use details_url as a fallback mirror here, as it often leads to the intermediate page anyway.
                # get_libgen_download_link will handle the intermediate page if needed.
                # If target_url_mirror1 is None, the logic will try it.
                 print(f"  No explicit Mirror 1 URL for '{title}'. Will use details page if selected.") # Log
                 target_url_mirror1 = details_url # Assigning it here so get_libgen... can use it

            # Only add if there's a potential way to download (Mirror 1 or 2)
            if target_url_mirror1 or target_url_mirror2:
                results.append({
                    "title": title, "authors": authors, "publisher": publisher, "year": year,
                    "pages": pages, "language": language, "size": size, "extension": extension,
                    "mirror1_url": target_url_mirror1, "mirror2_url": target_url_mirror2,
                    "details_url": details_url # Keep details URL for context if needed
                })
            else:
                print(f"Skipping '{title}' - No usable mirror or details URLs found.") # Log
                continue
        except Exception as e:
            print(f"Error parsing row for '{title}': {e}") # Log detailed error on server
            # Optionally import traceback and print_exc() here for debugging
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
        # Try to set a plausible referer, fallback to base LibGen URL
        referer = http_session.headers.get('Referer', LIBGEN_BASE_URL + '/')
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

    # Pattern 1 (libgen.li style - specific TD background)
    get_td_li = soup.find('td', {'bgcolor': '#A9F5BC'}) # Check if this is still relevant for libgen.is mirrors
    if get_td_li:
        get_link_tag_in_li_td = get_td_li.find('a', href=re.compile(r'get\.php\?md5=|/get\?')) # More general GET pattern
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
                 # Prioritize links clearly pointing to the same domain or known download subdomains
                 link_parsed = urllib.parse.urlparse(href)
                 if not link_parsed.netloc or link_parsed.netloc == mirror_host:
                      download_link_tag = link
                      print(f"Found potential 'GET' link pattern 3 (general search): {href}") # Log
                      break # Take the first likely match

    # Pattern 4 (SciMag/Alternative Links - specific domains)
    if not download_link_tag:
        print("  Primary 'GET' failed. Looking for SciMag/alternative links...") # Log
        # Look in lists or specific divs if possible, otherwise scan all links
        all_links = soup.find_all('a', href=True)
        possible_links = []
        for link in all_links:
            href = link['href']
            # Prioritize known direct download/mirror host patterns
            if any(domain in href for domain in ['library.lol', 'libgen.rs', 'books.ms', 'sci-hub']):
                 # Check if it looks like a direct file link or scimag link
                 if '/scimag/' in href or '/get?' in href or 'sci-hub' in href:
                      print(f"Found potential direct SciMag/alternative mirror link: {href}") # Log
                      possible_links.append(href)

        if possible_links:
            return possible_links[0] # Return the first likely alternative link found


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

            if 'text/html' in content_type:
                 # Check if it's a Sci-Hub CAPTCHA page even from HEAD
                 if 'sci-hub.' in str(final_url_from_head): # Check the final URL domain
                     # Need to fetch a bit of content to be sure
                     pass # Let the GET request handle HTML check
                 else:
                     print("  HEAD indicates HTML content, likely an error page.") # Log
                     # Proceed to GET anyway, it might redirect differently or provide more info
                     # return "Download link led to an HTML page, not a file."

        except requests.exceptions.RequestException as head_err:
            print(f"  HEAD request failed: {head_err}. Proceeding with GET.") # Log
            # Continue with GET request even if HEAD fails

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
            # Try to read a small part to check for CAPTCHA on Sci-Hub
            try:
                text_snippet = ""
                async for chunk in response.aiter_content(chunk_size=512, decode_unicode=True):
                    text_snippet += chunk
                    if len(text_snippet) > 1000: break
                if 'sci-hub.' in str(final_url) and ('captcha' in text_snippet.lower() or '<form' in text_snippet.lower()): # More robust check
                    print(">>> CAPTCHA BLOCK DETECTED <<<") # Log
                    return f"Download failed: CAPTCHA required at {final_url}. Please visit the link manually."
            except Exception as snippet_err:
                 print(f"Could not read HTML snippet: {snippet_err}")
            print(">>> DOWNLOAD FAILED: Received HTML instead of a file. <<<") # Log
            return "Download failed: Link led to an HTML page (possibly an error or login page)."

        # Refine filename/extension based on final URL and headers (similar to original)
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
             if 'application/pdf' in content_type: final_extension = 'pdf'
             elif 'application/epub+zip' in content_type: final_extension = 'epub'
             elif 'application/x-mobipocket-ebook' in content_type: final_extension = 'mobi'
             elif 'application/vnd.amazon.ebook' in content_type: final_extension = 'azw3'
             elif 'image/vnd.djvu' in content_type: final_extension = 'djvu'
             elif 'application/zip' in content_type: final_extension = 'zip'
             elif 'application/vnd.rar' in content_type: final_extension = 'rar'
             elif 'text/plain' in content_type: final_extension = 'txt'
             else: final_extension = 'bin'; print("  Warning: Could not determine extension, using .bin") # Log

        filename = f"{filename_base}.{final_extension}"
        print(f"  Final filename: {filename}") # Log

        # Download into memory (BytesIO buffer)
        file_buffer = io.BytesIO()
        downloaded_size = 0
        limit_exceeded = False
        async for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                downloaded_size += len(chunk)
                if downloaded_size > DISCORD_FILE_LIMIT_BYTES:
                    limit_exceeded = True
                    print(f"  Download aborted: File size exceeds {DISCORD_FILE_LIMIT_MB} MB limit.") # Log
                    # Must close the connection to avoid partial downloads hanging
                    response.close()
                    break
                file_buffer.write(chunk)

        if limit_exceeded:
            return f"File is too large ({downloaded_size/(1024*1024):.2f} MB+). Max size: {DISCORD_FILE_LIMIT_MB} MB."

        # If download completed within limits
        file_buffer.seek(0) # Reset buffer position to the beginning
        print(f"  Downloaded {downloaded_size} bytes into memory.") # Log
        discord_file = discord.File(fp=file_buffer, filename=filename)
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
# intents.message_content = True # Make sure this is enabled in Dev Portal

class BookFinderBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync commands globally. Use guild=discord.Object(id=...) for faster testing on a specific server.
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
        self.select_menu = Select(
            placeholder="Select a book/article to download...",
            options=[
                discord.SelectOption(
                    label=f"{i+1}. {item['title'][:95]}...", # Limit label length
                    description=f"{item['authors'][:40]} ({item.get('year', 'N/A')}) - Ext: {item.get('extension', 'N/A')} | Lang: {item.get('language', 'N/A')}"[:100], # Add Lang, adjust lengths, ensure max 100 chars
                    value=str(i) # Value is the index in the list
                ) for i, item in enumerate(search_results)
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
        selected_item = self.search_results[selected_index]

        # Disable the dropdown after selection
        self.select_menu.disabled = True
        self.select_menu.placeholder = f"Selected: {selected_item['title'][:50]}..." # Update placeholder
        await self.original_interaction.edit_original_response(view=self) # Update the original message

        await interaction.response.defer(thinking=True, ephemeral=False) # Acknowledge selection, visible to others

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
            error_message = "Mirror 1 URL was not found in the search results."
            print(error_message) # Log

        # --- Process Download Result ---
        if isinstance(download_result, discord.File):
            try:
                 await interaction.followup.send(f"✅ Download successful for **{selected_item['title']}**!", file=download_result)
            except discord.HTTPException as e:
                 # This might happen if the file *just* exceeded the limit after download, or other Discord API errors
                 print(f"Discord API error sending file: {e}") # Log
                 await interaction.followup.send(f"⚠️ Could not send the file via Discord (Error: {e.code}). It might be slightly too large or another issue occurred.")
                 # Still offer Mirror 2 as fallback
                 mirror2_url = selected_item.get('mirror2_url')
                 if mirror2_url:
                     await interaction.followup.send(
                         f"You can try downloading manually using Mirror 2:\n"
                         f"<{mirror2_url}>\n"
                         f"(Remember to click 'GET' on the page, possibly twice due to pop-ups)."
                     )

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
            else:
                fallback_message += "\nNo Mirror 2 URL was found for this item."

            await interaction.followup.send(fallback_message)

    async def on_timeout(self):
         # Edit the original message to indicate timeout and disable view
        if self.original_interaction:
            try:
                self.select_menu.disabled = True
                self.select_menu.placeholder = "Selection timed out."
                await self.original_interaction.edit_original_response(content="Selection timed out.", view=self)
            except discord.NotFound:
                pass # Original message might have been deleted
            except Exception as e:
                print(f"Error updating view on timeout: {e}") # Log

# --- Slash Commands ---

@client.tree.command(name="help", description="Shows information about the Book Finder bot.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.user_install()
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📚 Book Finder Bot Help",
        description="This bot helps you search for books, articles, and fiction on Library Genesis.",
        color=discord.Color.blue()
    )
    embed.add_field(
        name="How to Use",
        value=(
            "1. Use the `/search` command.\n"
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


@client.tree.command(name="search", description="Search for books, fiction, or articles.")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.user_install()
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
async def search_command(interaction: discord.Interaction, query: str, category: app_commands.Choice[str]):
    search_topic = category.value
    search_topic_name = category.name

    await interaction.response.defer(thinking=True) # Acknowledge command, need time to search

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
        description=f"Found {len(results)} results. Select one below to attempt download.",
        color=discord.Color.green()
    )

    # Add limited fields for overview, full details in dropdown description
    for i, item in enumerate(results[:5]): # Show first 5 in embed for preview
         embed.add_field(
             name=f"{i+1}. {item['title'][:100]}", # Limit title length in field name
             value=f"Author(s): {item.get('authors', 'N/A')[:100]}\n"
                   f"Format: {item.get('extension', 'N/A')} | Size: {item.get('size', 'N/A')} | Lang: {item.get('language', 'N/A')}",
             inline=False
         )
    if len(results) > 5:
         embed.set_footer(text=f"Showing first 5 of {len(results)} results. Use dropdown for all.")
    else:
        embed.set_footer(text=f"Total results: {len(results)}")


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
        try:
            client.run(BOT_TOKEN)
        except discord.LoginFailure:
            print("ERROR: Invalid Discord Bot Token. Please check your token.")
        except Exception as e:
            print(f"An error occurred while running the bot: {e}")
