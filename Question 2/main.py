from utils.file_handler import read_sales_data
from utils.data_processor import parse_transactions, validate_and_filter

def main():
    raw_lines = read_sales_data("data/sales_data.txt")
    print(f"Raw transaction lines read: {len(raw_lines)}")

    transactions = parse_transactions(raw_lines)
    print(f"Parsed transactions: {len(transactions)}")

    valid_tx, invalid_count, summary = validate_and_filter(transactions)

    print("\nValidation summary:")
    for k, v in summary.items():
        print(f"{k}: {v}")

if __name__ == "__main__":
    main()