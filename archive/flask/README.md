# Alpaca News Labeler

A Flask web application for viewing and labeling financial news articles from Alpaca's news feed.

## Features

- **Article Loading**: Automatically loads JSON articles from `output/alpaca/` directory
- **Dynamic Field Management**: Add/remove custom labeling fields without code changes
- **Article Labeling**: Label articles with custom fields (text, boolean, dropdown, number, date)
- **Search & Filter**: Search articles and filter by source and custom fields
- **Navigation**: Browse articles with previous/next navigation
- **Refresh**: Manual refresh button to load new articles

## Requirements

- Python 3.7+
- PostgreSQL
- pip

## Installation

1. **Install PostgreSQL** (if not already installed):
   ```bash
   # Ubuntu/Debian
   sudo apt-get install postgresql postgresql-contrib
   sudo systemctl start postgresql
   
   # macOS
   brew install postgresql
   brew services start postgresql
   ```

2. **Run the setup script**:
   ```bash
   cd flask
   python setup.py
   ```

   This will:
   - Check PostgreSQL connection
   - Create the `alpaca_news` database
   - Install Python dependencies
   - Create a `.env` file with configuration

3. **Run the application**:
   ```bash
   python main.py
   ```

4. **Access the application**:
   Open your browser to `http://localhost:5000`

## Usage

### First Time Setup

1. **Configure Fields**: Go to the "Config" page to add your first labeling fields
2. **Load Articles**: Click "Refresh" to load articles from the JSON files
3. **Start Labeling**: Click on any article to begin labeling

### Adding Custom Fields

1. Go to the "Config" page
2. Fill out the "Add New Field" form:
   - **Field Name**: Internal name (alphanumeric with underscores)
   - **Display Name**: User-friendly name shown in the interface
   - **Field Type**: Choose from Text, Yes/No, Dropdown, Number, or Date
   - **Default Value**: Optional default value for the field
   - **Options**: For dropdown fields, list options (one per line)
   - **Required**: Check if the field should be required

### Labeling Articles

1. Browse articles on the main page
2. Click "Label" on any article
3. Fill out the labeling form with your custom fields
4. Click "Save Labels" or "Save & Next Article"

### Searching and Filtering

- Use the search box to search headlines, summaries, and content
- Filter by news source using the dropdown
- Use custom field filters to find articles with specific labels

## File Structure

```
flask/
├── main.py              # Application entry point
├── app.py               # Flask application and routes
├── models.py            # Database models
├── config.py            # Configuration settings
├── article_loader.py    # Article loading from JSON files
├── field_manager.py     # Dynamic field management
├── setup.py             # Setup script
├── requirements.txt     # Python dependencies
├── .env                 # Environment variables (created by setup)
├── templates/           # HTML templates
│   ├── base.html        # Base template
│   ├── index.html       # Main articles page
│   ├── article.html     # Article viewing/labeling page
│   └── config.html      # Field configuration page
└── README.md           # This file
```

## Database Schema

### Articles Table
- `id`: Primary key
- `article_id`: Original article ID from JSON
- `headline`: Article headline
- `source`: News source
- `url`: Article URL
- `summary`: Article summary
- `created_at`: Article creation time
- `updated_at`: Article update time
- `symbols`: JSON array of stock symbols
- `author`: Article author
- `content`: Full article content
- `images`: JSON array of image URLs
- `filename`: Original JSON filename
- `loaded_at`: When article was loaded into database
- Plus any custom fields added dynamically

### Field Configs Table
- `id`: Primary key
- `name`: Field name (internal)
- `display_name`: User-friendly name
- `field_type`: Type (text, boolean, select, number, date)
- `default_value`: Default value
- `options`: JSON array of options (for select fields)
- `required`: Whether field is required
- `created_at`: When field was created

### Article Labels Table
- `id`: Primary key
- `article_id`: Reference to article
- `field_name`: Reference to field config
- `value`: Label value
- `created_at`: When label was created
- `updated_at`: When label was last updated

## Configuration

The application uses environment variables for configuration. Create a `.env` file:

```env
DATABASE_URL=postgresql://localhost/alpaca_news
SECRET_KEY=your-secret-key-change-in-production
```

## Troubleshooting

### Database Connection Issues
- Make sure PostgreSQL is running
- Check that the `alpaca_news` database exists
- Verify connection settings in `.env` file

### Article Loading Issues
- Check that JSON files exist in `../output/alpaca/`
- Verify file permissions
- Check application logs for specific errors

### Field Management Issues
- Ensure you have proper database permissions
- Check that field names are valid (alphanumeric with underscores)
- Verify that required options are provided for dropdown fields

## Development

To run in development mode:
```bash
cd flask
python main.py
```

The application will run in debug mode with auto-reload enabled.

## License

This project is part of the Alpaca News trading system.

