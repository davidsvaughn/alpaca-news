import os
import json
from datetime import datetime
from models import db, Article
from config import Config

class ArticleLoader:
    def __init__(self, articles_dir=None):
        self.articles_dir = articles_dir or Config.ARTICLES_DIR
    
    def scan_and_load_articles(self):
        """Scan the articles directory and load any new articles into the database"""
        if not os.path.exists(self.articles_dir):
            print(f"Articles directory not found: {self.articles_dir}")
            return 0
        
        loaded_count = 0
        json_files = [f for f in os.listdir(self.articles_dir) if f.endswith('.json')]
        
        for filename in json_files:
            if self._should_load_file(filename):
                try:
                    article_data = self._load_json_file(filename)
                    if article_data:
                        self._save_article_to_db(article_data, filename)
                        loaded_count += 1
                        print(f"Loaded article: {filename}")
                except Exception as e:
                    print(f"Error loading {filename}: {e}")
        
        return loaded_count
    
    def _should_load_file(self, filename):
        """Check if file should be loaded (not already in database)"""
        try:
            # First check by filename
            existing_by_filename = Article.query.filter_by(filename=filename).first()
            if existing_by_filename:
                return False
            
            # Then check by article_id (in case same article has different filename)
            article_data = self._load_json_file(filename)
            if article_data and article_data.get('id'):
                existing_by_id = Article.query.filter_by(article_id=article_data.get('id')).first()
                if existing_by_id:
                    print(f"Skipping {filename}: article_id {article_data.get('id')} already exists")
                    return False
            
            return True
        except Exception as e:
            # If there's a database error, rollback and try again
            db.session.rollback()
            print(f"Error checking if file should be loaded {filename}: {e}")
            return False  # Don't try to load if we can't check
    
    def _load_json_file(self, filename):
        """Load and parse JSON file"""
        filepath = os.path.join(self.articles_dir, filename)
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data
        except Exception as e:
            print(f"Error reading {filename}: {e}")
            return None
    
    def _save_article_to_db(self, article_data, filename):
        """Save article data to database"""
        try:
            # Parse datetime strings
            created_at = None
            updated_at = None
            
            if article_data.get('created_at'):
                try:
                    created_at = datetime.fromisoformat(article_data['created_at'].replace('Z', '+00:00'))
                except:
                    pass
            
            if article_data.get('updated_at'):
                try:
                    updated_at = datetime.fromisoformat(article_data['updated_at'].replace('Z', '+00:00'))
                except:
                    pass
            
            # Convert lists to JSON strings
            symbols = json.dumps(article_data.get('symbols', []))
            images = json.dumps(article_data.get('images', []))
            
            article = Article(
                article_id=article_data.get('id'),
                headline=article_data.get('headline'),
                source=article_data.get('source'),
                url=article_data.get('url'),
                summary=article_data.get('summary'),
                created_at=created_at,
                updated_at=updated_at,
                symbols=symbols,
                author=article_data.get('author'),
                content=article_data.get('content'),
                images=images,
                filename=filename
            )
            
            db.session.add(article)
            db.session.commit()
        except Exception as e:
            # Rollback on error and continue
            db.session.rollback()
            print(f"Error saving article {filename}: {e}")
            # Don't re-raise the exception to continue processing other files
