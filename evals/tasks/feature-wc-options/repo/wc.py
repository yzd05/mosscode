import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Count the words in a file.")
    parser.add_argument("path")
    args = parser.parse_args(argv)
    with open(args.path, encoding="utf-8") as file:
        text = file.read()
    print(len(text.split()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
