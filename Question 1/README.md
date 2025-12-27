# Sales Analytics System

## Overview
This project processes messy sales transaction data, cleans and validates it,
enriches product information using an external API, analyzes sales patterns and
customer behavior, and generates a business-ready report.

## How to Run
1. Install dependencies:
   pip install -r requirements.txt

2. Run the application:
   python main.py

## Data Cleaning Rules
- Records with TransactionID not starting with `T` are removed
- Records missing CustomerID or Region are removed
- Records with Quantity ≤ 0 or UnitPrice ≤ 0 are removed
- Commas are removed from product names
- Numeric fields are cleaned and converted
- Empty lines are skipped
- Records with extra fields are safely trimmed

## API Integration
Product category information is fetched in real time from an external API
and added to the cleaned dataset.

## Output
- Console displays validation summary
- Report is generated at: output/sales_report.txt