#!/usr/bin/env python3
"""
Setup script for the Alpaca News Labeler Flask application
"""

import os
import sys
import subprocess
import psycopg2
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

def check_postgresql():
    """Check if PostgreSQL is running and accessible"""
    print("Checking PostgreSQL connection...")
    
    # Load environment variables
    from dotenv import load_dotenv
    load_dotenv()
    
    # Try to use the DATABASE_URL from .env file first
    database_url = os.getenv('DATABASE_URL')
    if database_url:
        try:
            print(f"  Trying connection with DATABASE_URL from .env...")
            conn = psycopg2.connect(database_url)
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cursor = conn.cursor()
            
            # Check if database exists
            cursor.execute("SELECT 1 FROM pg_database WHERE datname='alpaca_news'")
            exists = cursor.fetchone()
            
            if not exists:
                print("Creating database 'alpaca_news'...")
                cursor.execute("CREATE DATABASE alpaca_news")
                print("Database created successfully!")
            else:
                print("Database 'alpaca_news' already exists.")
            
            cursor.close()
            conn.close()
            print("✓ PostgreSQL connection successful!")
            return True
            
        except Exception as e:
            print(f"    Connection with DATABASE_URL failed: {e}")
    
    # Fallback connection methods
    connection_methods = [
        # Method 1: Try with current user (peer authentication)
        {
            'host': 'localhost',
            'user': os.getenv('USER', 'postgres'),
            'password': '',
            'database': 'postgres'
        },
        # Method 2: Try with postgres user (might need password)
        {
            'host': 'localhost',
            'user': 'postgres',
            'password': '',
            'database': 'postgres'
        },
        # Method 3: Try with socket connection
        {
            'host': '/var/run/postgresql',
            'user': os.getenv('USER', 'postgres'),
            'password': '',
            'database': 'postgres'
        }
    ]
    
    for i, params in enumerate(connection_methods, 1):
        try:
            print(f"  Trying connection method {i}...")
            conn = psycopg2.connect(**params)
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            cursor = conn.cursor()
            
            # Check if database exists
            cursor.execute("SELECT 1 FROM pg_database WHERE datname='alpaca_news'")
            exists = cursor.fetchone()
            
            if not exists:
                print("Creating database 'alpaca_news'...")
                cursor.execute("CREATE DATABASE alpaca_news")
                print("Database created successfully!")
            else:
                print("Database 'alpaca_news' already exists.")
            
            cursor.close()
            conn.close()
            print("✓ PostgreSQL connection successful!")
            return True
            
        except psycopg2.OperationalError as e:
            print(f"    Connection method {i} failed: {e}")
            continue
        except Exception as e:
            print(f"    Unexpected error with method {i}: {e}")
            continue
    
    print("\n❌ All connection methods failed.")
    print("\nTo fix this, try one of these solutions:")
    print("\n1. Create the database manually:")
    print("   sudo -u postgres psql")
    print("   CREATE DATABASE alpaca_news;")
    print("   \\q")
    print("\n2. Or set up password authentication:")
    print("   sudo -u postgres psql")
    print("   ALTER USER postgres PASSWORD 'your_password';")
    print("   \\q")
    print("   Then update the .env file with the password")
    print("\n3. Or use peer authentication (current user):")
    print("   sudo -u postgres createuser --superuser $USER")
    print("   createdb alpaca_news")
    
    return False

def install_requirements():
    """Install Python requirements"""
    print("Installing Python requirements...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"])
        print("Requirements installed successfully!")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error installing requirements: {e}")
        return False

def create_env_file():
    """Create .env file with database configuration"""
    env_file = ".env"
    if not os.path.exists(env_file):
        print("Creating .env file...")
        with open(env_file, "w") as f:
            f.write("# Database configuration\n")
            f.write("# Try one of these URLs depending on your PostgreSQL setup:\n")
            f.write("# For peer authentication (current user):\n")
            f.write("DATABASE_URL=postgresql://localhost/alpaca_news\n")
            f.write("# For password authentication:\n")
            f.write("# DATABASE_URL=postgresql://postgres:your_password@localhost/alpaca_news\n")
            f.write("# For socket connection:\n")
            f.write("# DATABASE_URL=postgresql:///alpaca_news\n")
            f.write("\n")
            f.write("SECRET_KEY=your-secret-key-change-in-production\n")
        print(".env file created!")
        print("Note: You may need to update the DATABASE_URL in .env if connection fails")
    else:
        print(".env file already exists.")

def main():
    print("Setting up Alpaca News Labeler...")
    print("=" * 50)
    
    # Check PostgreSQL
    if not check_postgresql():
        return False
    
    # Install requirements
    if not install_requirements():
        return False
    
    # Create .env file
    create_env_file()
    
    print("\n" + "=" * 50)
    print("Setup completed successfully!")
    print("\nTo run the application:")
    print("  cd flask")
    print("  python main.py")
    print("\nThen open your browser to: http://localhost:5000")
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
