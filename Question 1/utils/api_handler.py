import requests

def fetch_product_category(product_id):
    """
    Fetch product category from an external API using ProductID.
    """
    try:
        pid_num = int(product_id.replace("P", "")) % 10 + 1
        url = f"https://dummyjson.com/products/{pid_num}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        return response.json().get("category", "Unknown")
    except Exception:
        return "Unknown"