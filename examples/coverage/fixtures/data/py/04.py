import csv
from pathlib import Path


DATA_FILE = Path(__file__).with_name("rename_map.csv")


def load_mapping(path):
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main():
    print("重命名预览")
    print("==========")
    for row in load_mapping(DATA_FILE):
        print(f"{row['directory']}/{row['old_name']} -> {row['new_name']}")


if __name__ == "__main__":
    main()
