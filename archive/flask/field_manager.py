import json
from sqlalchemy import text
from models import db, FieldConfig, ArticleLabel

class FieldManager:
    @staticmethod
    def add_field(name, display_name, field_type, default_value=None, options=None, required=False):
        """Add a new dynamic field to the system"""
        # Validate field name (must be valid SQL column name)
        if not name.replace('_', '').isalnum():
            raise ValueError("Field name must be alphanumeric with underscores only")
        
        # Check if field already exists
        existing = FieldConfig.query.filter_by(name=name).first()
        if existing:
            raise ValueError(f"Field '{name}' already exists")
        
        # Create field config
        field_config = FieldConfig(
            name=name,
            display_name=display_name,
            field_type=field_type,
            default_value=default_value,
            options=json.dumps(options) if options else None,
            required=required
        )
        
        db.session.add(field_config)
        
        # Add column to articles table
        FieldManager._add_column_to_articles_table(name, field_type)
        
        # Set default values for existing articles
        if default_value:
            FieldManager._set_default_values(name, default_value)
        
        db.session.commit()
        return field_config
    
    @staticmethod
    def remove_field(name):
        """Remove a dynamic field from the system"""
        field_config = FieldConfig.query.filter_by(name=name).first()
        if not field_config:
            raise ValueError(f"Field '{name}' not found")
        
        # Remove all labels for this field
        ArticleLabel.query.filter_by(field_name=name).delete()
        
        # Remove field config
        db.session.delete(field_config)
        
        # Remove column from articles table
        FieldManager._remove_column_from_articles_table(name)
        
        db.session.commit()
    
    @staticmethod
    def get_all_fields():
        """Get all configured fields"""
        return FieldConfig.query.order_by(FieldConfig.created_at).all()
    
    @staticmethod
    def get_field_by_name(name):
        """Get field config by name"""
        return FieldConfig.query.filter_by(name=name).first()
    
    @staticmethod
    def _add_column_to_articles_table(column_name, field_type):
        """Add a new column to the articles table"""
        sql_type = FieldManager._get_sql_type(field_type)
        sql = f"ALTER TABLE articles ADD COLUMN {column_name} {sql_type}"
        
        try:
            db.session.execute(text(sql))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise Exception(f"Failed to add column {column_name}: {e}")
    
    @staticmethod
    def _remove_column_from_articles_table(column_name):
        """Remove a column from the articles table"""
        sql = f"ALTER TABLE articles DROP COLUMN {column_name}"
        
        try:
            db.session.execute(text(sql))
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            raise Exception(f"Failed to remove column {column_name}: {e}")
    
    @staticmethod
    def _get_sql_type(field_type):
        """Convert field type to SQL type"""
        type_mapping = {
            'text': 'TEXT',
            'boolean': 'BOOLEAN',
            'select': 'TEXT',
            'number': 'NUMERIC',
            'date': 'DATE'
        }
        return type_mapping.get(field_type, 'TEXT')
    
    @staticmethod
    def _set_default_values(column_name, default_value):
        """Set default values for existing articles"""
        sql = f"UPDATE articles SET {column_name} = :default_value WHERE {column_name} IS NULL"
        
        try:
            db.session.execute(text(sql), {'default_value': default_value})
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"Warning: Failed to set default values for {column_name}: {e}")
    
    @staticmethod
    def get_field_types():
        """Get available field types"""
        return [
            {'value': 'text', 'label': 'Text'},
            {'value': 'boolean', 'label': 'Boolean'},
            {'value': 'select', 'label': 'Dropdown'},
            {'value': 'number', 'label': 'Number'},
            {'value': 'date', 'label': 'Date'}
        ]
