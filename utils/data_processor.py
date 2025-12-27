from datetime import datetime

# ---------- Q1: Parsing & Validation ----------

def parse_transactions(raw_lines):
    transactions = []

    for line in raw_lines:
        parts = line.split("|")
        if len(parts) != 8:
            continue

        tid, date, pid, pname, qty, price, cid, region = parts
        pname = pname.replace(",", "")

        try:
            qty = int(qty)
            price = float(price.replace(",", ""))
        except ValueError:
            continue

        transactions.append({
            "TransactionID": tid,
            "Date": date,
            "ProductID": pid,
            "ProductName": pname,
            "Quantity": qty,
            "UnitPrice": price,
            "CustomerID": cid,
            "Region": region
        })

    return transactions


def validate_and_filter(transactions, region=None, min_amount=None, max_amount=None):
    valid = []
    invalid = 0
    regions = set()
    amounts = []

    for tx in transactions:
        if (
            tx["Quantity"] <= 0 or
            tx["UnitPrice"] <= 0 or
            not tx["TransactionID"].startswith("T") or
            not tx["ProductID"].startswith("P") or
            not tx["CustomerID"].startswith("C") or
            not tx["Region"]
        ):
            invalid += 1
            continue

        amount = tx["Quantity"] * tx["UnitPrice"]
        tx["_amount"] = amount
        regions.add(tx["Region"])
        amounts.append(amount)
        valid.append(tx)

    print(f"Available regions: {sorted(regions)}")
    print(f"Transaction amount range: {min(amounts)} - {max(amounts)}")

    for t in valid:
        t.pop("_amount", None)

    summary = {
        "total_input": len(transactions),
        "invalid": invalid,
        "final_count": len(valid)
    }

    return valid, invalid, summary


# ---------- Q2: Sales Analytics ----------

def calculate_total_revenue(transactions):
    return sum(t["Quantity"] * t["UnitPrice"] for t in transactions)


def region_wise_sales(transactions):
    data = {}
    total = calculate_total_revenue(transactions)

    for t in transactions:
        r = t["Region"]
        amt = t["Quantity"] * t["UnitPrice"]

        if r not in data:
            data[r] = {"total_sales": 0.0, "transaction_count": 0}

        data[r]["total_sales"] += amt
        data[r]["transaction_count"] += 1

    for r in data:
        data[r]["percentage"] = round((data[r]["total_sales"] / total) * 100, 2)

    return dict(sorted(data.items(), key=lambda x: x[1]["total_sales"], reverse=True))


def top_selling_products(transactions, n=5):
    products = {}

    for t in transactions:
        p = t["ProductName"]
        qty = t["Quantity"]
        rev = qty * t["UnitPrice"]

        if p not in products:
            products[p] = {"qty": 0, "rev": 0.0}

        products[p]["qty"] += qty
        products[p]["rev"] += rev

    sorted_p = sorted(products.items(), key=lambda x: x[1]["qty"], reverse=True)
    return [(p, d["qty"], d["rev"]) for p, d in sorted_p[:n]]


def customer_analysis(transactions):
    customers = {}

    for t in transactions:
        c = t["CustomerID"]
        amt = t["Quantity"] * t["UnitPrice"]

        if c not in customers:
            customers[c] = {
                "total_spent": 0.0,
                "purchase_count": 0,
                "products_bought": set()
            }

        customers[c]["total_spent"] += amt
        customers[c]["purchase_count"] += 1
        customers[c]["products_bought"].add(t["ProductName"])

    for c in customers:
        customers[c]["avg_order_value"] = round(
            customers[c]["total_spent"] / customers[c]["purchase_count"], 2
        )
        customers[c]["products_bought"] = list(customers[c]["products_bought"])

    return dict(sorted(customers.items(), key=lambda x: x[1]["total_spent"], reverse=True))


def daily_sales_trend(transactions):
    daily = {}

    for t in transactions:
        d = t["Date"]
        amt = t["Quantity"] * t["UnitPrice"]

        if d not in daily:
            daily[d] = {
                "revenue": 0.0,
                "transaction_count": 0,
                "customers": set()
            }

        daily[d]["revenue"] += amt
        daily[d]["transaction_count"] += 1
        daily[d]["customers"].add(t["CustomerID"])

    for d in daily:
        daily[d]["unique_customers"] = len(daily[d]["customers"])
        del daily[d]["customers"]

    return dict(sorted(daily.items()))


