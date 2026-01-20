from urllib.parse import unquote
import requests
import argparse
import sys
from tqdm import tqdm
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import json
import signal
import tempfile
from datetime import datetime, timezone
from typing import Callable, Any
import time

def extract_drive_id(input_str: str) -> str:
    """Extracts the Google Drive file ID from a URL or returns the input if it's already an ID."""
    pattern = r'/file/d/([a-zA-Z0-9_-]+)'
    match = re.search(pattern, input_str)
    if match:
        return match.group(1)
    return input_str

def extract_folder_id(input_str: str) -> tuple[str, str | None]:
    """Extracts folder ID and optional resource key from a Google Drive folder URL.

    Returns:
        tuple of (folder_id, resource_key) where resource_key may be None
    """
    # Match /folders/FOLDER_ID pattern
    pattern = r'/folders/([a-zA-Z0-9_-]+)'
    match = re.search(pattern, input_str)
    if not match:
        return None, None

    folder_id = match.group(1)

    # Extract resource key if present (for link-shared folders)
    resource_key = None
    rk_pattern = r'resourcekey=([a-zA-Z0-9_-]+)'
    rk_match = re.search(rk_pattern, input_str)
    if rk_match:
        resource_key = rk_match.group(1)

    return folder_id, resource_key

def is_folder_url(input_str: str) -> bool:
    """Check if the input string is a Google Drive folder URL."""
    return '/folders/' in input_str


# --- Batch Download State Management ---

def load_state(path: str) -> dict:
    """Load JSON state file or return empty state structure."""
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "version": 1,
        "created": datetime.now(timezone.utc).isoformat(),
        "updated": datetime.now(timezone.utc).isoformat(),
        "videos": {}
    }


def save_state(path: str, state: dict) -> None:
    """Atomically save state (write to temp file, then rename)."""
    state["updated"] = datetime.now(timezone.utc).isoformat()

    # Write to temp file first, then rename for atomic operation
    dir_name = os.path.dirname(path) or '.'
    fd, temp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
        raise


def detect_rate_limit(response: requests.Response = None, exception: Exception = None) -> bool:
    """Check if an error indicates a rate limit."""
    if response is not None:
        if response.status_code == 429:
            return True
        # Google sometimes returns 403 with rate limit messages
        if response.status_code == 403:
            text = response.text.lower()
            if 'rate' in text or 'quota' in text or 'limit' in text:
                return True
    if exception is not None:
        exc_str = str(exception).lower()
        if 'rate' in exc_str or 'quota' in exc_str or 'limit' in exc_str:
            return True
    return False


def retry_with_backoff(
    func: Callable[[], Any],
    max_retries: int = 5,
    base_delay: float = 1.0,
    rate_limit_delay: float = 60.0,
    verbose: bool = False
) -> tuple[Any, str | None]:
    """
    Retry a function with exponential backoff.

    Returns:
        tuple of (result, error_type) where error_type is None on success,
        or one of: 'rate_limit', 'network', 'other'
    """
    last_exception = None
    last_error_type = None

    for attempt in range(max_retries + 1):
        try:
            result = func()
            return result, None
        except Exception as e:
            last_exception = e

            # Determine error type
            is_rate_limit = detect_rate_limit(exception=e)
            if is_rate_limit:
                last_error_type = 'rate_limit'
                delay = rate_limit_delay
            elif isinstance(e, (requests.exceptions.ConnectionError,
                               requests.exceptions.Timeout)):
                last_error_type = 'network'
                delay = base_delay * (2 ** attempt)
            else:
                last_error_type = 'other'
                delay = base_delay * (2 ** attempt)

            if attempt < max_retries:
                if verbose:
                    print(f"[RETRY] Attempt {attempt + 1}/{max_retries} failed: {e}")
                    print(f"[RETRY] Waiting {delay:.1f}s before retry...")
                time.sleep(delay)
            else:
                if verbose:
                    print(f"[RETRY] All {max_retries} retries exhausted: {e}")

    # All retries failed - raise the last exception
    raise last_exception


