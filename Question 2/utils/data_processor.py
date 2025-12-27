def parse_transactions(raw_lines):
    """
    Parses raw lines into clean list of dictionaries
    """
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
    """
    Validates transactions and applies optional filters
    """
    valid_transactions = []
    invalid_count = 0

    regions = set()
    amounts = []

    for tx in transactions:
        if (
            tx.get("Quantity", 0) <= 0 or
            tx.get("UnitPrice", 0) <= 0 or
            not tx.get("TransactionID", "").startswith("T") or
            not tx.get("ProductID", "").startswith("P") or
            not tx.get("CustomerID", "").startswith("C") or
            not tx.get("Region")
        ):
            invalid_count += 1
            continue

        amount = tx["Quantity"] * tx["UnitPrice"]
        regions.add(tx["Region"])
        amounts.append(amount)

        tx["_amount"] = amount
        valid_transactions.append(tx)

    print(f"Available regions: {sorted(regions)}")
    if amounts:
        print(f"Transaction amount range: {min(amounts)} - {max(amounts)}")

    filtered = valid_transactions
    filtered_by_region = 0
    filtered_by_amount = 0

    if region:
        before = len(filtered)
        filtered = [tx for tx in filtered if tx["Region"] == region]
        filtered_by_region = before - len(filtered)

    if min_amount is not None:
        before = len(filtered)
        filtered = [tx for tx in filtered if tx["_amount"] >= min_amount]
        filtered_by_amount += before - len(filtered)

    if max_amount is not None:
        before = len(filtered)
        filtered = [tx for tx in filtered if tx["_amount"] <= max_amount]
        filtered_by_amount += before - len(filtered)

    for tx in filtered:
        tx.pop("_amount", None)

    summary = {
        "total_input": len(transactions),
        "invalid": invalid_count,
        "filtered_by_region": filtered_by_region,
        "filtered_by_amount": filtered_by_amount,
        "final_count": len(filtered)
    }

    return filtered, invalid_count, summary