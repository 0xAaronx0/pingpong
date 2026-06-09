"""Withdraw one of your offers (e.g. plans changed, or you already matched).

    python withdraw.py --offer-id <uuid>
"""
from __future__ import annotations

import argparse

import client


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offer-id", required=True)
    args = ap.parse_args()
    ident = client.Identity.load_or_create()
    client.delete(f"/offers/{args.offer_id}", ident=ident)
    print(f"Angebot {args.offer_id} zurückgezogen.")


if __name__ == "__main__":
    main()
