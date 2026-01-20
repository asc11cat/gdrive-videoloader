# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

GDrive VideoLoader is a Python CLI tool that downloads videos from Google Drive, including "view-only" videos that don't have a download button. It supports resumable downloads and progress tracking.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Download a single video
python gdrive_videoloader.py <video_id_or_url>
python gdrive_videoloader.py <video_id> -o output.mp4 -v

# Download all videos from a folder (requires API key)
python gdrive_videoloader.py <folder_url> --api-key YOUR_API_KEY
python gdrive_videoloader.py <folder_url> --api-key YOUR_API_KEY -o ./my_videos/

# Using environment variable for API key
export GOOGLE_API_KEY=your_api_key
python gdrive_videoloader.py <folder_url>

# No test suite exists yet
```

## Architecture

Single-file application (`gdrive_videoloader.py`) with these functions:

**Single video download:**
- `extract_drive_id()` - Parses Google Drive URLs to extract file IDs
- `get_video_url()` - Parses Google's video info API response to extract playback URL and title
- `download_file()` - Handles chunked downloads with resume support via Range headers
- `download_single_video()` - Downloads a single video by ID

**Folder download (requires Google API key):**
- `extract_folder_id()` - Parses folder URLs to extract folder ID and optional resource key
- `is_folder_url()` - Detects if input is a folder URL
- `list_folder_contents()` - Calls Drive API to list folder contents with pagination
- `collect_videos_recursive()` - Recursively finds all videos in folder and subfolders
- `download_folder()` - Downloads all videos to output directory

- `main()` - Orchestrates the flow, routing to single video or folder download

The tool works by requesting `drive.google.com/u/0/get_video_info` which returns video metadata including a direct playback URL, then downloads from that URL with the session cookies. For folder downloads, it uses the official Google Drive API v3 to enumerate folder contents.

## Known Security Issues

**Path Traversal Vulnerability**: The video title from Google Drive is used directly as the filename without sanitization. Always use the `-o` flag to specify a safe output filename when downloading from untrusted sources.

## Claude Code Behavior

Always use Context7 MCP when I need library/API documentation, code generation, setup or configuration steps without me having to explicitly ask.
