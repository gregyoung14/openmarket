"""
Quick proxy test — connects to Polymarket WS through SOCKS5 proxy.
Usage: python test_proxy.py
"""

import json
import websocket
import socks
import socket

# Set up SOCKS5 proxy
PROXY_HOST = "3.248.72.89"
PROXY_PORT = 1080

socks.set_default_proxy(socks.SOCKS5, PROXY_HOST, PROXY_PORT)
socket.socket = socks.socksocket

# Polymarket public market WS
ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/"

def on_message(ws, message):
    print("Received:", message[:200])

def on_error(ws, error):
    print("Error:", error)

def on_close(ws, close_status_code, close_msg):
    print("Closed:", close_status_code, close_msg)

def on_open(ws):
    print(f"Connected via proxy {PROXY_HOST}:{PROXY_PORT}!")
    sub_msg = {
        "method": "subscribe",
        "channel": "market",
        "params": {}
    }
    ws.send(json.dumps(sub_msg))
    print("Subscribed to market channel")

ws = websocket.WebSocketApp(ws_url,
                            on_open=on_open,
                            on_message=on_message,
                            on_error=on_error,
                            on_close=on_close)

print(f"Connecting to {ws_url} via SOCKS5 {PROXY_HOST}:{PROXY_PORT}...")
ws.run_forever()
