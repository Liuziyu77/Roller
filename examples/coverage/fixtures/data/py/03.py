import json
from pathlib import Path


DATA_FILE = Path(__file__).with_name("migrations.json")


def load_migrations(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    print("Migration plan")
    print("==============")
    for migration in load_migrations(DATA_FILE):
        print(f"{migration['version']}: {migration['description']}")


if __name__ == "__main__":
    main()
