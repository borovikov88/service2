#!/usr/bin/env python3
"""Fail closed unless paginated GitHub reviews contain a current approval."""

import argparse
import json
import sys


DECISIVE_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


def has_current_independent_approval(pages, author, head_sha):
    if not isinstance(pages, list) or not pages:
        return False
    reviews = []
    for page in pages:
        if not isinstance(page, list):
            return False
        reviews.extend(page)

    latest = {}
    for review in reviews:
        if not isinstance(review, dict):
            return False
        state = review.get("state")
        if state not in DECISIVE_STATES or review.get("commit_id") != head_sha:
            continue
        user = review.get("user")
        review_id = review.get("id")
        if not isinstance(user, dict) or not isinstance(user.get("login"), str):
            return False
        login = user["login"]
        if not login or login == author or not isinstance(review_id, int):
            continue
        if login not in latest or review_id > latest[login][0]:
            latest[login] = (review_id, state)
    return any(state == "APPROVED" for _, state in latest.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--author", required=True)
    parser.add_argument("--head-sha", required=True)
    args = parser.parse_args()
    try:
        pages = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 2
    return 0 if has_current_independent_approval(pages, args.author, args.head_sha) else 1


if __name__ == "__main__":
    raise SystemExit(main())
