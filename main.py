from utils.file_handler import load_file
from utils.data_processor import clean_sales_data, analyze_sales

def main():
    try:
        lines = load_file("data/sales_data.txt")
        cleaned_data = clean_sales_data(lines)
        analytics = analyze_sales(cleaned_data)

        with open("output/sales_report.txt", "w") as f:
            f.write("SALES ANALYTICS REPORT\n")
            f.write("======================\n\n")
            f.write(f"Total Revenue: {analytics['total_revenue']:.2f}\n\n")

            f.write("Revenue by Region:\n")
            for region, revenue in analytics["region"].items():
                f.write(f"{region}: {revenue:.2f}\n")

            f.write("\nTop 5 Customers by Revenue:\n")
            for cust, rev in sorted(
                analytics["customer"].items(),
                key=lambda x: x[1],
                reverse=True
            )[:5]:
                f.write(f"{cust}: {rev:.2f}\n")

        print("Report generated successfully.")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()