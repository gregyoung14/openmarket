#!/bin/bash
# Database Backup Script
# Creates timestamped backups of the SQLite database

APP_DIR="/home/polymarket/openmarket"
DB_FILE="$APP_DIR/polymarket_data.db"
BACKUP_DIR="$APP_DIR/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/polymarket_data_$TIMESTAMP.db.gz"

# Create backup directory
mkdir -p "$BACKUP_DIR"

echo "$(date): Starting database backup..."

# Check if database exists
if [ ! -f "$DB_FILE" ]; then
    echo "$(date): ERROR - Database not found: $DB_FILE"
    exit 1
fi

# Create compressed backup
gzip -c "$DB_FILE" > "$BACKUP_FILE"

if [ -f "$BACKUP_FILE" ]; then
    size=$(du -h "$BACKUP_FILE" | cut -f1)
    echo "$(date): ✓ Backup created: $BACKUP_FILE ($size)"
    
    # Keep only last 7 backups
    backups_count=$(ls -1 "$BACKUP_DIR"/polymarket_data_*.db.gz 2>/dev/null | wc -l)
    if [ "$backups_count" -gt 7 ]; then
        echo "$(date): Cleaning old backups..."
        ls -1tr "$BACKUP_DIR"/polymarket_data_*.db.gz | head -n -7 | xargs rm
        echo "$(date): ✓ Old backups removed"
    fi
else
    echo "$(date): ERROR - Failed to create backup"
    exit 1
fi

echo "$(date): Backup complete"
