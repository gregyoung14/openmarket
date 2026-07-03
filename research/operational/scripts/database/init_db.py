#!/usr/bin/env python3
"""
Database initialization script for Polymarket BTC Scraper
Creates necessary tables and indexes
"""
import sqlite3
import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import config

def init_database():
    """Initialize SQLite database with required tables and indexes"""
    
    db_url = config.DATABASE_URL or "sqlite:///./polymarket_data.db"
    
    # Extract database path from URL
    if db_url.startswith("sqlite:///"):
        db_path = db_url.replace("sqlite:///", "")
    else:
        print(f"Error: Only SQLite is supported for initialization. Got: {db_url}")
        return False
    
    # Create directory if needed
    db_dir = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(db_dir, exist_ok=True)
    
    print(f"Initializing database: {db_path}")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Trades table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                side TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(timestamp, condition_id, token_id, side)
            )
        """)
        print("✓ Created trades table")
        
        # Markets table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                condition_id TEXT UNIQUE NOT NULL,
                slug TEXT,
                title TEXT,
                created_at INTEGER,
                resolved_at INTEGER,
                active BOOLEAN,
                volume REAL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        print("✓ Created markets table")
        
        # Indexes for faster queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp 
            ON trades(timestamp)
        """)
        print("✓ Created index on trades.timestamp")
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_condition 
            ON trades(condition_id)
        """)
        print("✓ Created index on trades.condition_id")
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_token 
            ON trades(token_id)
        """)
        print("✓ Created index on trades.token_id")
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_trades_composite 
            ON trades(condition_id, timestamp)
        """)
        print("✓ Created composite index on trades")
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_markets_condition 
            ON markets(condition_id)
        """)
        print("✓ Created index on markets.condition_id")
        
        conn.commit()
        print("\n✓ Database initialization complete!")
        return True
        
    except sqlite3.Error as e:
        print(f"✗ Database error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    success = init_database()
    sys.exit(0 if success else 1)