def find_peak_sales_day(transactions):
    daily = daily_sales_trend(transactions)
    peak = max(daily.items(), key=lambda x: x[1]["revenue"])
    return peak[0], peak[1]["revenue"], peak[1]["transaction_count"]


def low_performing_products(transactions, threshold=10):
    products = {}

    for t in transactions:
        p = t["ProductName"]
        qty = t["Quantity"]
        rev = qty * t["UnitPrice"]

        if p not in products:
            products[p] = {"qty": 0, "rev": 0.0}

        products[p]["qty"] += qty
        products[p]["rev"] += rev

    low = [(p, d["qty"], d["rev"]) for p, d in products.items() if d["qty"] < threshold]
    return sorted(low, key=lambda x: x[1])


# ---------- Q4: API Enrichment ----------

def enrich_sales_data(transactions, product_mapping):
    enriched = []

    for tx in transactions:
        new_tx = tx.copy()

        try:
            numeric_id = int(tx["ProductID"].replace("P", ""))
        except Exception:
            numeric_id = None

        if numeric_id and numeric_id in product_mapping:
            api = product_mapping[numeric_id]
            new_tx["API_Category"] = api["category"]
            new_tx["API_Brand"] = api["brand"]
            new_tx["API_Rating"] = api["rating"]
            new_tx["API_Match"] = True
        else:
            new_tx["API_Category"] = None
            new_tx["API_Brand"] = None
            new_tx["API_Rating"] = None
            new_tx["API_Match"] = False

        enriched.append(new_tx)

    save_enriched_data(enriched)
    return enriched


def save_enriched_data(enriched_transactions, filename="data/enriched_sales_data.txt"):
    header = [
        "TransactionID", "Date", "ProductID", "ProductName",
        "Quantity", "UnitPrice", "CustomerID", "Region",
        "API_Category", "API_Brand", "API_Rating", "API_Match"
    ]

    with open(filename, "w", encoding="utf-8") as f:
        f.write("|".join(header) + "\n")

        for tx in enriched_transactions:
            row = []
            for col in header:
                val = tx.get(col)
                row.append("" if val is None else str(val))
            f.write("|".join(row) + "\n")
            
 
# ---------- Q5: Report Generation ----------
 
