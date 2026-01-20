from urllib.parse import unquote
import requests
import argparse
import sys
from tqdm import tqdm
import os
import re

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

def download_single_video(video_id: str, output_file: str, chunk_size: int, verbose: bool) -> bool:
    """Downloads a single video by its ID. Returns True on success, False on failure."""
    if verbose:
        print(f"[INFO] Downloading video ID: {video_id}")

    drive_url = f'https://drive.google.com/u/0/get_video_info?docid={video_id}&drive_originator_app=303'

    if verbose:
        print(f"[INFO] Accessing {drive_url}")

    response = requests.get(drive_url)
    page_content = response.text
    cookies = response.cookies.get_dict()

    video, title = get_video_url(page_content, verbose)

    filename = output_file if output_file else title
    if video:
        download_file(video, cookies, filename, chunk_size, verbose)
        return True
    else:
        print(f"Unable to retrieve the video URL for {video_id}.")
        return False

def download_folder(folder_url: str, api_key: str, output_dir: str, chunk_size: int, verbose: bool) -> None:
    """Downloads all videos from a Google Drive folder recursively.

    Args:
        folder_url: The Google Drive folder URL
        api_key: Google API key with Drive API enabled
        output_dir: Directory to save downloaded videos
        chunk_size: Chunk size for downloads
        verbose: Enable verbose output
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

        if download_single_video(video['id'], output_file, chunk_size, verbose):
            success_count += 1
        else:
            fail_count += 1
        print()

    print(f"\nDownload complete: {success_count} succeeded, {fail_count} failed.")

def main(urls: list[str], output: str = None, chunk_size: int = 1024, verbose: bool = False, api_key: str = None) -> None:
    """Main function to process video/folder IDs or URLs and download.

    Args:
        urls: List of video IDs, video URLs, or folder URLs
        output: Output filename (for single video) or directory (for multiple/folder)
        chunk_size: Chunk size for downloads
        verbose: Enable verbose output
        api_key: Google API key (required for folder downloads)
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

            download_folder(url, api_key, output_dir, chunk_size, verbose)
        else:
            # Single video download
            video_id = extract_drive_id(url)

            if verbose:
                print(f"[INFO] Extracted video ID: {video_id}")

            if multiple_videos:
                # When downloading multiple videos, use output as directory
                if download_single_video(video_id, None, chunk_size, verbose):
                    success_count += 1
                else:
                    print("Ensure the video ID is correct and accessible.")
                    fail_count += 1
            else:
                # Single video: use output as filename
                if not download_single_video(video_id, output, chunk_size, verbose):
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
               "  %(prog)s FOLDER_URL -o ./my_videos/ --api-key YOUR_KEY",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "urls",
        type=str,
        nargs='+',
        help="Video ID(s), video URL(s), or folder URL(s) from Google Drive"
    )
    parser.add_argument(
        "-o", "--output",
        type=str,
        help="Output filename (single video) or directory (multiple videos/folder, default: ./downloads/)"
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
    parser.add_argument("--version", action="version", version="%(prog)s 2.0")

    args = parser.parse_args()
    # Expand comma-separated URLs into individual URLs
    urls = []
    for url in args.urls:
        urls.extend(u.strip() for u in url.split(',') if u.strip())
    main(urls, args.output, args.chunk_size, args.verbose, args.api_key)
