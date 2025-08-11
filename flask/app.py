from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from models import db, Article, FieldConfig, ArticleLabel
from article_loader import ArticleLoader
from field_manager import FieldManager
from config import Config
import json

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    
    # Initialize database
    db.init_app(app)
    
    # Add Jinja2 filter for JSON parsing
    @app.template_filter('from_json')
    def from_json_filter(value):
        if value:
            try:
                return json.loads(value)
            except:
                return []
        return []
    
    # Create tables
    with app.app_context():
        db.create_all()
        # Note: Automatic article loading on startup is disabled
        # Use the "Refresh" button in the web interface to load articles manually
        print("Database tables created. Use 'Refresh' button to load articles.")
    
    @app.route('/')
    def index():
        """Main page - show articles with search/filter"""
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        
        # Get search parameters
        search = request.args.get('search', '')
        source = request.args.get('source', '')
        symbols = request.args.get('symbols', '')
        
        # Get sorting parameters
        sort_by = request.args.get('sort_by', 'created_at')
        sort_order = request.args.get('sort_order', 'desc')
        
        # Build query
        query = Article.query
        
        if search:
            query = query.filter(
                Article.headline.ilike(f'%{search}%') |
                Article.summary.ilike(f'%{search}%') |
                Article.content.ilike(f'%{search}%')
            )
        
        if source:
            query = query.filter(Article.source == source)
        
        if symbols:
            # Filter by symbols (JSON array contains the symbol)
            query = query.filter(Article.symbols.ilike(f'%{symbols}%'))
        
        # Get unique sources for filter dropdown
        sources = db.session.query(Article.source).distinct().all()
        sources = [s[0] for s in sources if s[0]]
        
        # Get dynamic fields for search
        fields = FieldManager.get_all_fields()
        
        # Apply dynamic field filters
        for field in fields:
            field_value = request.args.get(f'field_{field.name}', '')
            if field_value:
                # For dynamic fields, always use ArticleLabel table since that's where the values are stored
                if field.field_type == 'boolean':
                    query = query.join(ArticleLabel).filter(
                        ArticleLabel.field_name == field.name,
                        ArticleLabel.value == field_value
                    )
                elif field.field_type == 'number':
                    try:
                        numeric_value = float(field_value)
                        query = query.join(ArticleLabel).filter(
                            ArticleLabel.field_name == field.name,
                            ArticleLabel.value == field_value
                        )
                    except ValueError:
                        # Skip invalid numeric values
                        pass
                elif field.field_type == 'date':
                    query = query.join(ArticleLabel).filter(
                        ArticleLabel.field_name == field.name,
                        ArticleLabel.value.ilike(f'%{field_value}%')
                    )
                else:  # text, select
                    query = query.join(ArticleLabel).filter(
                        ArticleLabel.field_name == field.name,
                        ArticleLabel.value.ilike(f'%{field_value}%')
                    )
        
        # Apply sorting
        if sort_by == 'headline':
            if sort_order == 'asc':
                query = query.order_by(Article.headline.asc())
            else:
                query = query.order_by(Article.headline.desc())
        elif sort_by == 'created_at':
            if sort_order == 'asc':
                query = query.order_by(Article.created_at.asc())
            else:
                query = query.order_by(Article.created_at.desc())
        elif sort_by == 'source':
            if sort_order == 'asc':
                query = query.order_by(Article.source.asc())
            else:
                query = query.order_by(Article.source.desc())
        elif sort_by == 'symbols':
            # Sort by symbols - use raw SQL to sort symbols alphabetically
            if sort_order == 'asc':
                query = query.order_by(db.text("""
                    CASE 
                        WHEN symbols IS NULL OR symbols = '[]' THEN ''
                        ELSE (
                            SELECT string_agg(symbol, ',' ORDER BY symbol)
                            FROM json_array_elements_text(symbols::json) AS symbol
                        )
                    END
                """))
            else:
                query = query.order_by(db.text("""
                    CASE 
                        WHEN symbols IS NULL OR symbols = '[]' THEN ''
                        ELSE (
                            SELECT string_agg(symbol, ',' ORDER BY symbol)
                            FROM json_array_elements_text(symbols::json) AS symbol
                        )
                    END DESC
                """))
        else:
            # Default sorting by created_at descending
            query = query.order_by(Article.created_at.desc())
        
        # Paginate
        articles = query.paginate(page=page, per_page=per_page, error_out=False)
        
        # Get dynamic field values for each article
        article_field_values = {}
        if fields:
            for article in articles.items:
                article_field_values[article.id] = {}
                for field in fields:
                    # Always get values from ArticleLabel table since that's where they're stored
                    label = ArticleLabel.query.filter_by(
                        article_id=article.id, 
                        field_name=field.name
                    ).first()
                    article_field_values[article.id][field.name] = label.value if label else ''
        
        return render_template('index.html', 
                             articles=articles, 
                             sources=sources,
                             fields=fields,
                             article_field_values=article_field_values,
                             search=search,
                             selected_source=source,
                             per_page=per_page,
                             sort_by=sort_by,
                             sort_order=sort_order)
    
    @app.route('/article/<int:article_id>')
    def view_article(article_id):
        """View and label a specific article"""
        article = Article.query.get_or_404(article_id)
        fields = FieldManager.get_all_fields()
        
        # Get existing labels
        labels = {}
        for field in fields:
            label = ArticleLabel.query.filter_by(
                article_id=article.id, 
                field_name=field.name
            ).first()
            labels[field.name] = label.value if label else ''
        
        # Get navigation with same filters as main page
        # Build the same query as the index page to get filtered results
        query = Article.query
        
        # Apply search filter
        search = request.args.get('search', '')
        if search:
            query = query.filter(
                Article.headline.ilike(f'%{search}%') |
                Article.summary.ilike(f'%{search}%') |
                Article.content.ilike(f'%{search}%')
            )
        
        # Apply source filter
        source = request.args.get('source', '')
        if source:
            query = query.filter(Article.source == source)
        
        # Apply symbols filter
        symbols = request.args.get('symbols', '')
        if symbols:
            query = query.filter(Article.symbols.ilike(f'%{symbols}%'))
        
        # Get sorting parameters
        sort_by = request.args.get('sort_by', 'created_at')
        sort_order = request.args.get('sort_order', 'desc')
        
        # Apply dynamic field filters
        for field in fields:
            field_value = request.args.get(f'field_{field.name}', '')
            if field_value:
                if field.field_type == 'boolean':
                    query = query.join(ArticleLabel).filter(
                        ArticleLabel.field_name == field.name,
                        ArticleLabel.value == field_value
                    )
                elif field.field_type == 'number':
                    try:
                        numeric_value = float(field_value)
                        query = query.join(ArticleLabel).filter(
                            ArticleLabel.field_name == field.name,
                            ArticleLabel.value == field_value
                        )
                    except ValueError:
                        pass
                elif field.field_type == 'date':
                    query = query.join(ArticleLabel).filter(
                        ArticleLabel.field_name == field.name,
                        ArticleLabel.value.ilike(f'%{field_value}%')
                    )
                else:  # text, select
                    query = query.join(ArticleLabel).filter(
                        ArticleLabel.field_name == field.name,
                        ArticleLabel.value.ilike(f'%{field_value}%')
                    )
        
        # Apply sorting (same as index page)
        if sort_by == 'headline':
            if sort_order == 'asc':
                query = query.order_by(Article.headline.asc())
            else:
                query = query.order_by(Article.headline.desc())
        elif sort_by == 'created_at':
            if sort_order == 'asc':
                query = query.order_by(Article.created_at.asc())
            else:
                query = query.order_by(Article.created_at.desc())
        elif sort_by == 'source':
            if sort_order == 'asc':
                query = query.order_by(Article.source.asc())
            else:
                query = query.order_by(Article.source.desc())
        elif sort_by == 'symbols':
            # Sort by symbols - use raw SQL to sort symbols alphabetically
            symbols_sort_expr = db.text("""
                CASE 
                    WHEN symbols IS NULL OR symbols = '[]' THEN ''
                    ELSE (
                        SELECT string_agg(symbol, ',' ORDER BY symbol)
                        FROM json_array_elements_text(symbols::json) AS symbol
                    )
                END
            """)
            if sort_order == 'asc':
                query = query.order_by(symbols_sort_expr.asc())
            else:
                query = query.order_by(symbols_sort_expr.desc())
        else:
            # Default sorting by created_at descending
            query = query.order_by(Article.created_at.desc())
        
        # Get all filtered articles
        filtered_articles = query.all()
        
        # Find current article position in filtered results
        current_index = None
        for i, art in enumerate(filtered_articles):
            if art.id == article_id:
                current_index = i
                break
        
        # Get previous and next articles from filtered results
        prev_article = None
        next_article = None
        
        if current_index is not None:
            if current_index > 0:
                prev_article = filtered_articles[current_index - 1]
            if current_index < len(filtered_articles) - 1:
                next_article = filtered_articles[current_index + 1]
        
        return render_template('article.html', 
                             article=article, 
                             fields=fields, 
                             labels=labels,
                             prev_article=prev_article,
                             next_article=next_article,
                             search=search,
                             selected_source=source,
                             symbols=symbols,
                             sort_by=sort_by,
                             sort_order=sort_order)
    
    @app.route('/article/<int:article_id>/save', methods=['POST'])
    def save_article_labels(article_id):
        """Save labels for an article"""
        article = Article.query.get_or_404(article_id)
        fields = FieldManager.get_all_fields()
        
        for field in fields:
            value = request.form.get(f'field_{field.name}', '')
            
            # Find existing label or create new one
            label = ArticleLabel.query.filter_by(
                article_id=article.id, 
                field_name=field.name
            ).first()
            
            if label:
                label.value = value
            else:
                label = ArticleLabel(
                    article_id=article.id,
                    field_name=field.name,
                    value=value
                )
                db.session.add(label)
        
        db.session.commit()
        flash('Labels saved successfully!', 'success')
        
        # Redirect to next article or back to current
        next_article_id = request.form.get('next_article_id')
        if next_article_id:
            return redirect(url_for('view_article', article_id=next_article_id))
        else:
            return redirect(url_for('view_article', article_id=article_id))
    
    @app.route('/refresh')
    def refresh_articles():
        """Manually refresh articles from JSON files"""
        loader = ArticleLoader()
        loaded_count = loader.scan_and_load_articles()
        flash(f'Loaded {loaded_count} new articles', 'success')
        return redirect(url_for('index'))
    
    @app.route('/config')
    def config_page():
        """Field configuration page"""
        fields = FieldManager.get_all_fields()
        field_types = FieldManager.get_field_types()
        return render_template('config.html', fields=fields, field_types=field_types)
    
    @app.route('/config/add_field', methods=['POST'])
    def add_field():
        """Add a new dynamic field"""
        try:
            name = request.form['name']
            display_name = request.form['display_name']
            field_type = request.form['field_type']
            default_value = request.form.get('default_value', '')
            required = 'required' in request.form
            
            options = None
            if field_type == 'select':
                options_text = request.form.get('options', '')
                options = [opt.strip() for opt in options_text.split('\n') if opt.strip()]
            
            FieldManager.add_field(
                name=name,
                display_name=display_name,
                field_type=field_type,
                default_value=default_value if default_value else None,
                options=options,
                required=required
            )
            
            flash(f'Field "{display_name}" added successfully!', 'success')
        except Exception as e:
            flash(f'Error adding field: {str(e)}', 'error')
        
        return redirect(url_for('config_page'))
    
    @app.route('/config/remove_field/<name>', methods=['POST'])
    def remove_field(name):
        """Remove a dynamic field"""
        try:
            FieldManager.remove_field(name)
            flash(f'Field "{name}" removed successfully!', 'success')
        except Exception as e:
            flash(f'Error removing field: {str(e)}', 'error')
        
        return redirect(url_for('config_page'))
    
    @app.route('/api/articles')
    def api_articles():
        """API endpoint for articles (for AJAX)"""
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 20, type=int)
        
        articles = Article.query.order_by(Article.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        
        return jsonify({
            'articles': [{
                'id': a.id,
                'headline': a.headline,
                'source': a.source,
                'created_at': a.created_at.isoformat() if a.created_at else None
            } for a in articles.items],
            'total': articles.total,
            'pages': articles.pages,
            'current_page': articles.page
        })
    
    return app

if __name__ == '__main__':
    app = create_app()
    app.run(debug=True, host='0.0.0.0', port=5000)
