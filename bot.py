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
import traceback # For detailed error logging

# --- Flask Setup (for Render deployment) ---
app = Flask(__name__)
PORT = int(os.environ.get('PORT', 10000)) # Render provides PORT env var

@app.route('/')
def home():
    # Simple endpoint to confirm the web server is running
    bot_status = "Unknown"
    try:
        # Check if client exists and is ready (handle potential NameError before client is defined)
        if 'client' in globals() and isinstance(client, BookFinderBot) and client.is_ready():
            bot_status = "Online"
        elif 'client' in globals() and isinstance(client, BookFinderBot):
            bot_status = "Starting..."
        else:
            bot_status = "Not Initialized"
    except NameError:
         bot_status = "Not Initialized"
    except Exception as e:
        bot_status = f"Error checking status: {e}"

    return f"Flask server running for Discord Bot. Bot status: {bot_status}"

def run_flask():
    # Run Flask app in a separate thread
    # Use '0.0.0.0' to bind to all interfaces, required by Render
    print(f"Starting Flask server on port {PORT}...")
    # Use a production-ready WSGI server like waitress or gunicorn in production instead of Flask's development server
    # For simplicity here, we use Flask's built-in server. For Render, this is usually fine.
    try:
        app.run(host='0.0.0.0', port=PORT)
    except Exception as e:
        print(f"Flask server failed to start: {e}")
        traceback.print_exc()


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
        traceback.print_exc()
        return []

    # --- Parsing logic ---
    try:
        soup = BeautifulSoup(response.text, 'lxml')
        main_table = None
        table_class = 'catalog' if search_topic in ['fiction', 'scimag'] else 'c'
        results_tables = soup.find_all('table', class_=table_class)

        if not results_tables:
            print(f"Error: No table with class='{table_class}'.") # Log
            return []
        else:
            for table in results_tables:
                 header_row = table.find('tr')
                 if header_row and header_row.find(['td', 'th'], string=re.compile(r'Author|Title|DOI|Journal|Series', re.I)):
                     main_table = table
                     print(f"Found results table (class='{table_class}') using header check.") # Log
                     break
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

        idx = {'authors': 1, 'title': 2, 'publisher': 3, 'year': 4, 'pages': 5, 'language': 6, 'size': 7, 'extension': 8, 'mirror1': 9, 'mirror2': 10, 'mirrors': -1, 'file_info': -1}
        if search_topic == 'fiction': idx = {'authors': 0, 'series': 1, 'title': 2, 'language': 3, 'file_info': 4, 'mirrors': 5, 'publisher': -1, 'year': -1, 'pages': -1, 'extension': -1, 'size': -1, 'mirror1': -1, 'mirror2': -1}
        elif search_topic == 'scimag': idx = {'authors': 0, 'title': 1, 'journal': 2, 'size': 3, 'mirrors': 4, 'publisher': -1, 'year': -1, 'pages': -1, 'extension': -1, 'mirror1': -1, 'mirror2': -1}

        for row_index, row in enumerate(rows[1:], 1):
            cells = row.find_all('td')
            max_defined_index = max((v for k, v in idx.items() if v is not None and v >= 0), default=-1)
            if len(cells) <= max_defined_index:
                print(f"Skipping row {row_index}: Not enough cells ({len(cells)} found, max index {max_defined_index}).")
                continue

            try:
                # --- Variable extraction ---
                title="N/A"; authors="N/A"; language="N/A"; size="N/A"; extension="N/A"; publisher=''; year=''; pages=''
                details_url=None; target_url_mirror1=None; target_url_mirror2=None

                title_idx = idx.get('title', -1)
                if title_idx >= 0 and len(cells) > title_idx:
                     title_cell = cells[title_idx]; link_tag = title_cell.find('a', href=True)
                     if link_tag:
                          title = link_tag.get_text(separator=' ', strip=True); title = re.sub(r'\s*\[?\d{10,13}[X]?\]?\s*$', '', title).strip()
                          relative_url = link_tag['href']; details_url = urllib.parse.urljoin(search_url, relative_url)
                     else: title = title_cell.get_text(strip=True)
                     if not title: title = "N/A" # Ensure title is not empty
                else:
                    print(f"Skipping row {row_index}: Cannot find title cell at index {title_idx}.")
                    continue # Skip row if no title found

                authors_idx = idx.get('authors', -1);
                if authors_idx >= 0 and len(cells) > authors_idx: authors = cells[authors_idx].get_text(strip=True) or "N/A"
                publisher_idx = idx.get('publisher', idx.get('journal', -1));
                if publisher_idx >= 0 and len(cells) > publisher_idx: publisher = cells[publisher_idx].get_text(strip=True)
                year_idx = idx.get('year', -1);
                if year_idx >= 0 and len(cells) > year_idx: year = cells[year_idx].get_text(strip=True)
                pages_idx = idx.get('pages', -1);
                if pages_idx >= 0 and len(cells) > pages_idx: pages = cells[pages_idx].get_text(strip=True).split('[')[0]
                language_idx = idx.get('language', -1);
                if language_idx >= 0 and len(cells) > language_idx: language = cells[language_idx].get_text(strip=True) or "N/A"
                size_idx = idx.get('size', -1);
                if size_idx >= 0 and len(cells) > size_idx: size = cells[size_idx].get_text(strip=True) or "N/A"
                extension_idx = idx.get('extension', -1);
                if extension_idx >= 0 and len(cells) > extension_idx: extension = cells[extension_idx].get_text(strip=True) or "n/a"
                file_info_idx = idx.get('file_info', -1);
                if file_info_idx >= 0 and len(cells) > file_info_idx:
                    file_info_text = cells[file_info_idx].get_text(strip=True); parts = file_info_text.split('/')
                    if len(parts) == 2: extension, size = parts[0].strip() or "n/a", parts[1].strip() or "N/A"
                    elif file_info_text: extension = file_info_text
                extension = extension.lower() if extension else 'n/a'

                mirror1_idx = idx.get('mirror1', -1); mirrors_idx = idx.get('mirrors', -1); mirror2_idx = idx.get('mirror2', -1)
                if mirror1_idx >= 0 and len(cells) > mirror1_idx:
                     mirror1_tag = cells[mirror1_idx].find('a', href=True); url_val = mirror1_tag['href'] if mirror1_tag else None
                     if url_val and url_val.startswith('http'): target_url_mirror1 = url_val
                if mirror2_idx >= 0 and len(cells) > mirror2_idx:
                     mirror2_tag = cells[mirror2_idx].find('a', href=True); url_val = mirror2_tag['href'] if mirror2_tag else None
                     if url_val and url_val.startswith('http'): target_url_mirror2 = url_val
                elif mirrors_idx >= 0 and len(cells) > mirrors_idx: # Fallback for combined mirrors column
                     mirrors_cell = cells[mirrors_idx]; mirror_links = mirrors_cell.find_all('a', href=True)
                     found_mirrors = [link['href'] for link in mirror_links if link.get('href', '').startswith('http')]
                     if len(found_mirrors) > 0: target_url_mirror1 = found_mirrors[0]
                     if len(found_mirrors) > 1: target_url_mirror2 = found_mirrors[1]

                if not target_url_mirror1 and details_url:
                     print(f"  No explicit Mirror 1 URL for '{title}'. Will use details page if selected.") # Log
                     target_url_mirror1 = details_url

                if target_url_mirror1 or target_url_mirror2:
                    results.append({
                        "title": title, "authors": authors, "publisher": publisher, "year": year,
                        "pages": pages, "language": language, "size": size, "extension": extension,
                        "mirror1_url": target_url_mirror1, "mirror2_url": target_url_mirror2,
                        "details_url": details_url
                    })
                else:
                    print(f"Skipping '{title}' - No usable mirror or details URLs found.") # Log
                    continue
            except Exception as e:
                print(f"Error parsing row {row_index} for title potentially starting with '{title[:50]}...': {e}") # Log detailed error on server
                traceback.print_exc() # Log full traceback for debugging
                continue # Skip malformed rows

        return results

    except Exception as e:
        print(f"Unexpected error during results parsing: {e}")
        traceback.print_exc()
        return []


