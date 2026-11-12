import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="Count the words in a file.")
    parser.add_argument("path")
    unit = parser.add_mutually_exclusive_group()
    unit.add_argument("--lines", action="store_true", help="count lines instead of words")
    unit.add_argument("--chars", action="store_true", help="count characters instead of words")
    args = parser.parse_args(argv)
    with open(args.path, encoding="utf-8") as file:
        text = file.read()
    if args.lines:
        print(len(text.splitlines()))
    elif args.chars:
        print(len(text))
    else:
        print(len(text.split()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
