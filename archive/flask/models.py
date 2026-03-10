from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()

class Article(db.Model):
    __tablename__ = 'articles'
    
    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, unique=True, nullable=False)  # From JSON
    headline = db.Column(db.Text)
    source = db.Column(db.String(100))
    url = db.Column(db.Text)
    summary = db.Column(db.Text)
    created_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime)
    symbols = db.Column(db.Text)  # JSON string
    author = db.Column(db.String(200))
    content = db.Column(db.Text)
    images = db.Column(db.Text)  # JSON string
    filename = db.Column(db.String(200), unique=True)  # Track which file was loaded
    loaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Article {self.article_id}: {self.headline}>'

class FieldConfig(db.Model):
    __tablename__ = 'field_configs'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    field_type = db.Column(db.String(50), nullable=False)  # text, boolean, select, number, date
    default_value = db.Column(db.Text)
    options = db.Column(db.Text)  # JSON string for select options
    required = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<FieldConfig {self.name}: {self.field_type}>'

class ArticleLabel(db.Model):
    __tablename__ = 'article_labels'
    
    id = db.Column(db.Integer, primary_key=True)
    article_id = db.Column(db.Integer, db.ForeignKey('articles.id'), nullable=False)
    field_name = db.Column(db.String(100), nullable=False)
    value = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (db.UniqueConstraint('article_id', 'field_name', name='unique_article_field'),)
    
    def __repr__(self):
        return f'<ArticleLabel {self.article_id}.{self.field_name}: {self.value}>'