async def get_libgen_download_link_async(page_url: str):
    """Fetches mirror page async, finds direct 'GET' link or SciMag/alternative."""
    print(f"\nAsync Fetching details/mirror page: {page_url}") # Log
    if not page_url or not page_url.startswith('http'):
        print(f"Error: Invalid page URL: {page_url}") # Log
        return None

    try:
        parsed_uri = urllib.parse.urlparse(page_url)
        mirror_host = parsed_uri.netloc
    except ValueError:
        print(f"Error: Could not parse mirror page URL: {page_url}") # Log
        return None

    try:
        loop = asyncio.get_running_loop()
        current_headers = http_session.headers.copy()
        referer = http_session.headers.get('Referer', LIBGEN_BASE_URL + '/') # Use last known referer or fallback
        current_headers['Referer'] = referer

        response = await loop.run_in_executor(
            None,
            lambda: http_session.get(page_url, timeout=40, headers=current_headers, allow_redirects=True)
        )
        response.raise_for_status()
        print(f"  Page status: {response.status_code}, Final URL: {response.url}") # Log final URL
        http_session.headers['Referer'] = response.url # Update session's referer
        await asyncio.sleep(0.5) # Small delay
    except requests.exceptions.RequestException as e:
        print(f"Error fetching page {page_url}: {e}") # Log
        return None
    except Exception as e:
        print(f"Unexpected error fetching page {page_url}: {e}") # Log
        traceback.print_exc()
        return None

    # --- Parsing logic ---
    try:
        soup = BeautifulSoup(response.text, 'lxml')
        download_url = None
        download_link_tag = None
        print(f"  Analyzing page from host: {mirror_host}") # Log

        # Pattern 1 (Common style - GET link inside H2)
        h2_get_link = soup.select_one('h2 > a[href]')
        if h2_get_link and h2_get_link.get_text(strip=True).upper() == 'GET':
            download_link_tag = h2_get_link
            print("Found 'GET' link pattern (inside H2).") # Log

        # Pattern 2 (libgen.li style - specific TD background)
        if not download_link_tag:
            get_td_li = soup.find('td', {'bgcolor': '#A9F5BC'})
            if get_td_li:
                get_link_tag_in_li_td = get_td_li.find('a', href=re.compile(r'get\.php\?md5=|/get\?'))
                if get_link_tag_in_li_td and get_link_tag_in_li_td.get_text(strip=True).upper() == 'GET':
                     download_link_tag = get_link_tag_in_li_td
                     print("Found 'GET' link pattern (specific TD).") # Log

        # Pattern 3 (General 'GET' link search)
        if not download_link_tag:
             print("  Primary patterns failed. Checking any prominent 'GET' link...") # Log
             all_links = soup.find_all('a', href=True)
             for link in all_links:
                 href = link.get('href', '')
                 text = link.get_text(strip=True).upper()
                 if text == 'GET' and ('get.php' in href or '/get?' in href or 'download' in href):
                     link_parsed = urllib.parse.urlparse(href)
                     # Prefer relative links or links on the same host
                     if not link_parsed.netloc or link_parsed.netloc == mirror_host:
                          download_link_tag = link
                          print(f"Found potential 'GET' link pattern (general search): {href}") # Log
                          break # Take the first likely match

        # Pattern 4 (SciMag/Alternative Links)
        if not download_link_tag:
            print("  No 'GET' link found. Looking for SciMag/alternative links...") # Log
            all_links = soup.find_all('a', href=True)
            possible_links = []
            for link in all_links:
                href = link['href']
                # Look for known download hosts or paths
                if any(domain in href for domain in ['library.lol', 'libgen.rs', 'books.ms', 'sci-hub']) \
                   or any(path in href for path in ['/scimag/', '/get?', '/download.php', '/book/index.php']):
                     # Basic check to avoid linking to search pages etc.
                     if not any(avoid in href for avoid in ['search.php', 'browse.php']):
                        print(f"Found potential direct SciMag/alternative mirror link: {href}") # Log
                        possible_links.append(href)

            if possible_links:
                # Prefer links containing 'get' or specific domains
                preferred = [l for l in possible_links if 'get' in l or 'library.lol' in l or 'sci-hub' in l]
                if preferred:
                    download_link_tag = soup.new_tag('a', href=preferred[0]) # Create a dummy tag
                    print(f"Using preferred alternative link: {preferred[0]}") # Log
                else:
                    download_link_tag = soup.new_tag('a', href=possible_links[0]) # Fallback to first found
                    print(f"Using first found alternative link: {possible_links[0]}") # Log

        # Process found tag or fallbacks
        if download_link_tag and download_link_tag.get('href'):
            relative_or_absolute_url = download_link_tag['href']
            download_url = urllib.parse.urljoin(response.url, relative_or_absolute_url) # Use response.url as base
            print(f"  Extracted download URL: {download_url}") # Log
            return download_url
        else:
            # Fallback: If the original page_url itself looks like a direct file link
            if any(page_url.lower().endswith(ext) for ext in ['.pdf', '.epub', '.mobi', '.zip', '.djvu', '.rar', '.chm', '.azw3']):
                 print(f"Assuming page URL might be the download link: {page_url}") # Log
                 return page_url

        print("Error: Could not find any usable download link on the page.") # Log
        return None

    except Exception as e:
        print(f"Unexpected error during link parsing for {page_url}: {e}")
        traceback.print_exc()
        return None

