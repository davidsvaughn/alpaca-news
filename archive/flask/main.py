#!/usr/bin/env python3
"""
Main entry point for the Alpaca News Labeler Flask application
"""

import sys
import os

# Add the flask directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app

if __name__ == '__main__':
    app = create_app()
    print("Starting Alpaca News Labeler...")
    print("Access the application at: http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)

