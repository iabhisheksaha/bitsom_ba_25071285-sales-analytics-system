# Sales Analytics System  
BITSOM - Graded Assignment 3 Submission

---

## Assignment Coverage Summary

This repository implements **all questions from Q1 to Q6** of the Sales Analytics System assignment, following the given problem statement and marking scheme.

| Assignment Part | Implemented In |
|-----------------|--------------|
Part 1 – File Handling & Preprocessing | `utils/file_handler.py`, `utils/data_processor.py`
Part 2 – Data Processing & Analytics | `utils/data_processor.py`
Part 3 – API Integration | `utils/api_handler.py`, `utils/data_processor.py`
Part 4 – Report Generation | `utils/data_processor.py`
Part 5 – Main Application Flow | `main.py`

The system executes end-to-end using a **single entry point (`main.py`)**.

---

## Repository Structure

sales-analytics-system/
├── main.py
├── requirements.txt
├── README.md
│
├── utils/
│ ├── file_handler.py # File reading with encoding handling
│ ├── data_processor.py # Parsing, validation, analytics, enrichment, reporting
│ └── api_handler.py # DummyJSON API integration
│
├── data/
│ ├── sales_data.txt
│ └── enriched_sales_data.txt
│
└── output/
└── sales_report.txt


---

## How to Run (Important)

### Step 1: Install Dependencies
```bash
pip install -r requirements.txt

### Step 2: Run the Program
python main.py

