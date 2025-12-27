import requests

def fetch_all_products():
    """
    Fetches all products from DummyJSON API
    """
    url = "https://dummyjson.com/products?limit=100"

    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        products = []
        for p in data.get("products", []):
            products.append({
                "id": p.get("id"),
                "title": p.get("title"),
                "category": p.get("category"),
                "brand": p.get("brand"),
                "price": p.get("price"),
                "rating": p.get("rating")
            })

        print("API Status: Successfully fetched products")
        return products

    except Exception as e:
        print(f"API Status: Failed to fetch products ({e})")
        return []


def create_product_mapping(api_products):
    """
    Creates mapping of product IDs to product info
    """
    mapping = {}

    for p in api_products:
        pid = p.get("id")
        if pid is not None:
            mapping[pid] = {
                "title": p.get("title"),
                "category": p.get("category"),
                "brand": p.get("brand"),
                "rating": p.get("rating")
            }

    return mapping