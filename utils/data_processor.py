from collections import defaultdict
from utils.api_handler import fetch_product_category

def clean_sales_data(lines):
    header, records = lines[0], lines[1:]

    total_records = len([r for r in records if r.strip()])
    invalid_records = 0
    valid_records = []

    for line in records:
        if not line.strip():
            continue

        parts = line.strip().split("|")

        if len(parts) < 8:
            invalid_records += 1
            continue

        parts = parts[:8]
        tid, date, pid, pname, qty, price, cid, region = parts

        if not tid.startswith("T"):
            invalid_records += 1
            continue

        if not cid or not region:
            invalid_records += 1
            continue

        try:
            qty = int(qty)
            price = float(price.replace(",", ""))
        except ValueError:
            invalid_records += 1
            continue

        if qty <= 0 or price <= 0:
            invalid_records += 1
            continue

        pname = pname.replace(",", "")
        category = fetch_product_category(pid)

        valid_records.append({
            "TransactionID": tid,
            "Date": date,
            "ProductID": pid,
            "ProductName": pname,
            "Category": category,
            "Quantity": qty,
            "UnitPrice": price,
            "CustomerID": cid,
            "Region": region
        })

    print(f"Total records parsed: {total_records}")
    print(f"Invalid records removed: {invalid_records}")
    print(f"Valid records after cleaning: {len(valid_records)}")

    return valid_records


def analyze_sales(data):
    total_revenue = 0
    revenue_by_region = defaultdict(float)
    revenue_by_product = defaultdict(float)
    revenue_by_customer = defaultdict(float)

    for row in data:
        revenue = row["Quantity"] * row["UnitPrice"]
        total_revenue += revenue

        revenue_by_region[row["Region"]] += revenue
        revenue_by_product[row["ProductName"]] += revenue
        revenue_by_customer[row["CustomerID"]] += revenue

    return {
        "total_revenue": total_revenue,
        "region": dict(revenue_by_region),
        "product": dict(revenue_by_product),
        "customer": dict(revenue_by_customer)
    }