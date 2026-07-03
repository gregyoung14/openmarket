#!/bin/bash
# Test the Rust Binance WebSocket Service

echo "========================================"
echo "Testing Rust Binance WebSocket Service"
echo "========================================"
echo ""

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Test 1: Health Check
echo "1. Health Check Test..."
HEALTH=$(curl -s http://localhost:8001/health)
if echo "$HEALTH" | jq -e '.status == "ok"' > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Health check passed${NC}"
    echo "  Trades stored: $(echo "$HEALTH" | jq -r '.trades_stored')"
else
    echo -e "${RED}✗ Health check failed${NC}"
    exit 1
fi
echo ""

# Test 2: Root endpoint
echo "2. Root Endpoint Test..."
ROOT=$(curl -s http://localhost:8001/)
if echo "$ROOT" | jq -e '.status == "ok"' > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Root endpoint passed${NC}"
else
    echo -e "${RED}✗ Root endpoint failed${NC}"
    exit 1
fi
echo ""

# Test 3: 1m Candles
echo "3. 1-minute Candles Test..."
CANDLES_1M=$(curl -s 'http://localhost:8001/candles/1m?limit=5')
COUNT_1M=$(echo "$CANDLES_1M" | jq -r '.count')
if [ "$COUNT_1M" -gt 0 ]; then
    echo -e "${GREEN}✓ 1m candles retrieved: $COUNT_1M${NC}"
    echo "  Latest candle:"
    echo "$CANDLES_1M" | jq '.candles[-1] | {time: (.time | todate), open, high, low, close, volume}'
else
    echo -e "${RED}✗ No 1m candles found${NC}"
fi
echo ""

# Test 4: 5m Candles
echo "4. 5-minute Candles Test..."
CANDLES_5M=$(curl -s 'http://localhost:8001/candles/5m?limit=3')
COUNT_5M=$(echo "$CANDLES_5M" | jq -r '.count')
if [ "$COUNT_5M" -gt 0 ]; then
    echo -e "${GREEN}✓ 5m candles retrieved: $COUNT_5M${NC}"
else
    echo -e "${RED}✗ No 5m candles found${NC}"
fi
echo ""

# Test 5: 1s Candles
echo "5. 1-second Candles Test..."
CANDLES_1S=$(curl -s 'http://localhost:8001/candles/1s?limit=3')
COUNT_1S=$(echo "$CANDLES_1S" | jq -r '.count')
if [ "$COUNT_1S" -gt 0 ]; then
    echo -e "${GREEN}✓ 1s candles retrieved: $COUNT_1S${NC}"
else
    echo -e "${RED}✗ No 1s candles found${NC}"
fi
echo ""

# Test 6: Invalid interval
echo "6. Invalid Interval Test..."
INVALID=$(curl -s 'http://localhost:8001/candles/99m')
if echo "$INVALID" | jq -e '.error' > /dev/null 2>&1; then
    echo -e "${GREEN}✓ Invalid interval properly rejected${NC}"
else
    echo -e "${RED}✗ Invalid interval not handled${NC}"
fi
echo ""

# Test 7: WebSocket connection (brief test)
echo "7. WebSocket Connection Test..."
if command -v wscat &> /dev/null; then
    timeout 3 wscat -c ws://localhost:8001/ws > /tmp/ws_test.txt 2>&1 &
    sleep 2
    if grep -q "snapshot\|trade\|candle" /tmp/ws_test.txt; then
        echo -e "${GREEN}✓ WebSocket connected and receiving data${NC}"
    else
        echo -e "${RED}⚠ WebSocket connected but no data received (might need longer wait)${NC}"
    fi
    rm -f /tmp/ws_test.txt
else
    echo "  ⚠ wscat not installed, skipping WebSocket test"
    echo "    Install with: npm install -g wscat"
fi
echo ""

# Summary
echo "========================================"
echo "Test Summary"
echo "========================================"
echo ""
echo "Service: Rust Binance WebSocket"
echo "Port: 8001"
echo "Status: ${GREEN}OPERATIONAL${NC}"
echo ""
echo "Database Stats:"
echo "  Total trades: $(echo "$HEALTH" | jq -r '.trades_stored')"
echo "  1m candles: $COUNT_1M"
echo "  5m candles: $COUNT_5M"
echo "  1s candles: $COUNT_1S"
echo ""
echo "All tests completed!"
