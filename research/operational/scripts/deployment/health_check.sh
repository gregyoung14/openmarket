#!/bin/bash
# Health Check Script
# Run periodically to verify scraper health

APP_DIR="$(dirname "$0")/../.."
LOG_FILE="$APP_DIR/logs/scraper.log"
DB_FILE="$APP_DIR/polymarket_data.db"

echo "================================================================================"
echo "Polymarket BTC Scraper - Health Check"
echo "$(date)"
echo "================================================================================"

# 1. Check service status
echo ""
echo "1. Service Status:"
status=$(supervisorctl status polymarket-scraper 2>/dev/null || echo "UNKNOWN")
if [[ $status == *"RUNNING"* ]]; then
    echo "   ✓ Service is RUNNING"
else
    echo "   ✗ Service is NOT RUNNING: $status"
fi

# 2. Check recent errors
echo ""
echo "2. Recent Errors (last 24 hours):"
if [ -f "$LOG_FILE" ]; then
    error_count=$(grep -c ERROR "$LOG_FILE" 2>/dev/null | tail -1 || echo "0")
    if [ "$error_count" -gt 0 ]; then
        echo "   ⚠️  Found $error_count errors"
        tail -5 "$LOG_FILE" | grep ERROR || true
    else
        echo "   ✓ No recent errors"
    fi
else
    echo "   ⚠️  Log file not found: $LOG_FILE"
fi

# 3. Check database
echo ""
echo "3. Database Status:"
if [ -f "$DB_FILE" ]; then
    db_size=$(du -h "$DB_FILE" | cut -f1)
    trade_count=$(sqlite3 "$DB_FILE" "SELECT COUNT(*) FROM trades 2>/dev/null;" 2>/dev/null || echo "ERROR")
    echo "   ✓ Database exists"
    echo "   - Size: $db_size"
    echo "   - Trade records: $trade_count"
else
    echo "   ✗ Database not found: $DB_FILE"
fi

# 4. Check VPS location
echo ""
echo "4. VPS Location:"
location=$(curl -s ipinfo.io | grep -o '"country":"[^"]*"' | cut -d'"' -f4)
if [ -z "$location" ]; then
    location="UNKNOWN"
fi

if [ "$location" = "US" ]; then
    echo "   ✗ VPS is in UNITED STATES - API will not work!"
else
    echo "   ✓ VPS location: $location"
fi

# 5. Check disk space
echo ""
echo "5. Disk Space:"
disk_usage=$(df $APP_DIR | tail -1 | awk '{print int($5)}')
if [ "$disk_usage" -gt 90 ]; then
    echo "   ✗ WARNING: Disk usage is ${disk_usage}%"
elif [ "$disk_usage" -gt 75 ]; then
    echo "   ⚠️  Disk usage is ${disk_usage}%"
else
    echo "   ✓ Disk usage: ${disk_usage}%"
fi

# 6. Check memory
echo ""
echo "6. Memory Usage:"
mem_usage=$(free | grep Mem | awk '{printf("%.1f", $3/$2 * 100)}')
echo "   Memory: ${mem_usage}%"

# 7. Last trade fetch
echo ""
echo "7. Last Trade Fetch:"
if [ -f "$LOG_FILE" ]; then
    last_fetch=$(grep -o "Fetched [0-9]* trades" "$LOG_FILE" | tail -1 || echo "No data")
    echo "   $last_fetch"
else
    echo "   ✗ Cannot determine"
fi

echo ""
echo "================================================================================"