async def download_book_to_discord(download_url: str, title: str, extension: str):
    """Downloads file async into memory, returns discord.File or None/error string."""
    if not download_url:
        return "No download URL provided."

    print(f"\nAsync Attempting to download '{title}' from {download_url}") # Log
    filename_base = safe_filename(title)
    final_extension = extension.lower() if extension and extension != 'n/a' else 'bin' # Default to .bin if unknown
    filename = f"{filename_base}.{final_extension}"

    response = None # Initialize response variable

    try:
        loop = asyncio.get_running_loop()
        current_headers = http_session.headers.copy() # Use latest headers from session
        print(f"  Using Referer: {current_headers.get('Referer')}") # Log

        # --- HEAD Request (Optional but recommended) ---
        head_failed = False
        try:
            head_response = await loop.run_in_executor(
                None,
                lambda: http_session.head(download_url, timeout=30, allow_redirects=True, headers=current_headers)
            )
            head_response.raise_for_status()
            content_length = head_response.headers.get('content-length')
            content_type = head_response.headers.get('content-type', '').lower()
            final_url_from_head = head_response.url

            print(f"  HEAD response: Status={head_response.status_code}, Type={content_type}, Length={content_length}, Final URL={final_url_from_head}")

            if content_length and int(content_length) > DISCORD_FILE_LIMIT_BYTES:
                print(f"  File size ({int(content_length)} bytes) exceeds Discord limit ({DISCORD_FILE_LIMIT_BYTES} bytes).")
                return f"File is too large ({int(content_length)/(1024*1024):.2f} MB). Max size: {DISCORD_FILE_LIMIT_MB} MB."

        except requests.exceptions.RequestException as head_err:
            print(f"  HEAD request failed: {head_err}. Proceeding with GET.")
            head_failed = True
        except Exception as head_ex:
            print(f"  Unexpected error during HEAD request: {head_ex}. Proceeding with GET.")
            head_failed = True
        # --- End HEAD Request ---


        # --- GET Request ---
        print("  Initiating GET request for download...")
        response = await loop.run_in_executor(
            None,
            lambda: http_session.get(download_url, stream=True, timeout=180, allow_redirects=True, headers=current_headers)
        )
        print(f"  Download GET status: {response.status_code}, URL: {response.url}")

        # Handle common server errors immediately
        if response.status_code in [502, 503, 504, 404, 403]:
             print(f"  Download failed: Server returned status {response.status_code}.")
             return f"Download failed (Server Status: {response.status_code}). Try the other mirror if available."

        response.raise_for_status() # Raise for other client/server errors (e.g., 4xx, 5xx not caught above)

        final_url = response.url
        content_type = response.headers.get('content-type', '').lower()
        print(f"  GET Content-Type: {content_type}")

        # Check for HTML response *after* GET request (could indicate error, login, CAPTCHA)
        if 'text/html' in content_type:
            # Check for Sci-Hub CAPTCHA specifically
            try:
                peek_bytes = await loop.run_in_executor(None, lambda: response.raw.peek(2048)) # Peek more data
                text_snippet = peek_bytes.decode('utf-8', errors='ignore')
                # More specific CAPTCHA checks
                if 'sci-hub.' in str(final_url) and \
                   ('captcha' in text_snippet.lower() or 'verify you are human' in text_snippet.lower() or '<form' in text_snippet):
                     print(">>> CAPTCHA BLOCK DETECTED on Sci-Hub page <<<")
                     response.close()
                     # Provide the Sci-Hub page URL itself for manual access
                     return f"Download failed: CAPTCHA required. Please visit the link manually: <{final_url}>"
            except Exception as snippet_err:
                 print(f"  Could not peek/decode HTML snippet: {snippet_err}")

            print(">>> DOWNLOAD FAILED: Received HTML instead of expected file type. <<<")
            response.close()
            return f"Download failed: Link led to an HTML page (possibly error/login/ad). Final URL: <{final_url}>"

        # Refine filename/extension based on headers/URL (if needed)
        content_disposition = response.headers.get('content-disposition')
        valid_extensions = ['pdf', 'epub', 'mobi', 'azw3', 'djvu', 'zip', 'rar', 'txt', 'chm'] # Keep this list updated
        if content_disposition:
            # Improved filename extraction from Content-Disposition
            disp_match = re.search('filename\*?=(?:UTF-\d\'\')?"?([^";\r\n]+)"?', content_disposition, re.IGNORECASE)
            if disp_match:
                try:
                    header_filename = urllib.parse.unquote(disp_match.group(1), errors='replace')
                    header_base, header_ext = os.path.splitext(header_filename)
                    header_ext = header_ext.lower().lstrip('.')
                    if header_ext in valid_extensions:
                        print(f"  Using filename from header: {header_filename}")
                        filename_base = safe_filename(header_base)
                        final_extension = header_ext
                    # Only override if original extension was unknown/binary
                    elif extension in ['n/a', 'bin'] and header_ext:
                         final_extension = header_ext
                         print(f"  Using extension '{final_extension}' from header (overriding '{extension}').")
                except Exception as e:
                     print(f"  Error parsing content-disposition filename: {e}")

        # Fallback extension detection based on Content-Type or URL if still needed
        if final_extension in ['bin', 'n/a']:
             type_map = {
                 'application/pdf': 'pdf', 'application/epub+zip': 'epub',
                 'application/x-mobipocket-ebook': 'mobi', 'application/vnd.amazon.ebook': 'azw3',
                 'image/vnd.djvu': 'djvu', 'application/zip': 'zip',
                 'application/vnd.rar': 'rar', 'application/x-rar-compressed': 'rar',
                 'text/plain': 'txt', 'application/vnd.ms-htmlhelp': 'chm'
             }
             content_type_base = content_type.split(';')[0].strip() # Ignore charset etc.
             if content_type_base in type_map:
                 final_extension = type_map[content_type_base]
                 print(f"  Deduced extension '{final_extension}' from Content-Type '{content_type_base}'")
             else:
                 # Last resort: check URL path
                 url_path = urllib.parse.urlparse(final_url).path
                 url_base, url_ext = os.path.splitext(url_path)
                 url_ext = url_ext.lower().lstrip('.')
                 if url_ext in valid_extensions:
                     final_extension = url_ext
                     print(f"  Deduced extension '{final_extension}' from final URL path.")
                 else:
                     print(f"  Warning: Could not determine extension from headers, type '{content_type}', or URL. Using '.{final_extension}'.")

        filename = f"{filename_base}.{final_extension}"
        print(f"  Final filename: {filename}")

        # Download into memory (BytesIO buffer)
        file_buffer = io.BytesIO()
        downloaded_size = 0
        limit_exceeded = False

        # Check Content-Length from GET response if HEAD failed or was missing
        content_length_get = response.headers.get('content-length')
        if not head_failed and content_length_get and int(content_length_get) > DISCORD_FILE_LIMIT_BYTES:
             print(f"  File size from GET header ({int(content_length_get)} bytes) exceeds limit.")
             response.close()
             return f"File is too large ({int(content_length_get)/(1024*1024):.2f} MB). Max size: {DISCORD_FILE_LIMIT_MB} MB."

        print("  Starting download stream...")
        for chunk in response.iter_content(chunk_size=8192*4): # Slightly larger chunk size
            if chunk: # filter out keep-alive new chunks
                downloaded_size += len(chunk)
                if downloaded_size > DISCORD_FILE_LIMIT_BYTES:
                    limit_exceeded = True
                    print(f"  Download aborted: File size ({downloaded_size}+ bytes) exceeds {DISCORD_FILE_LIMIT_MB} MB limit.")
                    response.close() # Crucial: stop the download
                    break
                file_buffer.write(chunk)

        if limit_exceeded:
            file_buffer.close()
            # Calculate approximate size for the error message
            return f"File is too large ({downloaded_size/(1024*1024):.2f} MB+). Max size: {DISCORD_FILE_LIMIT_MB} MB."

        # If download completed within limits
        file_buffer.seek(0) # Reset buffer position to the beginning
        print(f"  Downloaded {downloaded_size} bytes into memory.")
        discord_file = discord.File(fp=file_buffer, filename=filename)
        # fp will be closed by discord.File upon sending/destruction
        return discord_file

    except requests.exceptions.Timeout as e:
        print(f"\nError: Download timed out for {download_url}")
        traceback.print_exc()
        return f"Download timed out: {e}. The server might be slow or unresponsive."
    except requests.exceptions.RequestException as e:
        print(f"\nError during download request: {e}")
        traceback.print_exc()
        status_code = e.response.status_code if hasattr(e, 'response') and e.response is not None else "N/A"
        return f"Download connection error (Status: {status_code}): {e}. Please check the URL or try again later."
    except Exception as e:
        print(f"\nUnexpected error during download processing: {e}")
        traceback.print_exc()
        return f"An unexpected error occurred during download: {e}"
    finally:
        # Ensure the response connection is closed if it exists and wasn't closed earlier
        if response and hasattr(response, 'close') and not response.raw.closed:
             print("  Ensuring download response connection is closed.")
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
        try:
            synced = await self.tree.sync()
            print(f'Synced {len(synced)} commands globally.')
        except Exception as e:
            print(f"Failed to sync commands: {e}")
            traceback.print_exc()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print(f'discord.py version: {discord.__version__}')
        print('------')

