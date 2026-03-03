import os
import requests

BASE_URL = "https://api.elections.kalshi.com"

API_KEY = os.environ.get("KALSHI_API_KEY")
API_SECRET = os.environ.get("KALSHI_API_SECRET")

HEADERS = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {API_KEY}"
}

def get_orderbook(ticker):
    url = f"{BASE_URL}/markets/{ticker}/orderbook"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json()

def create_order(payload):
    url = f"{BASE_URL}/portfolio/orders"
    r = requests.post(url, headers=HEADERS, json=payload)
    r.raise_for_status()
    return r.json()

def get_order(order_id):
    url = f"{BASE_URL}/portfolio/orders/{order_id}"
    r = requests.get(url, headers=HEADERS)
    r.raise_for_status()
    return r.json()
