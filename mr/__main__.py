import sys

from mr import cli


def main(argv=None) -> int:
    if argv is None:
        sys.exit(cli.main(None))
    return cli.main(argv)


if __name__ == "__main__":
    sys.exit(main())