client = BookFinderBot(intents=intents)

# --- Discord UI Components ---

class SearchResultSelectView(View):
    def __init__(self, search_results, original_interaction: discord.Interaction):
        super().__init__(timeout=300) # 5 minute timeout for selection
        self.search_results = search_results
        self.original_interaction = original_interaction

        # Limit options to Discord's max of 25
        options_limited = search_results[:25]
        select_options = []
        for i, item in enumerate(options_limited):
            label_text = f"{i+1}. {item['title']}"
            final_label = label_text[:98] + ".." if len(label_text) > 100 else label_text

            authors_part = item.get('authors', 'N/A')[:30]
            year_part = item.get('year', 'N/A')
            ext_part = item.get('extension', 'N/A')[:10]
            size_part = item.get('size', 'N/A')[:15]
            desc_text = f"{authors_part} ({year_part}) | {ext_part} | {size_part}"
            final_desc = desc_text[:98] + ".." if len(desc_text) > 100 else desc_text

            # Ensure values are strings
            value_str = str(i)

            select_options.append(
                discord.SelectOption(label=final_label, description=final_desc, value=value_str)
            )

        self.select_menu = Select(
            placeholder="Select a book/article to download (Up to 25 shown)...",
            options=select_options,
            custom_id="book_select_dropdown" # Added custom ID for potential state recovery/debugging
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    # <<< THIS IS THE UPDATED CALLBACK METHOD >>>
    async def select_callback(self, interaction: discord.Interaction):
        # Ensure only the original user can interact
        if interaction.user != self.original_interaction.user:
             await interaction.response.send_message("You didn't initiate this search.", ephemeral=True)
             return

        # Check if interaction has expired (can happen with long processing)
        if interaction.is_expired():
            print("Interaction expired before processing selection.")
            # Maybe try to send an ephemeral message if possible?
            # await interaction.followup.send("Sorry, the interaction timed out.", ephemeral=True) # Might fail
            return

        selected_index = -1
        try:
            # Ensure selected value is valid before converting to int
            if not self.select_menu.values or not self.select_menu.values[0].isdigit():
                await interaction.response.send_message("Invalid selection value.", ephemeral=True)
                return
            selected_index = int(self.select_menu.values[0])

            if selected_index < 0 or selected_index >= len(self.search_results):
                await interaction.response.send_message(f"Selection index ({selected_index}) out of bounds.", ephemeral=True)
                return
        except ValueError:
             await interaction.response.send_message("Invalid selection format.", ephemeral=True)
             return
        except Exception as e:
            print(f"Error processing selection value: {e}")
            traceback.print_exc()
            await interaction.response.send_message("Error processing your selection.", ephemeral=True)
            return

        selected_item = self.search_results[selected_index]

        # 1. Disable the select menu immediately & Acknowledge Interaction
        self.select_menu.disabled = True
        self.select_menu.placeholder = f"Processing: {selected_item['title'][:50]}..."
        try:
            # Respond to the interaction *first* before heavy processing
            # Using edit_message as the initial response to the button click
            await interaction.response.edit_message(view=self)
        except discord.NotFound:
            print("Interaction not found during edit_message (likely deleted). Aborting.")
            return
        except discord.HTTPException as e:
            # 40060: Interaction has already been responded to (might happen in rare race conditions)
            if e.code == 40060:
                print("Interaction already responded to, proceeding with followup.")
            else:
                print(f"Error editing message to disable select (continuing): {e}")
            # If edit fails, we MUST use followup for the next messages.

        # 2. Send a 'thinking' or 'attempting' message using followup
        status_message = None
        try:
            thinking_message = f"⏳ Attempting to download '{selected_item['title']}'..."
            # Use followup since we've already responded/acknowledged via edit_message or handled the 40060 error
            status_message = await interaction.followup.send(thinking_message[:2000], wait=True, ephemeral=False)
        except discord.NotFound:
            print("Interaction not found during followup.send (likely deleted). Aborting.")
            return
        except Exception as e:
            print(f"Error sending status followup: {e}")
            traceback.print_exc()
            # Attempt to edit the original message with an error if followup fails
            try:
                await interaction.edit_original_response(content="Error starting download process.", view=self)
            except Exception: pass # Ignore errors editing original if followup failed
            return

        # --- Attempt Download ---
        download_result = None
        error_message = "Download failed. Unknown reason."
        mirror1_url = selected_item.get('mirror1_url')
        direct_download_link = None # Initialize variable to store the link

        if mirror1_url:
            print(f"Attempting Mirror 1: {mirror1_url}")
            direct_download_link = await get_libgen_download_link_async(mirror1_url) # Store the link

            if direct_download_link:
                download_result = await download_book_to_discord(
                    direct_download_link,
                    selected_item['title'],
                    selected_item['extension']
                )
            else:
                # This specific error means we couldn't even get a link from Mirror 1 page
                error_message = "Could not find a download link on the first mirror page."
                print(error_message)
        else:
            error_message = "Mirror 1 URL was not found in the search results."
            print(error_message)

        # --- Process Download Result ---
        # 3. Edit the status message with the final result
        if status_message is None:
             print("Error: Status message object is None. Cannot edit with final result.")
             # Potentially try editing original interaction response as fallback
             # await interaction.edit_original_response(content="Processing complete, but status message failed.", view=self)
             return # Cannot proceed reliably

        if isinstance(download_result, discord.File):
            success_message = f"✅ Download successful for **{selected_item['title']}**!"
            try:
                 # Edit the followup message WITH the file
                 await status_message.edit(
                     content=success_message[:2000],
                     attachments=[download_result]
                 )
            except discord.HTTPException as e:
                 print(f"Discord API error editing status message with file: {e}")
                 traceback.print_exc()
                 # Fallback: Try sending as a new message if edit fails
                 try:
                     await interaction.followup.send(success_message[:2000], file=download_result)
                 except discord.HTTPException as e2:
                     print(f"Discord API error sending file (followup): {e2}")
                     await status_message.edit(
                         content=f"⚠️ Could not send the file via Discord (Error: {e2.code}). It might be slightly too large or another issue occurred.",
                         attachments=[] # Ensure no attachments on error edit
                     )
                     # Offer Mirror 2 link in a separate followup
                     mirror2_url = selected_item.get('mirror2_url')
                     if mirror2_url:
                         await interaction.followup.send(
                             f"You can try downloading manually using Mirror 2:\n"
                             f"<{mirror2_url}>\n"
                             f"(Remember to click 'GET' on the page, possibly twice due to pop-ups)."
                         )
                 except Exception as ex_followup:
                      print(f"Unexpected error sending followup file: {ex_followup}")
                      traceback.print_exc()
                      await status_message.edit(content="Error sending file after initial failure.", attachments=[])

            except Exception as ex:
                 print(f"Unexpected error sending file: {ex}")
                 traceback.print_exc()
                 await status_message.edit(content=f"An unexpected error occurred while sending the file.", attachments=[])

        else: # Download failed
            # Update error_message if download_result provided a specific string
            if isinstance(download_result, str):
                 error_message = download_result

            print(f"Mirror 1 failed for '{selected_item['title']}'. Reason: {error_message}")
            final_error_message = ""

            # --- NEW LOGIC: Check if failure is due to size limit ---
            if error_message.startswith("File is too large"):
                size_info = "size limit"
                match = re.search(r'\((.*MB.*)\)', error_message) # Extract size like (X.XX MB)
                if match:
                    size_info = match.group(1)

                final_error_message = f"❌ Download failed: File is too large for Discord ({size_info}).\n"
                if direct_download_link:
                    final_error_message += (
                        f"\nYou can download it directly using the Mirror 1 link:\n"
                        f"<{direct_download_link}>"
                    )
                else:
                    # Should be rare if size limit was hit, but handle it
                    final_error_message += "\nCould not retrieve the direct download link."
                # Optionally still mention Mirror 2 as secondary backup
                mirror2_url = selected_item.get('mirror2_url')
                if mirror2_url:
                    final_error_message += f"\n\nIf the above link fails, you can *also* try Mirror 2 manually: <{mirror2_url}>"

            # --- ELSE: Handle other failures ---
            else:
                final_error_message = f"❌ Download via Mirror 1 failed: {error_message}\n"
                mirror2_url = selected_item.get('mirror2_url')
                if mirror2_url:
                    final_error_message += (
                        f"\nYou can try **Mirror 2** manually in your browser:\n"
                        f"<{mirror2_url}>\n"
                        f"*(Click GET, close ad, click GET again)*"
                    )
                elif selected_item.get('details_url') and selected_item.get('details_url') != mirror1_url: # Offer details if different from M1 attempt
                     final_error_message += f"\nYou can visit the details page for more options: <{selected_item.get('details_url')}>"
                else:
                    final_error_message += "\nNo alternative Mirror 2 URL was found for this item."
            # --- End of specific failure handling ---

            # Edit the status message with the appropriate error
            try:
                await status_message.edit(content=final_error_message[:2000], attachments=[])
            except discord.NotFound:
                 print("Status message not found during final error edit (likely deleted).")
            except discord.HTTPException as e:
                 print(f"Error editing status message with final error: {e}")
                 traceback.print_exc()
            except Exception as e:
                 print(f"Unexpected error editing status message with final error: {e}")
                 traceback.print_exc()

    # on_timeout remains mostly the same, but ensure it edits content/view correctly
    async def on_timeout(self):
         print(f"View timed out for interaction {self.original_interaction.id}")
         if self.original_interaction and not self.original_interaction.is_expired():
            try:
                # Disable all components without checking original message content
                for item in self.children:
                    if hasattr(item, 'disabled'):
                        item.disabled = True
                # Update placeholder if it's a select menu
                if isinstance(self.select_menu, Select):
                    self.select_menu.placeholder = "Selection timed out."

                # Edit the original message the view was attached to
                await self.original_interaction.edit_original_response(content="Selection timed out.", view=self)
                print(f"Successfully edited original message on timeout for interaction {self.original_interaction.id}")
            except discord.NotFound:
                print(f"Original interaction message not found on timeout for {self.original_interaction.id}.")
            except discord.HTTPException as e:
                # Ignore errors if the interaction was already responded to or deleted
                if e.code == 40060: # Interaction has already been responded to
                     print("Interaction already responded to before timeout edit.")
                else:
                    print(f"HTTP error updating view on timeout for {self.original_interaction.id}: {e}")
                    traceback.print_exc()
            except Exception as e:
                print(f"Unexpected error updating view on timeout for {self.original_interaction.id}: {e}")
                traceback.print_exc()
         else:
             print(f"Original interaction {self.original_interaction.id if self.original_interaction else 'N/A'} expired or missing on timeout.")

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
            "1. Use the `/findbook` command.\n"
            "2. Enter your search query (title, author, ISBN, etc.).\n"
            "3. Choose a category (Non-Fiction, Fiction, Articles).\n"
            "4. If results are found, a dropdown menu will appear (up to 25 items).\n"
            "5. Select an item from the dropdown to attempt download.\n"
            f"   - Files under **{DISCORD_FILE_LIMIT_MB}MB** will be sent directly.\n"
            f"   - For larger files, a direct download link from **Mirror 1** will be provided.\n"
            f"   - If Mirror 1 fails for other reasons, you'll get a link to try **Mirror 2** manually."
        ),
        inline=False
    )
    embed.add_field(
        name="⚠️ Copyright & Disclaimer",
        value=(
            "Please use this bot responsibly and respect copyright laws. "
            "Downloading copyrighted material without permission may be illegal in your country. "
            "This tool is provided for informational purposes and ease of access to publicly available resources. "
            "The developers are not responsible for misuse."
        ),
        inline=False
    )
    embed.add_field(
        name="💡 Search Tips",
        value=(
            "- Be specific: Include author names if known.\n"
            "- Use ISBN/DOI: Provides more exact matches.\n"
            "- Check Categories: If not found in one, try another relevant category.\n"
            "- Libgen Domains: The bot uses libgen.is, but mirrors change. Functionality depends on site availability."
        ),
        inline=False
    )
    embed.set_footer(text=f"Bot created by {BOT_CREATOR}")

    try:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        print(f"Error sending help message: {e}")
        traceback.print_exc()


