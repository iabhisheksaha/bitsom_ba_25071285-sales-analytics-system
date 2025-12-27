def load_file(file_path):
    try:
        with open(file_path, "r", encoding="latin-1") as file:
            return file.readlines()
    except FileNotFoundError:
        raise RuntimeError("Sales data file not found.")