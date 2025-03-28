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
    bot_status = 'Online' if client and client.is_ready() else 'Starting...'
    return f"Flask server running for Discord Bot. Bot status: {bot_status}"

def run_flask():
    # Run Flask app in a separate thread
    # Use '0.0.0.0' to bind to all interfaces, required by Render
    print(f"Starting Flask server on port {PORT}...")
    # Disable Flask's default logging to avoid duplicate startup messages if preferred
    # import logging
    # log = logging.getLogger('werkzeug')
    # log.setLevel(logging.ERROR)
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
MAX_RESULTS_TO_SHOW = 25 # Max options in Discord Select Menu
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
    # Request slightly more than dropdown limit to show accurate total count if needed
    params['res'] = MAX_RESULTS_TO_SHOW + 5
    if search_topic == 'libgen': params.update({'lg_topic': 'libgen', 'open': 0, 'view': 'simple', 'phrase': 1, 'column': 'def'})

    try:
        loop = asyncio.get_running_loop()
        current_headers = http_session.headers.copy()
        current_headers['Referer'] = LIBGEN_BASE_URL + '/'

        response = await loop.run_in_executor(
            None,
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
    print(f"Found {len(rows) - 1} results rows. Parsing...") # Log

    idx = {'authors': 1, 'title': 2, 'publisher': 3, 'year': 4, 'pages': 5, 'language': 6, 'size': 7, 'extension': 8, 'mirror1': 9, 'mirror2': 10, 'mirrors': -1, 'file_info': -1}
    if search_topic == 'fiction': idx = {'authors': 0, 'series': 1, 'title': 2, 'language': 3, 'file_info': 4, 'mirrors': 5, 'publisher': -1, 'year': -1, 'pages': -1, 'extension': -1, 'size': -1, 'mirror1': -1, 'mirror2': -1}
    elif search_topic == 'scimag': idx = {'authors': 0, 'title': 1, 'journal': 2, 'size': 3, 'mirrors': 4, 'publisher': -1, 'year': -1, 'pages': -1, 'extension': -1, 'mirror1': -1, 'mirror2': -1}

    for row_index, row in enumerate(rows[1:]): # Use enumerate for logging index
        cells = row.find_all('td')
        max_defined_index = max((idx.get(k, -1) for k in idx if idx.get(k,-1) is not None), default=-1) # Ensure positive indices
        if len(cells) <= max_defined_index:
             print(f"Skipping row {row_index+1}: Not enough cells ({len(cells)} found, max index needed {max_defined_index}).")
             continue

        try:
            title="N/A"; authors="N/A"; language="N/A"; size="N/A"; extension="N/A"; publisher=''; year=''; pages=''
            details_url=None; target_url_mirror1=None; target_url_mirror2=None

            title_idx = idx.get('title', -1)
            if title_idx >= 0 and len(cells) > title_idx:
                 title_cell = cells[title_idx]; link_tag = title_cell.find('a', href=True)
                 if link_tag:
                      title = link_tag.get_text(separator=' ', strip=True); title = re.sub(r'\s*\[?\d{10,13}[X]?\]?\s*$', '', title).strip()
                      relative_url = link_tag['href']; details_url = urllib.parse.urljoin(search_url, relative_url)
                 else: title = title_cell.get_text(strip=True)
            else:
                 print(f"Skipping row {row_index+1}: Could not find title at index {title_idx}.")
                 continue # Cannot proceed without a title

            authors_idx = idx.get('authors', -1);
            if authors_idx >= 0 and len(cells) > authors_idx: authors = cells[authors_idx].get_text(strip=True)
            publisher_idx = idx.get('publisher', idx.get('journal', -1));
            if publisher_idx >= 0 and len(cells) > publisher_idx: publisher = cells[publisher_idx].get_text(strip=True)
            year_idx = idx.get('year', -1);
            if year_idx >= 0 and len(cells) > year_idx: year = cells[year_idx].get_text(strip=True)
            pages_idx = idx.get('pages', -1);
            if pages_idx >= 0 and len(cells) > pages_idx: pages = cells[pages_idx].get_text(strip=True).split('[')[0]
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
                elif file_info_text: extension = file_info_text # Assume only extension if not split by '/' and not empty
            extension = extension.lower() if extension else 'n/a'

            mirror1_idx = idx.get('mirror1', -1); mirrors_idx = idx.get('mirrors', -1); mirror2_idx = idx.get('mirror2', -1)
            if mirror1_idx >= 0 and len(cells) > mirror1_idx:
                 mirror1_tag = cells[mirror1_idx].find('a'); url_val = mirror1_tag.get('href') if mirror1_tag else None # Use .get for safety
                 if url_val and url_val.startswith('http'): target_url_mirror1 = url_val
            if mirror2_idx >= 0 and len(cells) > mirror2_idx:
                 mirror2_tag = cells[mirror2_idx].find('a'); url_val = mirror2_tag.get('href') if mirror2_tag else None
                 if url_val and url_val.startswith('http'): target_url_mirror2 = url_val
            elif mirrors_idx >= 0 and len(cells) > mirrors_idx:
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
            current_title_for_log = title if 'title' in locals() and title != "N/A" else f"Row {row_index+1}"
            print(f"Error parsing row {row_index+1} (title: '{current_title_for_log[:50]}...'): {e}") # Log detailed error on server
            import traceback
            traceback.print_exc() # Print full traceback for debugging parse errors
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
        if not mirror_host: # Handle cases like relative URLs passed erroneously
             print(f"Error: Could not determine host from URL: {page_url}")
             return None
    except ValueError as e:
        print(f"Error: Could not parse mirror page URL '{page_url}': {e}") # Log
        return None

    try:
        loop = asyncio.get_running_loop()
        current_headers = http_session.headers.copy()
        referer = http_session.headers.get('Referer', LIBGEN_BASE_URL + '/')
        current_headers['Referer'] = referer

        response = await loop.run_in_executor(
            None,
            lambda: http_session.get(page_url, timeout=40, headers=current_headers, allow_redirects=True)
        )
        response.raise_for_status()
        print(f"  Page status: {response.status_code}, Final URL: {response.url}") # Log final URL
        http_session.headers['Referer'] = response.url
        await asyncio.sleep(0.5)
    except requests.exceptions.RequestException as e:
        print(f"Error fetching page {page_url}: {e}") # Log
        return None
    except Exception as e:
        print(f"Unexpected error fetching page {page_url}: {e}") # Log
        return None

    soup = BeautifulSoup(response.text, 'lxml')
    download_url = None
    download_link_tag = None
    print(f"  Analyzing page from host: {mirror_host}") # Log

    # Pattern 1 (libgen.li style) - Check relevance for current mirrors
    get_td_li = soup.find('td', {'bgcolor': '#A9F5BC'})
    if get_td_li:
        get_link_tag_in_li_td = get_td_li.find('a', href=re.compile(r'get\.php\?md5=|/get\?|key=', re.I)) # Broader regex
        if get_link_tag_in_li_td and get_link_tag_in_li_td.get_text(strip=True).upper() == 'GET':
            download_link_tag = get_link_tag_in_li_td
            print("Found 'GET' link pattern 1 (specific TD).") # Log

    # Pattern 2 (Common style - H2)
    if not download_link_tag:
        print("  Pattern 1 failed/NA. Checking GET link inside H2...") # Log
        # Check multiple H tags just in case
        h_tags = soup.select('h1 > a[href], h2 > a[href], h3 > a[href]')
        for h_link in h_tags:
            if h_link.get_text(strip=True).upper() == 'GET':
                download_link_tag = h_link
                print(f"Found 'GET' link pattern 2 (inside {h_link.parent.name}).") # Log
                break

    # Pattern 3 (General 'GET')
    if not download_link_tag:
         print("  Pattern 2 failed/NA. Checking any prominent 'GET' link...") # Log
         all_links = soup.find_all('a', href=True)
         for link in all_links:
             href = link.get('href', '')
             text = link.get_text(strip=True).upper()
             # Add common download URL patterns
             if text == 'GET' and ('get.php' in href or '/get?' in href or 'download' in href or 'main/' in href or 'md5/' in href):
                 try:
                     link_parsed = urllib.parse.urlparse(href)
                     # Allow relative links or links matching the current mirror host
                     if not link_parsed.netloc or link_parsed.netloc == mirror_host:
                          download_link_tag = link
                          print(f"Found potential 'GET' link pattern 3 (general search): {href}") # Log
                          break
                 except ValueError:
                     continue # Ignore badly formed hrefs

    # Pattern 4 (SciMag/Alternative) - Use as fallback if GET fails
    if not download_link_tag:
        print("  Primary 'GET' failed. Looking for SciMag/alternative links...") # Log
        all_links = soup.find_all('a', href=True)
        possible_links = []
        # Prioritize known good mirrors/patterns
        known_domains = ['library.lol', 'libgen.rs', 'books.ms', 'sci-hub']
        for link in all_links:
            href = link['href']
            try:
                 link_parsed = urllib.parse.urlparse(href)
                 if link_parsed.netloc and any(domain in link_parsed.netloc for domain in known_domains):
                     if '/scimag/' in href or '/get?' in href or 'sci-hub' in href or 'main/' in href or 'md5/' in href:
                          print(f"Found potential direct SciMag/alternative mirror link: {href}") # Log
                          possible_links.append(href)
            except ValueError:
                 continue
        if possible_links:
            # Create a dummy tag to pass the URL to the final processing step
            download_link_tag = soup.new_tag('a', href=possible_links[0])
            print("Using alternative SciMag/mirror link as primary.") # Log


    if download_link_tag and download_link_tag.get('href'):
        relative_or_absolute_url = download_link_tag['href']
        # Ensure the URL is absolute, using the *final* response URL as base
        download_url = urllib.parse.urljoin(response.url, relative_or_absolute_url)
        print(f"  Extracted download URL: {download_url}") # Log
        return download_url
    else:
        # Last resort: Check if the current page URL itself looks like a file
        if response.url.split('?')[0].lower().endswith(('.pdf', '.epub', '.mobi', '.zip', '.djvu')):
             print(f"Assuming final page URL might be the download link: {response.url}") # Log
             return response.url

    print("Error: Could not find any usable download link on the page.") # Log
    return None

async def download_book_to_discord(download_url: str, title: str, extension: str):
    """Downloads file async into memory, returns discord.File or None/error string."""
    if not download_url:
        return "No download URL provided."

    print(f"\nAsync Attempting to download '{title}' from {download_url}") # Log
    filename_base = safe_filename(title)
    final_extension = extension.lower() if extension and extension != 'n/a' else 'bin'
    filename = f"{filename_base}.{final_extension}"
    response = None # Initialize response variable

    try:
        loop = asyncio.get_running_loop()
        current_headers = http_session.headers.copy()
        # Ensure referer is set from the previous step
        if 'Referer' not in current_headers:
             current_headers['Referer'] = LIBGEN_BASE_URL + '/'
             print("Warning: Referer not found in session, using base URL.")
        print(f"  Using Referer: {current_headers.get('Referer')}") # Log

        # HEAD Request (optional but good practice)
        head_response = None
        try:
            head_response = await loop.run_in_executor(
                None,
                lambda: http_session.head(download_url, timeout=30, allow_redirects=True, headers=current_headers)
            )
            head_response.raise_for_status()
            content_length = head_response.headers.get('content-length')
            content_type = head_response.headers.get('content-type', '').lower()
            final_url_from_head = head_response.url

            print(f"  HEAD response: Status={head_response.status_code}, Type={content_type}, Length={content_length}") # Log

            if content_length and int(content_length) > DISCORD_FILE_LIMIT_BYTES:
                print(f"  File size ({int(content_length)} bytes) exceeds Discord limit ({DISCORD_FILE_LIMIT_BYTES} bytes).") # Log
                return f"File is too large ({int(content_length)/(1024*1024):.2f} MB). Max size: {DISCORD_FILE_LIMIT_MB} MB."

        except requests.exceptions.RequestException as head_err:
            print(f"  HEAD request failed: {head_err}. Proceeding with GET.") # Log

        # GET Request
        response = await loop.run_in_executor(
            None,
            lambda: http_session.get(download_url, stream=True, timeout=180, allow_redirects=True, headers=current_headers)
        )
        print(f"  Download GET status: {response.status_code}") # Log

        # Handle common server errors explicitly
        if response.status_code in [502, 503, 504, 404, 403]:
             print(f"  Download failed: Server returned status {response.status_code}.") # Log
             return f"Download failed (Server Status: {response.status_code}). Try the other mirror if available."

        response.raise_for_status() # Raise for other client/server errors (e.g., 4xx, 5xx)

        final_url = response.url
        content_type = response.headers.get('content-type', '').lower()
        print(f"  Final download URL: {final_url}, Content-Type: {content_type}")

        # Check for HTML content after GET, indicating an error page or CAPTCHA
        if 'text/html' in content_type:
            # Peek into the stream without consuming it all immediately
            peek_bytes = await loop.run_in_executor(None, lambda: response.raw.peek(2048)) # Read a larger chunk
            text_snippet = peek_bytes.decode('utf-8', errors='ignore')
            try:
                # Check for Sci-Hub CAPTCHA indicators
                if 'sci-hub.' in str(final_url) and ('captcha' in text_snippet.lower() or '<form' in text_snippet.lower() or 'please solve' in text_snippet.lower()):
                    print(">>> CAPTCHA BLOCK DETECTED <<<") # Log
                    response.close() # Close the connection
                    return f"Download failed: CAPTCHA required at {final_url}. Please visit the link manually."
                # Check for generic error messages
                elif 'error' in text_snippet.lower() or 'not found' in text_snippet.lower() or 'unavailable' in text_snippet.lower():
                     print(">>> Potential Error Page Detected <<<")
                     response.close()
                     return "Download failed: Link led to an HTML page (possibly an error or unavailable file)."
            except Exception as snippet_err:
                 print(f"Could not reliably check HTML snippet: {snippet_err}")
                 # Proceed cautiously, but log the HTML detection

            print(">>> DOWNLOAD FAILED: Received HTML instead of a file. <<<") # Log
            # Close connection if not already closed
            if not response.raw.closed:
                response.close()
            return "Download failed: Link led to an HTML page (unexpected content)."


        # Refine filename/extension based on headers and content type
        content_disposition = response.headers.get('content-disposition')
        valid_extensions = ['pdf', 'epub', 'mobi', 'azw', 'azw3', 'djvu', 'zip', 'rar', '7z', 'txt', 'chm', 'fb2']
        if content_disposition:
            # Improved regex to handle quotes and encoding artifacts
            match = re.findall('filename\*?=[\'"]?([^\'";]+)[\'"]?', content_disposition)
            if match:
                try:
                    # Handle potential URL encoding and UTF-8 encoding hints
                    raw_filename = match[0]
                    if raw_filename.lower().startswith("utf-8''"):
                         raw_filename = raw_filename[len("utf-8''"):]
                    header_filename = urllib.parse.unquote(raw_filename, errors='replace')

                    header_base, header_ext = os.path.splitext(header_filename)
                    header_ext = header_ext.lower().lstrip('.')
                    if header_ext in valid_extensions:
                        print(f"  Using filename from header: {header_filename}") # Log
                        filename_base = safe_filename(header_base)
                        final_extension = header_ext
                    elif final_extension == 'bin' or final_extension == 'n/a': # Only override if original was unknown
                         if header_ext: # Use header extension if it exists
                             final_extension = header_ext
                             print(f"  Using extension '{final_extension}' from header (override unknown).") # Log
                         else:
                             print(f"  Header filename '{header_filename}' has no extension, keeping '{final_extension}'.")
                except Exception as e:
                     print(f"Error parsing content-disposition '{content_disposition}': {e}")

        # Fallback extension detection if still unknown
        if final_extension == 'bin' or final_extension == 'n/a':
             type_map = {
                 'application/pdf': 'pdf', 'application/epub+zip': 'epub',
                 'application/x-mobipocket-ebook': 'mobi', # Common MIME for mobi/azw
                 'application/vnd.amazon.ebook': 'azw3', # More specific AZW3
                 'image/vnd.djvu': 'djvu', 'image/x-djvu': 'djvu',
                 'application/zip': 'zip', 'application/x-zip-compressed': 'zip',
                 'application/vnd.rar': 'rar', 'application/x-rar-compressed': 'rar',
                 'application/x-7z-compressed': '7z',
                 'text/plain': 'txt', 'application/vnd.ms-htmlhelp': 'chm',
                 'application/x-fictionbook+xml': 'fb2',
                 # Add more MIME types as needed
             }
             found_ext = False
             for mime, ext in type_map.items():
                 if mime in content_type:
                     final_extension = ext
                     print(f"  Deduced extension '{ext}' from Content-Type '{content_type}'")
                     found_ext = True
                     break
             if not found_ext:
                print(f"  Warning: Could not determine extension from Content-Type '{content_type}', using '.bin'") # Log

        filename = f"{filename_base}.{final_extension}"
        print(f"  Final filename: {filename}") # Log

        # Download into memory
        file_buffer = io.BytesIO()
        downloaded_size = 0
        limit_exceeded = False
        start_time = time.time()
        try:
            # Read chunks from the stream
            for chunk in response.iter_content(chunk_size=8192):
                if chunk: # filter out keep-alive new chunks
                    downloaded_size += len(chunk)
                    if downloaded_size > DISCORD_FILE_LIMIT_BYTES:
                        limit_exceeded = True
                        print(f"  Download aborted: File size exceeds {DISCORD_FILE_LIMIT_MB} MB limit after {time.time() - start_time:.2f}s.") # Log
                        break
                    file_buffer.write(chunk)
        except Exception as download_err:
             print(f"Error reading download stream: {download_err}")
             # Close response and buffer before returning error
             response.close()
             file_buffer.close()
             return f"Error occurred during download stream: {download_err}"

        # MUST close the response here to release connection, especially if limit exceeded or finished early
        if not response.raw.closed:
            response.close()

        if limit_exceeded:
            file_buffer.close() # Close the buffer too
            return f"File is too large ({downloaded_size/(1024*1024):.2f} MB+). Max size: {DISCORD_FILE_LIMIT_MB} MB."

        # If download completed within limits
        file_buffer.seek(0) # Reset buffer position to the beginning
        elapsed_time = time.time() - start_time
        print(f"  Downloaded {downloaded_size} bytes into memory in {elapsed_time:.2f}s.") # Log
        discord_file = discord.File(fp=file_buffer, filename=filename)
        # discord.File handles closing the fp (BytesIO buffer)
        return discord_file

    except requests.exceptions.Timeout:
        print(f"\nError: Download timed out for {download_url}")
        return "Download timed out. The server might be slow or the file is very large."
    except requests.exceptions.RequestException as e:
        print(f"\nError during download setup or connection: {e}") # Log
        if hasattr(e, 'response') and e.response is not None:
            print(f"  Status Code: {e.response.status_code}") # Log status if available
        return f"An error occurred connecting or downloading: {e}"
    except Exception as e:
        print(f"\nUnexpected error during download processing: {e}") # Log
        import traceback; traceback.print_exc() # Log full traceback
        return f"An unexpected error occurred during download: {e}"
    finally:
        # Ensure response is closed if it exists and wasn't closed before
        if 'response' in locals() and response and hasattr(response, 'close') and not response.raw.closed:
            print("Closing response in finally block.")
            response.close()


# --- Discord Bot Setup ---
intents = discord.Intents.default()
# intents.message_content = True # Not needed for slash commands

class BookFinderBot(discord.Client):
    def __init__(self, *, intents: discord.Intents):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Sync commands globally. Use guild=discord.Object(id=...) for testing.
        try:
             synced = await self.tree.sync()
             print(f'Synced {len(synced)} commands globally.')
        except Exception as e:
             print(f"Failed to sync commands: {e}")
        print(f'Setup hook complete for {self.user}.')

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

# Instantiate the client globally
client = BookFinderBot(intents=intents)


# --- Discord UI Components (Updated View Logic) ---

class SearchResultSelectView(View):
    def __init__(self, search_results, original_interaction: discord.Interaction):
        super().__init__(timeout=300) # 5 minute timeout for selection
        self.search_results = search_results
        self.original_interaction = original_interaction
        self.status_message: discord.WebhookMessage | None = None # To store the followup message object
        options_limited = search_results[:25] # Discord limit is 25 options

        select_options = []
        for i, item in enumerate(options_limited):
            # --- Stricter Truncation for SelectOption ---
            label_text = f"{i+1}. {item.get('title', 'Unknown Title')}" # Use .get with fallback
            # Max 100 chars for label
            final_label = label_text[:98] + ".." if len(label_text) > 100 else label_text

            # Truncate description parts *before* combining
            authors_part = item.get('authors', 'N/A')[:30]
            year_part = item.get('year', 'N/A')
            ext_part = item.get('extension', 'N/A')[:10]
            size_part = item.get('size', 'N/A')[:15]

            desc_text = f"{authors_part} ({year_part}) | {ext_part} | {size_part}"
            # Max 100 chars for description
            final_desc = desc_text[:98] + ".." if len(desc_text) > 100 else desc_text
            # --- End Stricter Truncation ---

            select_options.append(
                discord.SelectOption(
                    label=final_label,
                    description=final_desc,
                    value=str(i) # Index in the original (potentially larger) list
                )
            )

        # Handle case with no valid options generated
        if not select_options:
             # Optionally disable the view or add a dummy option indicating no selectable items
             print("Warning: No valid options generated for Select menu.")
             # You might want to handle this case in the command itself before creating the view

        self.select_menu = Select(
            placeholder="Select a book/article to download (Up to 25 shown)...",
            options=select_options if select_options else [discord.SelectOption(label="No results", value="-1", description="No results found")] # Add dummy if empty
        )
        self.select_menu.callback = self.select_callback
        self.add_item(self.select_menu)

    async def select_callback(self, interaction: discord.Interaction):
        # Ensure only the original user can interact
        if interaction.user.id != self.original_interaction.user.id:
             await interaction.response.send_message("You didn't initiate this search.", ephemeral=True)
             return

        selected_value = self.select_menu.values[0]
        if selected_value == "-1": # Handle dummy option case
             await interaction.response.edit_message(content="No selectable item.", view=None)
             return

        selected_index = int(selected_value)
        if selected_index >= len(self.search_results):
            await interaction.response.send_message("Invalid selection index.", ephemeral=True)
            return

        selected_item = self.search_results[selected_index]

        # 1. Disable the select menu on the original message
        self.select_menu.disabled = True
        self.select_menu.placeholder = f"Selected: {selected_item.get('title', 'Unknown')[:50]}..." # Show what was selected
        try:
            # Respond to the select interaction by editing the original message VIEW ONLY
            await interaction.response.edit_message(view=self)
        except discord.HTTPException as e:
            print(f"Error editing original message view: {e}")
            # If interaction is already responded to or expired, this might fail.

        # 2. Send a *NEW* followup message for the status
        thinking_message = f"⏳ Attempting to download '{selected_item.get('title', 'Unknown')}'..."
        try:
            # Use wait=True to get the message object back for later editing
            self.status_message = await interaction.followup.send(thinking_message[:2000], wait=True)
        except discord.HTTPException as e:
            print(f"Error sending followup 'thinking' message: {e}")
            # Inform user ephemerally if we can't send status
            # Check if original edit failed too, maybe send there?
            try:
                await interaction.followup.send("Failed to send status update message.", ephemeral=True)
            except discord.HTTPException: # If followup fails too, interaction is likely dead
                 print("Interaction likely dead, cannot send error followup.")
            return # Stop if we cannot show status

        # --- Attempt Download ---
        download_result = None
        error_message = "Download failed. Unknown reason."
        mirror1_url = selected_item.get('mirror1_url')

        if mirror1_url:
            print(f"Attempting Mirror 1: {mirror1_url}")
            direct_download_link = await get_libgen_download_link_async(mirror1_url)
            if direct_download_link:
                download_result = await download_book_to_discord(
                    direct_download_link,
                    selected_item.get('title', 'Unknown'),
                    selected_item.get('extension', 'bin')
                )
            else:
                error_message = "Could not find a direct download link from the first mirror page."
                print(error_message)
        else:
            error_message = "Mirror 1 URL was not found in the search results."
            print(error_message)
        # --- End Download Logic ---


        # --- Process Download Result ---
        # 3. Edit the followup status message with the final result
        if not self.status_message:
            print("Error: Status message object was not stored correctly.")
            # Attempt to send a new followup if status_message is missing
            try:
                 await interaction.followup.send("An internal error occurred tracking status.", ephemeral=True)
            except discord.HTTPException as e:
                 print(f"Failed to send internal error followup: {e}")
            return

        if isinstance(download_result, discord.File):
            success_message = f"✅ Download successful for **{selected_item.get('title', 'Unknown')}**!"
            try:
                 # Edit the status message (the followup)
                 await self.status_message.edit(
                     content=success_message[:2000],
                     attachments=[download_result]
                 )
            except discord.HTTPException as e:
                 print(f"Discord API error editing status message with file: {e}")
                 # Fallback: Try sending *another* followup as a last resort
                 try:
                     await interaction.followup.send(success_message[:2000], file=download_result)
                 except discord.HTTPException as e2:
                     print(f"Discord API error sending file (final followup): {e2}")
                     # Edit status message with error if possible
                     await self.status_message.edit(
                         content=f"⚠️ Could not send the file via Discord (Error: {e2.code}). It might be slightly too large or another issue occurred.",
                         attachments=[]
                     )
                     # Offer Mirror 2 link separately
                     mirror2_url = selected_item.get('mirror2_url')
                     if mirror2_url:
                          try:
                              await interaction.followup.send(
                                  f"Try Mirror 2: <{mirror2_url}>", ephemeral=True
                              )
                          except discord.HTTPException as follow_e:
                              print(f"Error sending mirror 2 followup: {follow_e}")

            except Exception as ex:
                 print(f"Unexpected error sending file: {ex}")
                 try:
                      await self.status_message.edit(content=f"An unexpected error occurred while sending the file.", attachments=[])
                 except discord.HTTPException as final_edit_err:
                     print(f"Failed to edit status message with final unexpected error: {final_edit_err}")

        else: # Download failed or file too large
            if isinstance(download_result, str):
                 error_message = download_result # Use the specific error

            print(f"Mirror 1 failed for '{selected_item.get('title', 'Unknown')}'. Reason: {error_message}")
            fallback_message = f"❌ Download via Mirror 1 failed: {error_message}\n"
            # (Mirror 2 logic remains the same)
            mirror2_url = selected_item.get('mirror2_url')
            if mirror2_url:
                fallback_message += (
                    f"\nYou can try **Mirror 2** manually:\n<{mirror2_url}>\n"
                    f"*(Click GET, close ad, click GET again)*"
                )
            elif selected_item.get('details_url') and selected_item.get('details_url') != mirror1_url:
                 fallback_message += f"\nVisit details page for more options: <{selected_item.get('details_url')}>"
            else:
                fallback_message += "\nNo alternative Mirror 2 URL found."

            try:
                # Edit the status message (the followup) with the error
                await self.status_message.edit(content=fallback_message[:2000], attachments=[])
            except discord.HTTPException as e:
                 print(f"Failed to edit status message with download failure info: {e}")
                 # Send ephemeral if edit fails
                 try:
                     await interaction.followup.send(fallback_message[:2000], ephemeral=True)
                 except discord.HTTPException as follow_e:
                     print(f"Failed to send ephemeral fallback message: {follow_e}")

    async def on_timeout(self):
         # Timeout logic primarily affects the original message with the view
         if self.original_interaction:
            try:
                # Disable components on the original message
                for item in self.children:
                    if hasattr(item, 'disabled'):
                         item.disabled = True
                # Update placeholder if it wasn't already changed by selection
                if isinstance(self.select_menu, Select) and self.select_menu.placeholder.startswith("Select a book"):
                    self.select_menu.placeholder = "Selection timed out."

                # Edit the original message's view
                await self.original_interaction.edit_original_response(view=self) # Edit only view, keep content/embed

                # Optionally, edit the status message too if it exists and hasn't completed
                if self.status_message:
                     # Check if message exists before trying to get content
                    try:
                        current_status_content = self.status_message.content # Get current content
                        if current_status_content.startswith("⏳"): # Check if it's still 'Attempting'
                             await self.status_message.edit(content="Download cancelled due to timeout.", attachments=[])
                    except discord.NotFound:
                         print("Status message not found on timeout, likely deleted.")
                    except discord.HTTPException as status_edit_err:
                         print(f"Error editing status message on timeout: {status_edit_err}")


            except (discord.NotFound, discord.HTTPException) as e:
                 print(f"Error updating original view on timeout (likely message deleted or interaction expired): {e}")
            except Exception as e:
                print(f"Unexpected error updating view/status on timeout: {e}")

# --- Slash Commands (Updated findbook) ---

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

    await interaction.response.send_message(embed=embed, ephemeral=True)


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

    # Defer publicly, indicates the bot is working
    await interaction.response.defer(thinking=True, ephemeral=False)

    if search_topic == 'magz':
        magz_url = f"http://magzdb.org/makelist?t={urllib.parse.quote_plus(query)}"
        await interaction.followup.send(
            f"Magazine searches are handled by MagzDB.\n"
            f"Please visit this link to see results for '{query}':\n<{magz_url}>"
        )
        return

    results = await search_libgen_async(query, search_topic=search_topic)

    if not results:
        await interaction.followup.send(f"No results found for '{query}' in '{search_topic_name}'. Try different keywords or categories.")
        return

    # Ensure description is within limits
    embed_desc = f"Found {len(results)} results. Select one below to attempt download (up to 25 shown)."
    embed = discord.Embed(
        # Ensure title is within limits
        title=f"Search Results for '{query}' ({search_topic_name})"[:256],
        description=embed_desc[:4096], # Max embed description length
        color=discord.Color.green()
    )

    results_to_preview = results[:5] # Show first 5 in embed preview
    for i, item in enumerate(results_to_preview):
         field_name_text = f"{i+1}. {item.get('title', 'Unknown Title')}"
         # Max 256 chars for field name
         final_field_name = field_name_text[:254] + ".." if len(field_name_text) > 256 else field_name_text

         authors_val = item.get('authors', 'N/A')[:200]
         ext_val = item.get('extension', 'N/A')
         size_val = item.get('size', 'N/A')
         lang_val = item.get('language', 'N/A')
         field_value_text = f"Author(s): {authors_val}\nFormat: {ext_val} | Size: {size_val} | Lang: {lang_val}"
         # Max 1024 chars for field value
         final_field_value = field_value_text[:1022] + ".." if len(field_value_text) > 1024 else field_value_text

         if final_field_name and final_field_value: # Avoid empty fields
            try:
                 embed.add_field(
                     name=final_field_name,
                     value=final_field_value,
                     inline=False
                 )
            except ValueError as e:
                 print(f"Error adding field {i}: {e}. Name='{final_field_name}', Value='{final_field_value}'")
                 # Optionally add a placeholder field indicating an error
                 embed.add_field(name=f"Error processing result {i+1}", value="Could not display details.", inline=False)
         else:
            print(f"Warning: Skipped adding embed field for item {i} due to empty name/value after truncation.")


    footer_text = f"Total results: {len(results)}."
    if len(results) > 25:
        footer_text += " Showing the first 25 in the dropdown."
    elif len(results) > len(results_to_preview) : # Check if more results exist than shown in preview
         footer_text += f" Showing {len(results_to_preview)} in preview. Use dropdown for all {len(results)}."
    # Keep footer simple if results <= preview count

    # Ensure footer is within limits
    embed.set_footer(text=footer_text[:2048])

    # Basic check for embed limits before sending
    if len(embed.fields) > 25:
         print("Error: Too many embed fields generated.")
         await interaction.followup.send("Error: Too many results generated to display properly.")
         return
    if len(embed) > 6000: # Check discord.py Embed.__len__ calculation
         print("Warning: Embed potentially too large, might be rejected by Discord.")
         # Consider reducing number of preview fields if this occurs often

    # Create the View with the Select dropdown (uses the updated class)
    view = SearchResultSelectView(results, interaction)

    # Send the initial response with embed and view
    try:
        await interaction.followup.send(embed=embed, view=view)
    except discord.HTTPException as e:
         print(f"Error sending initial search results followup: {e}")
         # Try sending just text if embed/view fails
         await interaction.followup.send("Error displaying search results. Please try again.")


# --- Run the Bot ---
if __name__ == "__main__":
    if BOT_TOKEN is None:
        print("ERROR: Discord Bot Token not found.")
        print("Please set the DISCORD_BOT_TOKEN environment variable.")
    else:
        # Start Flask in a background thread
        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()

        try:
            # Start the Discord bot (blocking call)
            print("Starting Discord bot...")
            # Use asyncio.run() if not already in an event loop context,
            # but client.run() typically handles the loop.
            client.run(BOT_TOKEN)
        except discord.LoginFailure:
            print("ERROR: Invalid Discord Bot Token. Please check your token.")
        except discord.PrivilegedIntentsRequired:
             print("ERROR: Privileged Intents (like Message Content) are required but not enabled.")
             print("Please enable required intents in your bot's settings on the Discord Developer Portal.")
        except Exception as e:
            print(f"An critical error occurred while running the bot: {e}")
            import traceback
            traceback.print_exc()