# Global flag for graceful interruption
_interrupted = False

def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM for graceful shutdown."""
    global _interrupted
    _interrupted = True
    print("\n[INTERRUPT] Caught signal, finishing current video and saving state...")


def list_folder_contents(folder_id: str, api_key: str, resource_key: str = None, verbose: bool = False) -> list[dict]:
    """Lists all files and folders in a Google Drive folder using the Drive API.

    Args:
        folder_id: The Google Drive folder ID
        api_key: Google API key with Drive API enabled
        resource_key: Optional resource key for link-shared folders
        verbose: Enable verbose output

    Returns:
        List of dicts with keys: id, name, mimeType
    """
    base_url = "https://www.googleapis.com/drive/v3/files"
    items = []
    page_token = None

    headers = {}
    if resource_key:
        headers['X-Goog-Drive-Resource-Keys'] = f'{folder_id}/{resource_key}'

    while True:
        params = {
            'q': f"'{folder_id}' in parents",
            'fields': 'nextPageToken,files(id,name,mimeType)',
            'pageSize': 100,
            'key': api_key
        }
        if page_token:
            params['pageToken'] = page_token

        if verbose:
            print(f"[INFO] Listing contents of folder {folder_id}")

        response = requests.get(base_url, params=params, headers=headers)

        if response.status_code != 200:
            error_msg = response.json().get('error', {}).get('message', response.text)
            raise Exception(f"Drive API error: {error_msg}")

        data = response.json()
        items.extend(data.get('files', []))

        page_token = data.get('nextPageToken')
        if not page_token:
            break

    if verbose:
        print(f"[INFO] Found {len(items)} items in folder")

    return items

def collect_videos_recursive(folder_id: str, api_key: str, resource_key: str = None, verbose: bool = False) -> list[dict]:
    """Recursively collects all video files from a folder and its subfolders.

    Args:
        folder_id: The Google Drive folder ID
        api_key: Google API key with Drive API enabled
        resource_key: Optional resource key for link-shared folders
        verbose: Enable verbose output

    Returns:
        List of dicts with keys: id, name (for video files only)
    """
    videos = []
    items = list_folder_contents(folder_id, api_key, resource_key, verbose)

    for item in items:
        mime_type = item.get('mimeType', '')

        if mime_type.startswith('video/'):
            videos.append({'id': item['id'], 'name': item['name']})
            if verbose:
                print(f"[INFO] Found video: {item['name']}")

        elif mime_type == 'application/vnd.google-apps.folder':
            if verbose:
                print(f"[INFO] Entering subfolder: {item['name']}")
            # Recurse into subfolder (no resource key needed for subfolders)
            sub_videos = collect_videos_recursive(item['id'], api_key, None, verbose)
            videos.extend(sub_videos)

    return videos

def get_video_url(page_content: str, verbose: bool) -> tuple[str, str]:
    """Extracts the video playback URL and title from the page content."""
    if verbose:
        print("[INFO] Parsing video playback URL and title.")
    contentList = page_content.split("&")
    video, title = None, None
    for content in contentList:
        if content.startswith('title=') and not title:
            title = unquote(content.split('=')[-1])
        elif "videoplayback" in content and not video:
            video = unquote(content).split("|")[-1]
        if video and title:
            break

    if verbose:
        print(f"[INFO] Video URL: {video}")
        print(f"[INFO] Video Title: {title}")
    return video, title

def download_file(url: str, cookies: dict, filename: str, chunk_size: int, verbose: bool) -> None:
    """Downloads the file from the given URL with provided cookies, supports resuming."""
    headers = {}
    file_mode = 'wb'

    downloaded_size = 0
    if os.path.exists(filename):
        downloaded_size = os.path.getsize(filename)
        headers['Range'] = f"bytes={downloaded_size}-"
        file_mode = 'ab'

    if verbose:
        print(f"[INFO] Starting download from {url}")
        if downloaded_size > 0:
            print(f"[INFO] Resuming download from byte {downloaded_size}")

    response = requests.get(url, stream=True, cookies=cookies, headers=headers)
    if response.status_code in (200, 206):  # 200 for new downloads, 206 for partial content
        total_size = int(response.headers.get('content-length', 0)) + downloaded_size
        with open(filename, file_mode) as file:
            with tqdm(total=total_size, initial=downloaded_size, unit='B', unit_scale=True, desc=filename, file=sys.stdout) as pbar:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        file.write(chunk)
                        pbar.update(len(chunk))
        print(f"\n{filename} downloaded successfully.")
    else:
        print(f"Error downloading {filename}, status code: {response.status_code}")


def download_chunk(url: str, cookies: dict, start: int, end: int, chunk_id: int,
                   filename: str, chunk_size: int, pbar: tqdm, lock: threading.Lock) -> bool:
    """Downloads a specific byte range of the file."""
    headers = {'Range': f'bytes={start}-{end}'}
    try:
        response = requests.get(url, stream=True, cookies=cookies, headers=headers)
        if response.status_code not in (200, 206):
            return False

        with open(filename, 'r+b') as f:
            f.seek(start)
            for chunk in response.iter_content(chunk_size=chunk_size):
                if chunk:
                    f.write(chunk)
                    with lock:
                        pbar.update(len(chunk))
        return True
    except Exception as e:
        return False


def download_file_parallel(url: str, cookies: dict, filename: str, chunk_size: int,
                           verbose: bool, num_connections: int) -> bool:
    """Downloads the file using multiple parallel connections."""
    # First, get the file size with a HEAD request
    try:
        head_response = requests.head(url, cookies=cookies, allow_redirects=True)
        if head_response.status_code != 200:
            # Fallback: try GET with stream to get content-length
            head_response = requests.get(url, cookies=cookies, stream=True)
            head_response.close()

        total_size = int(head_response.headers.get('content-length', 0))

        # Check if server supports range requests
        accept_ranges = head_response.headers.get('accept-ranges', 'none')
        if accept_ranges == 'none' and 'content-range' not in head_response.headers:
            if verbose:
                print("[INFO] Server may not support range requests, trying anyway...")
    except Exception as e:
        if verbose:
            print(f"[INFO] Could not get file size: {e}")
        return False

    if total_size == 0:
        if verbose:
            print("[INFO] Could not determine file size, falling back to single connection")
        return False

    if verbose:
        print(f"[INFO] Total file size: {total_size / (1024*1024):.2f} MB")
        print(f"[INFO] Using {num_connections} parallel connections")

    # Calculate chunk ranges for each connection
    chunk_size_per_conn = total_size // num_connections
    ranges = []
    for i in range(num_connections):
        start = i * chunk_size_per_conn
        end = start + chunk_size_per_conn - 1 if i < num_connections - 1 else total_size - 1
        ranges.append((start, end, i))

    # Pre-allocate the file
    with open(filename, 'wb') as f:
        f.truncate(total_size)

    # Create progress bar and lock
    pbar = tqdm(total=total_size, unit='B', unit_scale=True, desc=filename, file=sys.stdout)
    lock = threading.Lock()

    # Download chunks in parallel
    success = True
    with ThreadPoolExecutor(max_workers=num_connections) as executor:
        futures = {
            executor.submit(download_chunk, url, cookies, start, end, chunk_id,
                          filename, chunk_size, pbar, lock): chunk_id
            for start, end, chunk_id in ranges
        }

        for future in as_completed(futures):
            chunk_id = futures[future]
            try:
                if not future.result():
                    print(f"\nChunk {chunk_id} failed to download")
                    success = False
            except Exception as e:
                print(f"\nChunk {chunk_id} error: {e}")
                success = False

    pbar.close()

    if success:
        print(f"\n{filename} downloaded successfully.")
    else:
        print(f"\n{filename} download had errors.")

    return success

def get_unique_filename(output_dir: str, filename: str) -> str:
    """Returns a unique filename by appending a counter if the file already exists."""
    filepath = os.path.join(output_dir, filename)
    if not os.path.exists(filepath):
        return filepath

    name, ext = os.path.splitext(filename)
    counter = 1
    while True:
        new_filename = f"{name}_{counter}{ext}"
        new_filepath = os.path.join(output_dir, new_filename)
        if not os.path.exists(new_filepath):
            return new_filepath
        counter += 1

def download_single_video(video_id: str, output_file: str, chunk_size: int, verbose: bool,
                          num_connections: int = 1) -> dict:
    """Downloads a single video by its ID.

    Returns:
        dict with keys:
            - success: bool
            - filename: str or None (actual filename used)
            - error_type: str or None ('rate_limit', 'network', 'no_video_url', 'other')
            - error_message: str or None
    """
    result = {'success': False, 'filename': None, 'error_type': None, 'error_message': None}

    if verbose:
        print(f"[INFO] Downloading video ID: {video_id}")

    drive_url = f'https://drive.google.com/u/0/get_video_info?docid={video_id}&drive_originator_app=303'

    if verbose:
        print(f"[INFO] Accessing {drive_url}")

    try:
        response = requests.get(drive_url)

        # Check for rate limiting
        if detect_rate_limit(response):
            result['error_type'] = 'rate_limit'
            result['error_message'] = f"Rate limited (HTTP {response.status_code})"
            print(f"[ERROR] Rate limited when fetching video info for {video_id}")
            raise requests.exceptions.HTTPError(result['error_message'])

        page_content = response.text
        cookies = response.cookies.get_dict()

        video, title = get_video_url(page_content, verbose)

        filename = output_file if output_file else title
        result['filename'] = filename

        if video:
            if num_connections > 1:
                success = download_file_parallel(video, cookies, filename, chunk_size, verbose, num_connections)
                if not success:
                    if verbose:
                        print("[INFO] Parallel download failed, falling back to single connection")
                    download_file(video, cookies, filename, chunk_size, verbose)
            else:
                download_file(video, cookies, filename, chunk_size, verbose)
            result['success'] = True
            return result
        else:
            result['error_type'] = 'no_video_url'
            result['error_message'] = "Unable to retrieve video URL"
            print(f"Unable to retrieve the video URL for {video_id}.")
            return result

    except requests.exceptions.ConnectionError as e:
        result['error_type'] = 'network'
        result['error_message'] = str(e)
        raise
    except requests.exceptions.Timeout as e:
        result['error_type'] = 'network'
        result['error_message'] = str(e)
        raise
    except requests.exceptions.HTTPError:
        # Already handled above (rate limit)
        raise
    except Exception as e:
        if result['error_type'] is None:
            result['error_type'] = 'other'
            result['error_message'] = str(e)
        raise

def download_folder(folder_url: str, api_key: str, output_dir: str, chunk_size: int,
                    verbose: bool, num_connections: int = 1) -> None:
    """Downloads all videos from a Google Drive folder recursively.

    Args:
        folder_url: The Google Drive folder URL
        api_key: Google API key with Drive API enabled
        output_dir: Directory to save downloaded videos
        chunk_size: Chunk size for downloads
        verbose: Enable verbose output
        num_connections: Number of parallel connections per file
    """
    folder_id, resource_key = extract_folder_id(folder_url)
    if not folder_id:
        print("Error: Could not extract folder ID from URL.")
        return

    if verbose:
        print(f"[INFO] Folder ID: {folder_id}")
        if resource_key:
            print(f"[INFO] Resource key: {resource_key}")

    # Create output directory if it doesn't exist
    os.makedirs(output_dir, exist_ok=True)

    print("Scanning folder for videos...")
    try:
        videos = collect_videos_recursive(folder_id, api_key, resource_key, verbose)
    except Exception as e:
        print(f"Error scanning folder: {e}")
        return

    if not videos:
        print("No videos found in the folder.")
        return

    print(f"Found {len(videos)} video(s) to download.\n")

    success_count = 0
    fail_count = 0

    for i, video in enumerate(videos, 1):
        print(f"[{i}/{len(videos)}] Downloading: {video['name']}")
        output_file = get_unique_filename(output_dir, video['name'])

        try:
            result = download_single_video(video['id'], output_file, chunk_size, verbose, num_connections)
            if result['success']:
                success_count += 1
            else:
                fail_count += 1
        except Exception:
            fail_count += 1
        print()

    print(f"\nDownload complete: {success_count} succeeded, {fail_count} failed.")


def download_from_url_list(
    url_file: str,
    output_dir: str,
    state_file: str | None = None,
    chunk_size: int = 1024,
    verbose: bool = False,
    num_connections: int = 1,
    max_retries: int = 5
) -> None:
    """Downloads videos from a URL list file with progress tracking and retry logic.

    Args:
        url_file: Path to text file with one Google Drive URL per line
        output_dir: Directory to save downloaded videos
        state_file: Path to progress state file (auto-generated if None)
        chunk_size: Chunk size for downloads
        verbose: Enable verbose output
        num_connections: Number of parallel connections per file
        max_retries: Maximum retry attempts per video
    """
    global _interrupted

    # Set up signal handlers for graceful interruption
    original_sigint = signal.signal(signal.SIGINT, _signal_handler)
    original_sigterm = signal.signal(signal.SIGTERM, _signal_handler)

    # Auto-generate state file path if not provided
    if state_file is None:
        base_name = os.path.splitext(url_file)[0]
        state_file = f"{base_name}.progress.json"

    # Load or create state
    state = load_state(state_file)

    # Read URLs from file
    if not os.path.exists(url_file):
        print(f"Error: URL list file not found: {url_file}")
        return

    with open(url_file, 'r', encoding='utf-8') as f:
        content = f.read()

    # Parse comma-separated URLs
    raw_urls = [u.strip() for u in content.split(',')]

    # Parse URLs and extract video IDs
    videos_to_process = []
    for idx, url in enumerate(raw_urls, 1):
        if not url or url.startswith('#'):  # Skip empty entries and comments
            continue

        video_id = extract_drive_id(url)
        if not video_id:
            print(f"[WARN] Entry {idx}: Could not extract video ID from: {url}")
            continue

        videos_to_process.append({
            'url': url,
            'video_id': video_id,
            'entry_num': idx
        })

        # Initialize state entry if not present
        if video_id not in state['videos']:
            state['videos'][video_id] = {
                'status': 'pending',
                'url': url,
                'filename': None,
                'error': None,
                'attempts': 0
            }

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Save initial state
    save_state(state_file, state)

    print(f"Loaded {len(videos_to_process)} video(s) from {url_file}")
    print(f"Progress file: {state_file}")

    # Count existing statuses
    pending = sum(1 for v in state['videos'].values() if v['status'] == 'pending')
    completed = sum(1 for v in state['videos'].values() if v['status'] == 'completed')
    failed = sum(1 for v in state['videos'].values() if v['status'] == 'failed')

    print(f"Status: {completed} completed, {pending} pending, {failed} failed\n")

    # Filter to only videos that need processing (pending or failed with retries left)
    videos_to_download = []
    for video in videos_to_process:
        video_state = state['videos'].get(video['video_id'], {})
        status = video_state.get('status', 'pending')

        if status == 'completed':
            continue
        if status == 'failed' and video_state.get('attempts', 0) >= max_retries:
            continue

        videos_to_download.append(video)

    if not videos_to_download:
        print("All videos already downloaded or permanently failed.")
        return

    print(f"Processing {len(videos_to_download)} video(s)...\n")

    success_count = 0
    fail_count = 0
    current_video_id = None

    try:
        for i, video in enumerate(videos_to_download, 1):
            if _interrupted:
                print("[INTERRUPT] Stopping before next video...")
                break

            video_id = video['video_id']
            current_video_id = video_id
            video_state = state['videos'][video_id]

            print(f"[{i}/{len(videos_to_download)}] Video ID: {video_id}")

            # Update state to in_progress
            video_state['status'] = 'in_progress'
            video_state['attempts'] = video_state.get('attempts', 0) + 1
            save_state(state_file, state)

            # Determine output filename
            output_file = None  # Let download_single_video use the video title

            def do_download():
                return download_single_video(
                    video_id, output_file, chunk_size, verbose, num_connections
                )

            try:
                # Try download with retries
                result, error_type = retry_with_backoff(
                    do_download,
                    max_retries=max_retries - video_state['attempts'],  # Remaining retries
                    verbose=verbose
                )

                if result['success']:
                    video_state['status'] = 'completed'
                    video_state['filename'] = result.get('filename')
                    video_state['error'] = None
                    success_count += 1
                    print(f"[OK] Downloaded: {result.get('filename')}")
                else:
                    video_state['status'] = 'failed'
                    video_state['error'] = result.get('error_type', 'unknown')
                    fail_count += 1
                    print(f"[FAIL] {result.get('error_message', 'Unknown error')}")

            except Exception as e:
                video_state['status'] = 'failed'
                video_state['error'] = str(e)
                fail_count += 1
                print(f"[FAIL] Error after retries: {e}")

            # Save state after each video
            current_video_id = None
            save_state(state_file, state)
            print()

    finally:
        # Restore signal handlers
        signal.signal(signal.SIGINT, original_sigint)
        signal.signal(signal.SIGTERM, original_sigterm)

        # If interrupted while processing a video, mark it as pending (not failed)
        if _interrupted and current_video_id and current_video_id in state['videos']:
            state['videos'][current_video_id]['status'] = 'pending'
            save_state(state_file, state)

        # Reset interrupted flag
        _interrupted = False

    # Final summary
    total_completed = sum(1 for v in state['videos'].values() if v['status'] == 'completed')
    total_failed = sum(1 for v in state['videos'].values() if v['status'] == 'failed')
    total_pending = sum(1 for v in state['videos'].values() if v['status'] == 'pending')

    print("=" * 50)
    print(f"Batch download summary:")
    print(f"  Completed: {total_completed}")
    print(f"  Failed: {total_failed}")
    print(f"  Pending: {total_pending}")
    print(f"\nProgress saved to: {state_file}")

    if total_pending > 0 or total_failed > 0:
        print(f"Re-run the same command to retry pending/failed videos.")


def main(urls: list[str], output: str = None, chunk_size: int = 1024, verbose: bool = False,
         api_key: str = None, num_connections: int = 1) -> None:
    """Main function to process video/folder IDs or URLs and download.

    Args:
        urls: List of video IDs, video URLs, or folder URLs
        output: Output filename (for single video) or directory (for multiple/folder)
        chunk_size: Chunk size for downloads
        verbose: Enable verbose output
        api_key: Google API key (required for folder downloads)
        num_connections: Number of parallel connections per file
    """
    # Determine if we have multiple URLs or any folder URLs
    has_folder = any(is_folder_url(url) for url in urls)
    multiple_videos = len(urls) > 1

    # If multiple URLs or folder, output is treated as a directory
    if multiple_videos or has_folder:
        output_dir = output if output else './downloads'
        os.makedirs(output_dir, exist_ok=True)

    success_count = 0
    fail_count = 0

    for i, url in enumerate(urls, 1):
        if len(urls) > 1:
            print(f"\n[{i}/{len(urls)}] Processing: {url}")

        if is_folder_url(url):
            if not api_key:
                # Try environment variable
                api_key = os.environ.get('GOOGLE_API_KEY')
                if not api_key:
                    print("Error: Folder downloads require a Google API key.")
                    print("Provide via --api-key flag or GOOGLE_API_KEY environment variable.")
                    print("\nTo get an API key:")
                    print("1. Go to https://console.cloud.google.com")
                    print("2. Create a project and enable Google Drive API")
                    print("3. Create an API key under Credentials")
                    fail_count += 1
                    continue

            download_folder(url, api_key, output_dir, chunk_size, verbose, num_connections)
        else:
            # Single video download
            video_id = extract_drive_id(url)

            if verbose:
                print(f"[INFO] Extracted video ID: {video_id}")

            if multiple_videos:
                # When downloading multiple videos, use output as directory
                try:
                    result = download_single_video(video_id, None, chunk_size, verbose, num_connections)
                    if result['success']:
                        success_count += 1
                    else:
                        print("Ensure the video ID is correct and accessible.")
                        fail_count += 1
                except Exception:
                    print("Ensure the video ID is correct and accessible.")
                    fail_count += 1
            else:
                # Single video: use output as filename
                try:
                    result = download_single_video(video_id, output, chunk_size, verbose, num_connections)
                    if not result['success']:
                        print("Ensure the video ID is correct and accessible.")
                except Exception:
                    print("Ensure the video ID is correct and accessible.")

    if multiple_videos and not has_folder:
        print(f"\nDownload complete: {success_count} succeeded, {fail_count} failed.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download videos from Google Drive, including view-only videos and entire folders.",
        epilog="Examples:\n"
               "  %(prog)s https://drive.google.com/file/d/VIDEO_ID/view\n"
               "  %(prog)s URL1 URL2 URL3  # Multiple videos (space-separated)\n"
               "  %(prog)s 'URL1,URL2,URL3'  # Multiple videos (comma-separated)\n"
               "  %(prog)s https://drive.google.com/drive/folders/FOLDER_ID --api-key YOUR_KEY\n"
               "  %(prog)s FOLDER_URL -o ./my_videos/ --api-key YOUR_KEY\n"
               "  %(prog)s --url-list urls.txt -o ./videos/  # Batch download with progress tracking",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "urls",
        type=str,
        nargs='*',
        help="Video ID(s), video URL(s), or folder URL(s) from Google Drive"
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        help="Output filename (single video) or directory (multiple videos/folder/url-list, default: ./downloads/)"
    )
    parser.add_argument(
        "-c", "--chunk_size",
        type=int,
        default=1024,
        help="Chunk size in bytes for downloading (default: 1024)"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose mode"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        help="Google API key (required for folder downloads, or set GOOGLE_API_KEY env var)"
    )
    parser.add_argument(
        "-p", "--parallel",
        type=int,
        default=1,
        help="Number of parallel connections per file (default: 1, try 4-8 for faster downloads)"
    )
    parser.add_argument(
        "--url-list",
        type=str,
        metavar="FILE",
        help="Path to file with comma-separated Google Drive URLs (enables batch mode with progress tracking)"
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Maximum retry attempts per video in batch mode (default: 5)"
    )
    parser.add_argument("--version", action="version", version="%(prog)s 3.0")

    args = parser.parse_args()

    # URL list mode (batch download with progress tracking)
    if args.url_list:
        output_dir = args.output if args.output else './downloads'
        download_from_url_list(
            url_file=args.url_list,
            output_dir=output_dir,
            chunk_size=args.chunk_size,
            verbose=args.verbose,
            num_connections=args.parallel,
            max_retries=args.max_retries
        )
    elif args.urls:
        # Expand comma-separated URLs into individual URLs
        urls = []
        for url in args.urls:
            urls.extend(u.strip() for u in url.split(',') if u.strip())
        main(urls, args.output, args.chunk_size, args.verbose, args.api_key, args.parallel)
    else:
        parser.print_help()
        sys.exit(1)
