from utils.file_handler import read_sales_data
from utils.data_processor import (
    parse_transactions,
    validate_and_filter,
    calculate_total_revenue,
    region_wise_sales,
    top_selling_products,
    customer_analysis,
    daily_sales_trend,
    find_peak_sales_day,
    low_performing_products,
    enrich_sales_data,
    generate_sales_report
)
from utils.api_handler import fetch_all_products, create_product_mapping


def main():
    """
    Main execution function for Sales Analytics System
    """
    try:
        print("=" * 40)
        print("        SALES ANALYTICS SYSTEM")
        print("=" * 40)

        # 1. Read sales data
        print("\n[1/10] Reading sales data...")
        raw_lines = read_sales_data("data/sales_data.txt")
        print(f"✓ Successfully read {len(raw_lines)} transactions")

        # 2. Parse and clean
        print("\n[2/10] Parsing and cleaning data...")
        transactions = parse_transactions(raw_lines)
        print(f"✓ Parsed {len(transactions)} records")

        # 3. Show filter options
        print("\n[3/10] Filter Options Available:")
        regions = sorted(set(t["Region"] for t in transactions if t.get("Region")))
        amounts = [t["Quantity"] * t["UnitPrice"] for t in transactions if t["Quantity"] > 0 and t["UnitPrice"] > 0]

        print(f"Regions: {', '.join(regions)}")
        print(f"Amount Range: ₹{min(amounts):,.0f} - ₹{max(amounts):,.0f}")

        apply_filter = input("\nDo you want to filter data? (y/n): ").strip().lower()

        region_filter = None
        min_amount = None
        max_amount = None

        if apply_filter == "y":
            region_filter = input("Enter region (or press Enter to skip): ").strip()
            if not region_filter:
                region_filter = None

            min_amt_input = input("Enter minimum transaction amount (or press Enter to skip): ").strip()
            max_amt_input = input("Enter maximum transaction amount (or press Enter to skip): ").strip()

            min_amount = float(min_amt_input) if min_amt_input else None
            max_amount = float(max_amt_input) if max_amt_input else None

        # 4. Validate and filter
        print("\n[4/10] Validating transactions...")
        valid_tx, invalid_count, summary = validate_and_filter(
            transactions,
            region=region_filter,
            min_amount=min_amount,
            max_amount=max_amount
        )
        print(f"✓ Valid: {len(valid_tx)} | Invalid: {invalid_count}")

        # 5. Analysis
        print("\n[5/10] Analyzing sales data...")
        calculate_total_revenue(valid_tx)
        region_wise_sales(valid_tx)
        top_selling_products(valid_tx)
        customer_analysis(valid_tx)
        daily_sales_trend(valid_tx)
        find_peak_sales_day(valid_tx)
        low_performing_products(valid_tx)
        print("✓ Analysis complete")

        # 6. API fetch
        print("\n[6/10] Fetching product data from API...")
        api_products = fetch_all_products()
        product_mapping = create_product_mapping(api_products)
        print(f"✓ Fetched {len(api_products)} products")

        # 7. Enrichment
        print("\n[7/10] Enriching sales data...")
        enriched_tx = enrich_sales_data(valid_tx, product_mapping)
        enriched_count = sum(1 for t in enriched_tx if t.get("API_Match"))
        success_rate = (enriched_count / len(enriched_tx)) * 100 if enriched_tx else 0
        print(f"✓ Enriched {enriched_count}/{len(enriched_tx)} transactions ({success_rate:.1f}%)")

        # 8. Save enriched data
        print("\n[8/10] Saving enriched data...")
        print("✓ Saved to: data/enriched_sales_data.txt")

        # 9. Generate report
        print("\n[9/10] Generating report...")
        generate_sales_report(valid_tx, enriched_tx)
        print("✓ Report saved to: output/sales_report.txt")

        # 10. Complete
        print("\n[10/10] Process Complete!")
        print("=" * 40)

    except Exception as e:
        print("\n❌ An error occurred during execution")
        print(f"Reason: {e}")
        print("Please check input files and try again.")


if __name__ == "__main__":
    main()
