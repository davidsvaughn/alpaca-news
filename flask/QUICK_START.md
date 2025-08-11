# Quick Start Guide

## 🚀 Get Running in 5 Minutes

### Prerequisites
- Python 3.7+
- PostgreSQL (install if needed)

### Step 1: Install PostgreSQL (if needed)
```bash
# Ubuntu/Debian
sudo apt-get install postgresql postgresql-contrib
sudo systemctl start postgresql

# macOS
brew install postgresql
brew services start postgresql
```

### Step 2: Setup Application
```bash
cd flask
python setup.py
```

### Step 3: Run Application
```bash
python main.py
```

### Step 4: Access Application
Open browser to: **http://localhost:5000**

## 🎯 First Time Setup

1. **Go to Config page** (click "Config" in navigation)
2. **Add your first field**:
   - Field Name: `is_important`
   - Display Name: `Is Important`
   - Field Type: `Yes/No`
   - Click "Add Field"
3. **Load articles**: Click "Refresh" in navigation
4. **Start labeling**: Click "Label" on any article

## 🏷️ Example Fields to Add

- **Is Important** (Yes/No) - Mark important articles
- **News Type** (Dropdown) - Breaking, Analysis, Earnings, etc.
- **Relevance Score** (Number) - 1-10 relevance rating
- **Trading Impact** (Dropdown) - High, Medium, Low
- **Notes** (Text) - Your personal notes

## 🔍 Search & Filter

- **Search box**: Search headlines, summaries, content
- **Source filter**: Filter by news source (benzinga, etc.)
- **Field filters**: Filter by your custom labels
- **Pagination**: Navigate through large result sets

## 📱 Navigation

- **Articles**: Main listing page
- **Config**: Manage labeling fields
- **Refresh**: Load new articles from JSON files
- **Previous/Next**: Navigate between articles when labeling

## 🆘 Troubleshooting

**Database connection error:**
- Make sure PostgreSQL is running
- Check that `alpaca_news` database exists

**No articles showing:**
- Click "Refresh" to load articles
- Check that JSON files exist in `../output/alpaca/`

**Field not showing:**
- Go to Config page and verify field exists
- Check field configuration

## 📞 Support

- Check the full README.md for detailed documentation
- Review IMPLEMENTATION_SUMMARY.md for technical details
- All code is well-documented and commented

