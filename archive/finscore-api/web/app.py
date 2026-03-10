#!/usr/bin/env python3
"""
Flask web interface for testing the FinScore API.
Allows loading articles from JSON files and submitting them to the FastAPI endpoint.
"""

import json
import os
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from flask import Flask, render_template, request, jsonify, session

app = Flask(__name__)
app.secret_key = os.urandom(24)  # For session management

# Configuration
API_SCORE_URL = os.getenv("API_SCORE_URL", "http://api:8001/score")
API_TYPE_URL = os.getenv("API_TYPE_URL", "http://api:8001/type")
# Backward compatibility
API_URL = os.getenv("API_URL", API_SCORE_URL)
DEFAULT_FOLDER = os.getenv("DEFAULT_FOLDER", "/project/output/train_data/json")
MAX_FILES_PER_PAGE = 100
MAX_FILES_WARNING = 1000
PROJECT_ROOT = "/project"

# Article type labels for display
ARTICLE_TYPES = {
    0: "Background / Informational",
    1: "Analyst Action",
    2: "Corporate Event / Announcement",
    3: "Financial / Earnings Report",
    4: "Market Activity / Sentiment Data",
    5: "Stock Movement Explanation (WIIM)",
    6: "Macro / Market Recap",
    7: "Technical / Chart Analysis"
}


def normalize_path(folder_path: str) -> str:
    """
    Normalize folder path to absolute path.
    If relative path is given, prepend /project/.
    """
    if not folder_path:
        return DEFAULT_FOLDER
    
    # If it's already an absolute path starting with /project, use as-is
    if folder_path.startswith(PROJECT_ROOT):
        return folder_path
    
    # If it's an absolute path but doesn't start with /project, use as-is
    if folder_path.startswith('/'):
        return folder_path
    
    # If it's a relative path, prepend /project/
    return str(Path(PROJECT_ROOT) / folder_path)


def scan_json_files(folder_path: str, offset: int = 0, limit: int = MAX_FILES_PER_PAGE) -> Dict[str, Any]:
    """
    Scan a folder for JSON files with pagination support.
    
    Returns:
        Dict with keys: files (list), total_count (int), has_more (bool)
    """
    try:
        # Normalize the path
        folder_path = normalize_path(folder_path)
        path = Path(folder_path)
        if not path.exists():
            return {"error": f"Folder not found: {folder_path}", "files": [], "total_count": 0, "has_more": False}
        
        if not path.is_dir():
            return {"error": f"Not a directory: {folder_path}", "files": [], "total_count": 0, "has_more": False}
        
        # Get all JSON files (sorted by name)
        all_files = sorted([f.name for f in path.glob("*.json")])
        total_count = len(all_files)
        
        # Paginate
        paginated_files = all_files[offset:offset + limit]
        has_more = (offset + limit) < total_count
        
        return {
            "files": paginated_files,
            "total_count": total_count,
            "has_more": has_more,
            "offset": offset,
            "limit": limit
        }
    except Exception as e:
        return {"error": str(e), "files": [], "total_count": 0, "has_more": False}


def load_article_json(folder_path: str, filename: str) -> Optional[Dict[str, Any]]:
    """Load and parse a JSON article file."""
    try:
        # Normalize the path
        folder_path = normalize_path(folder_path)
        file_path = Path(folder_path) / filename
        with open(file_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        return {"error": f"Failed to load article: {str(e)}"}


def submit_to_api(article: Dict[str, Any], endpoint_url: str = None) -> Dict[str, Any]:
    """Submit an article to the FastAPI endpoint."""
    if endpoint_url is None:
        endpoint_url = API_URL
    
    try:
        response = requests.post(endpoint_url, json=article, timeout=60)
        response.raise_for_status()
        result = {
            "success": True,
            "response": response.json(),
            "status_code": response.status_code
        }
        
        # Add type label if this is a type classification response
        if 'type' in result['response']:
            type_id = result['response']['type']
            result['response']['type_label'] = ARTICLE_TYPES.get(type_id, f"Unknown ({type_id})")
        
        return result
    except requests.exceptions.Timeout:
        return {
            "success": False,
            "error": "Request timed out (>60s). The model may be slow or overloaded.",
            "status_code": 504
        }
    except requests.exceptions.RequestException as e:
        return {
            "success": False,
            "error": str(e),
            "status_code": getattr(e.response, 'status_code', 500) if hasattr(e, 'response') else 500
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}",
            "status_code": 500
        }


@app.route('/')
def index():
    """Main page."""
    return render_template('index.html', default_folder=DEFAULT_FOLDER)


@app.route('/api/scan', methods=['POST'])
def scan_folder():
    """API endpoint to scan a folder for JSON files."""
    data = request.get_json()
    folder_path = data.get('folder_path', DEFAULT_FOLDER)
    offset = data.get('offset', 0)
    limit = data.get('limit', MAX_FILES_PER_PAGE)
    search = data.get('search', '').lower()
    
    result = scan_json_files(folder_path, offset, limit)
    
    # Apply search filter if provided
    if search and not result.get('error'):
        filtered_files = [f for f in result['files'] if search in f.lower()]
        result['files'] = filtered_files
        result['filtered'] = True
    
    # Add warning flag for large directories
    if result['total_count'] > MAX_FILES_WARNING:
        result['warning'] = f"Large directory detected ({result['total_count']:,} files). Loading in batches."
    
    # Store folder path in session for convenience
    session['folder_path'] = folder_path
    
    return jsonify(result)


@app.route('/api/load', methods=['POST'])
def load_article():
    """API endpoint to load a specific article."""
    data = request.get_json()
    folder_path = data.get('folder_path', session.get('folder_path', DEFAULT_FOLDER))
    filename = data.get('filename')
    
    if not filename:
        return jsonify({"error": "No filename provided"}), 400
    
    article = load_article_json(folder_path, filename)
    
    if article and 'error' not in article:
        return jsonify({"success": True, "article": article})
    else:
        return jsonify({"success": False, "error": article.get('error', 'Unknown error')}), 400


@app.route('/api/submit', methods=['POST'])
def submit_article():
    """API endpoint to submit an article to the scoring API."""
    data = request.get_json()
    article = data.get('article')
    
    if not article:
        return jsonify({"success": False, "error": "No article provided"}), 400
    
    result = submit_to_api(article, API_SCORE_URL)
    return jsonify(result)


@app.route('/api/submit-type', methods=['POST'])
def submit_article_type():
    """API endpoint to submit an article for type classification."""
    data = request.get_json()
    article = data.get('article')
    
    if not article:
        return jsonify({"success": False, "error": "No article provided"}), 400
    
    result = submit_to_api(article, API_TYPE_URL)
    return jsonify(result)


@app.route('/health')
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy"})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