def generate_sales_report(transactions, enriched_transactions, output_file="output/sales_report.txt"):
    """
    Generates a comprehensive formatted text sales report
    """

    # ---------- BASIC METRICS ----------
    total_transactions = len(transactions)
    total_revenue = sum(t["Quantity"] * t["UnitPrice"] for t in transactions)
    avg_order_value = total_revenue / total_transactions if total_transactions else 0

    dates = sorted(t["Date"] for t in transactions)
    date_range = f"{dates[0]} to {dates[-1]}" if dates else "N/A"

    # ---------- REGION DATA ----------
    region_data = {}
    for t in transactions:
        r = t["Region"]
        amt = t["Quantity"] * t["UnitPrice"]
        if r not in region_data:
            region_data[r] = {"sales": 0.0, "count": 0}
        region_data[r]["sales"] += amt
        region_data[r]["count"] += 1

    # ---------- TOP PRODUCTS ----------
    product_data = {}
    for t in transactions:
        p = t["ProductName"]
        amt = t["Quantity"] * t["UnitPrice"]
        if p not in product_data:
            product_data[p] = {"qty": 0, "rev": 0.0}
        product_data[p]["qty"] += t["Quantity"]
        product_data[p]["rev"] += amt

    top_products = sorted(product_data.items(), key=lambda x: x[1]["qty"], reverse=True)[:5]

    # ---------- TOP CUSTOMERS ----------
    customer_data = {}
    for t in transactions:
        c = t["CustomerID"]
        amt = t["Quantity"] * t["UnitPrice"]
        if c not in customer_data:
            customer_data[c] = {"spent": 0.0, "count": 0}
        customer_data[c]["spent"] += amt
        customer_data[c]["count"] += 1

    top_customers = sorted(customer_data.items(), key=lambda x: x[1]["spent"], reverse=True)[:5]

    # ---------- DAILY TREND ----------
    daily = {}
    for t in transactions:
        d = t["Date"]
        amt = t["Quantity"] * t["UnitPrice"]
        if d not in daily:
            daily[d] = {"rev": 0.0, "count": 0, "customers": set()}
        daily[d]["rev"] += amt
        daily[d]["count"] += 1
        daily[d]["customers"].add(t["CustomerID"])

    # ---------- API ENRICHMENT ----------
    enriched_count = sum(1 for t in enriched_transactions if t.get("API_Match"))
    success_rate = (enriched_count / len(enriched_transactions)) * 100 if enriched_transactions else 0

    unenriched_products = sorted(
        set(t["ProductName"] for t in enriched_transactions if not t.get("API_Match"))
    )

    # ---------- WRITE REPORT ----------
    with open(output_file, "a", encoding="utf-8") as f:
        f.write("\n\n" + "=" * 60 + "\n")
        f.write("Q5 -> COMPREHENSIVE SALES REPORT \n")
        f.write(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"  Records Processed: {total_transactions}\n")
        f.write("=" * 60 + "\n\n")

        # 1. OVERALL SUMMARY
        f.write("OVERALL SUMMARY\n")
        f.write("-" * 44 + "\n")
        f.write(f"Total Revenue:        ₹{total_revenue:,.2f}\n")
        f.write(f"Total Transactions:   {total_transactions}\n")
        f.write(f"Average Order Value:  ₹{avg_order_value:,.2f}\n")
        f.write(f"Date Range:           {date_range}\n\n")

        # 2. REGION PERFORMANCE
        f.write("REGION-WISE PERFORMANCE\n")
        f.write("-" * 44 + "\n")
        f.write("Region    Sales           % of Total   Transactions\n")
        for r, d in sorted(region_data.items(), key=lambda x: x[1]["sales"], reverse=True):
            pct = (d["sales"] / total_revenue) * 100
            f.write(f"{r:<9} ₹{d['sales']:>12,.2f}   {pct:>6.2f}%        {d['count']}\n")
        f.write("\n")

        # 3. TOP PRODUCTS
        f.write("TOP 5 PRODUCTS\n")
        f.write("-" * 44 + "\n")
        f.write("Rank  Product              Quantity   Revenue\n")
        for i, (p, d) in enumerate(top_products, 1):
            f.write(f"{i:<5} {p:<20} {d['qty']:>8}   ₹{d['rev']:,.2f}\n")
        f.write("\n")

        # 4. TOP CUSTOMERS
        f.write("TOP 5 CUSTOMERS\n")
        f.write("-" * 44 + "\n")
        f.write("Rank  Customer   Total Spent     Orders\n")
        for i, (c, d) in enumerate(top_customers, 1):
            f.write(f"{i:<5} {c:<9} ₹{d['spent']:>10,.2f}     {d['count']}\n")
        f.write("\n")

        # 5. DAILY SALES TREND
        f.write("DAILY SALES TREND\n")
        f.write("-" * 44 + "\n")
        f.write("Date         Revenue        Txns   Customers\n")
        for d, info in sorted(daily.items()):
            f.write(f"{d}   ₹{info['rev']:>10,.2f}   {info['count']:>4}   {len(info['customers'])}\n")
        f.write("\n")

        # 6. PRODUCT PERFORMANCE
        f.write("PRODUCT PERFORMANCE ANALYSIS\n")
        f.write("-" * 44 + "\n")
        best_day = max(daily.items(), key=lambda x: x[1]["rev"])
        f.write(f"Best Selling Day: {best_day[0]} (₹{best_day[1]['rev']:,.2f})\n\n")

        # 7. API ENRICHMENT SUMMARY
        f.write("API ENRICHMENT SUMMARY\n")
        f.write("-" * 44 + "\n")
        f.write(f"Total Records Enriched: {enriched_count}\n")
        f.write(f"Success Rate: {success_rate:.2f}%\n")
        f.write("Products Not Enriched:\n")
        for p in unenriched_products:
            f.write(f" - {p}\n")
