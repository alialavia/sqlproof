"""Allow ``python -m sqlproof`` as an alias for the ``sqlproof`` console script."""

from sqlproof.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
