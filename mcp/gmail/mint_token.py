#!/usr/bin/env python3
"""Mint a long-lived Gmail refresh token for ONE shared mailbox.

Operator tool — runs on your laptop, NOT inside the container (it opens a
browser). It is deliberately not COPYed into the image.

    pip install google-auth-oauthlib google-api-python-client
    python mint_token.py --account choiz --client-secrets client_secret.json

It prints the four .env lines for that mailbox. Repeat once per mailbox,
signing in each time as the shared inbox itself (hola@choiz.com.mx,
hi@gotimeless.ai) — NOT as your personal account. The token you get can read
exactly the mailbox you signed in as, which is the whole security argument for
this approach over domain-wide delegation.

Prerequisites in Google Cloud, per Workspace org that owns a mailbox:

  1. A GCP project with the **Gmail API** enabled.
  2. OAuth consent screen with **User type = Internal**. This matters twice
     over: an "External" app requesting gmail.readonly (a Google *restricted*
     scope) needs formal verification plus a CASA security assessment, and an
     External app left in "Testing" hands out refresh tokens that expire after
     7 days. Internal apps have neither problem.
  3. An OAuth client of type **Desktop app**; download its JSON.
  4. Admin Console → Security → Access and data control → API controls →
     Manage third-party app access: mark the app **Trusted**. Restricted scopes
     are blocked by default, and the symptom is a 403 at tool-call time rather
     than a failure here.

If gotimeless.ai turns out to be a secondary domain of the Choiz Workspace
tenant (Admin Console → Account → Domains), one project and one client covers
both mailboxes — just run this twice with the same --client-secrets.
"""
from __future__ import annotations

import argparse
import sys

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mint a Gmail readonly refresh token for one shared mailbox.",
    )
    parser.add_argument(
        "--account",
        required=True,
        help="short key used in the env var names, e.g. choiz or timeless "
        "(must match an entry in GMAIL_ACCOUNTS)",
    )
    parser.add_argument(
        "--client-secrets",
        help="path to the Desktop-app OAuth client JSON downloaded from GCP",
    )
    parser.add_argument("--client-id", help="alternative to --client-secrets")
    parser.add_argument("--client-secret", help="alternative to --client-secrets")
    parser.add_argument(
        "--email",
        help="mailbox you intend to authorize; if given, the script refuses to "
        "print anything when you sign in as somebody else",
    )
    return parser.parse_args()


def build_flow(args: argparse.Namespace):
    from google_auth_oauthlib.flow import InstalledAppFlow

    if args.client_secrets:
        return InstalledAppFlow.from_client_secrets_file(args.client_secrets, SCOPES)
    if args.client_id and args.client_secret:
        config = {
            "installed": {
                "client_id": args.client_id,
                "client_secret": args.client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        }
        return InstalledAppFlow.from_client_config(config, SCOPES)
    sys.exit("need either --client-secrets, or both --client-id and --client-secret")


def main() -> int:
    args = parse_args()
    key = args.account.strip().lower()
    flow = build_flow(args)

    print(f"\nA browser will open. Sign in as the {key.upper()} shared mailbox")
    print("(NOT your personal account) and approve read-only Gmail access.\n")

    # access_type=offline is what returns a refresh_token at all;
    # prompt=consent forces a fresh one even if this client was authorized
    # before (Google otherwise silently omits it on re-consent).
    creds = flow.run_local_server(
        port=0, access_type="offline", prompt="consent", open_browser=True
    )

    if not creds.refresh_token:
        return exit_no_refresh_token()

    # Confirm which mailbox we actually got, before printing anything. Signing
    # in as the wrong user is the single most likely mistake here, and it fails
    # silently at runtime (the MCP would read the wrong inbox).
    from googleapiclient.discovery import build

    service = build("gmail", "v1", credentials=creds, static_discovery=True)
    profile = service.users().getProfile(userId="me").execute()
    actual = profile.get("emailAddress", "")

    if args.email and actual.lower() != args.email.strip().lower():
        print(
            f"\nABORTED: you signed in as {actual}, but --email said "
            f"{args.email}. Nothing printed. Sign out of Google in that browser "
            "profile and run this again as the shared mailbox.",
            file=sys.stderr,
        )
        return 1

    print(f"\nAuthorized {actual} ({profile.get('messagesTotal')} messages).")
    print("\nAdd these four lines to the EC2 .env (the refresh token is a")
    print("credential — do not commit it, do not paste it in Slack):\n")
    upper = key.upper()
    print(f"GMAIL_{upper}_EMAIL={actual}")
    print(f"GMAIL_{upper}_CLIENT_ID={creds.client_id}")
    print(f"GMAIL_{upper}_CLIENT_SECRET={creds.client_secret}")
    print(f"GMAIL_{upper}_REFRESH_TOKEN={creds.refresh_token}")
    print(
        f"\nThen make sure GMAIL_ACCOUNTS lists '{key}', add the operator's "
        "address to GMAIL_ALLOWED_EMAILS, and restart the stack."
    )
    return 0


def exit_no_refresh_token() -> int:
    print(
        "\nGoogle returned no refresh_token. Usual causes:\n"
        "  - the OAuth client is not of type 'Desktop app'\n"
        "  - this client+user pair was already authorized and prompt=consent\n"
        "    was suppressed by an SSO/session policy\n"
        "Revoke the app at myaccount.google.com/permissions for that mailbox "
        "and retry.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
