def read_sales_data(filename):
    """
    Reads sales data from file handling encoding issues

    Returns: list of raw lines (strings)
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

    # Skip header and remove empty lines
    cleaned_lines = []
    for line in lines[1:]:
        line = line.strip()
        if line:
            cleaned_lines.append(line)

    return cleaned_lines