import json
from pathlib import Path


DATA_FILE = Path(__file__).with_name("feed_items.json")


def load_items(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    print("Feed items")
    print("==========")
    for item in load_items(DATA_FILE):
        print(f"{item['published_at']} {item['title']}")


if __name__ == "__main__":
    main()
