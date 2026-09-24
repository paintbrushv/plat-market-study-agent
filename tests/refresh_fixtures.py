"""Refresh checked-in fixtures for the gainesville sources.

Manual / opt-in. Run before declaring a release if any source's parser
has been quiet for a while — site drift is silent until a parser test
starts flunking against fresh HTML.
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from pathlib import Path

USER_AGENT = (
    "market-study-agent/1.0 (Gainesville TX listings tracker; "
    "maintainers@example.invalid)"
)

REFRESH_TARGETS = {
    "craigslist_search.rss": "https://dallas.craigslist.org/search/apa?format=rss",
    "zori_sample.csv": (
        "https://files.zillowstatic.com/research/public_csvs/zori/"
        "Zip_zori_uc_sfrcondomfr_sm_month.csv"
    ),
    # Apartments.com / Zillow / property_direct fetches are JS-heavy.
    # Refresh those manually by saving page source from a real browser
    # and dropping the file at tests/gainesville/fixtures/<name>.html.
}


def refresh(name: str) -> Path:
    url = REFRESH_TARGETS[name]
    out = Path(__file__).parent / "gainesville" / "fixtures" / name
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        out.write_bytes(resp.read())
    print(f"refreshed {out}")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=list(REFRESH_TARGETS), required=False)
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    if args.all:
        for name in REFRESH_TARGETS:
            refresh(name)
    elif args.source:
        refresh(args.source)
    else:
        print("specify --source <name> or --all")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
