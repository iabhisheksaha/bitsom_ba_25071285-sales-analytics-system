def read_sales_data(filename):
    """
    Reads sales data from file handling encoding issues
    Returns: list of raw transaction lines
    """
    encodings = ["utf-8", "latin-1", "cp1252"]

    for enc in encodings:
        try:
            with open(filename, "r", encoding=enc) as f:
                lines = f.readlines()
            break
        except UnicodeDecodeError:
            continue
        except FileNotFoundError:
            raise FileNotFoundError(f"File not found: {filename}")
    else:
        raise UnicodeDecodeError("Unable to decode file with supported encodings")

    # Skip header and empty lines
    cleaned = []
    for line in lines[1:]:
        line = line.strip()
        if line:
            cleaned.append(line)

    return cleaned
