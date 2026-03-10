import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'postgresql://localhost/alpaca_news'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    ARTICLES_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'output', 'alpaca')