@client.tree.command(name="findbook", description="Search for books, fiction, or articles.")
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
async def findbook_command(interaction: discord.Interaction, query: str, category: app_commands.Choice[str]):
    search_topic = category.value
    search_topic_name = category.name

    try:
        # Defer publicly BEFORE any network requests or heavy processing
        await interaction.response.defer(thinking=True, ephemeral=False)
    except discord.NotFound:
        print("Interaction not found during initial defer. Aborting command.")
        return
    except discord.HTTPException as e:
        print(f"HTTP error during initial defer: {e}")
        # Attempt to send an ephemeral error if defer fails
        try: await interaction.followup.send("Error starting search process. Please try again.", ephemeral=True)
        except: pass
        return
    except Exception as e:
        print(f"Unexpected error during initial defer: {e}")
        traceback.print_exc()
        try: await interaction.followup.send("An unexpected error occurred. Please try again.", ephemeral=True)
        except: pass
        return


    if search_topic == 'magz':
        try:
            magz_url = f"http://magzdb.org/makelist?t={urllib.parse.quote_plus(query)}"
            await interaction.followup.send(
                f"Magazine searches are handled by MagzDB.\n"
                f"Please visit this link to see results for '{query}':\n<{magz_url}>"
            )
        except Exception as e:
            print(f"Error sending MagzDB link: {e}")
            traceback.print_exc()
            await interaction.followup.send("Failed to generate the MagzDB link.")
        return

    # Perform the async search
    results = await search_libgen_async(query, search_topic=search_topic)

    if not results:
        try:
            await interaction.followup.send(f"No results found for '{query}' in '{search_topic_name}'. Try different keywords or categories.")
        except Exception as e:
             print(f"Error sending 'no results' message: {e}")
        return

    # Format results for Discord Embed
    try:
        embed_desc = f"Found {len(results)} results. Select one below to attempt download (up to 25 shown)."
        embed = discord.Embed(
            title=f"Search Results for '{query}' ({search_topic_name})",
            description=embed_desc[:4090], # Truncate description
            color=discord.Color.green()
        )

        results_to_preview = results[:5] # Show first 5 in embed preview
        added_fields = 0
        for i, item in enumerate(results_to_preview):
             if added_fields >= 5: break # Ensure max 5 preview fields

             field_name_text = f"{i+1}. {item['title']}"
             final_field_name = field_name_text[:254] + ".." if len(field_name_text) > 256 else field_name_text # Max 256

             authors_val = item.get('authors', 'N/A')[:200]
             ext_val = item.get('extension', 'N/A')
             size_val = item.get('size', 'N/A')
             lang_val = item.get('language', 'N/A')
             field_value_text = f"Author(s): {authors_val}\nFormat: {ext_val} | Size: {size_val} | Lang: {lang_val}"
             final_field_value = field_value_text[:1022] + ".." if len(field_value_text) > 1024 else field_value_text # Max 1024

             if final_field_name and final_field_value:
                embed.add_field(name=final_field_name, value=final_field_value, inline=False)
                added_fields += 1
             else:
                print(f"Warning: Skipped adding embed field for item {i} due to empty name/value after truncation.")


        footer_text = f"Total results: {len(results)}."
        if len(results) > 25:
            footer_text += " Showing the first 25 in the dropdown."
        elif len(results) > added_fields: # If more results exist than shown in preview
             footer_text += f" Showing {added_fields} in preview."

        embed.set_footer(text=footer_text[:2048]) # Truncate footer

        # Create the View with the Select dropdown (uses the updated class)
        view = SearchResultSelectView(results, interaction)

        # Send the initial response with results
        await interaction.followup.send(embed=embed, view=view)

    except Exception as e:
        print(f"Error preparing or sending results message: {e}")
        traceback.print_exc()
        # Try sending a simple error message if embed/view failed
        try: await interaction.followup.send("Error displaying search results.")
        except: pass


# --- Run the Bot ---
if __name__ == "__main__":
    if BOT_TOKEN is None:
        print("###########################################################")
        print("ERROR: Discord Bot Token not found.")
        print("Please set the DISCORD_BOT_TOKEN environment variable.")
        print("###########################################################")
    else:
        print("Starting Flask server thread...")
        # Start Flask in a background thread
        flask_thread = threading.Thread(target=run_flask, daemon=True) # Use daemon=True so it exits when main thread exits
        flask_thread.start()

        print("Starting Discord bot...")
        try:
            # Start the Discord bot (blocking call)
            client.run(BOT_TOKEN, reconnect=True) # Enable auto-reconnect
        except discord.LoginFailure:
            print("###########################################################")
            print("ERROR: Invalid Discord Bot Token. Please check your token.")
            print("###########################################################")
        except discord.PrivilegedIntentsRequired:
            print("###########################################################")
            print("ERROR: Privileged Intents (like Message Content) are required but not enabled.")
            print("Please enable the necessary intents in your bot's application settings on the Discord Developer Portal.")
            print("###########################################################")
        except Exception as e:
            print(f"An critical error occurred while running the bot: {e}")
            traceback.print_exc()
