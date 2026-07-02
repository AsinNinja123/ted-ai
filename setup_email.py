#!/usr/bin/env python3
"""Run this once to connect Ted to your Outlook email."""

import getpass, imaplib, json, os, sys

CONFIG = os.path.expanduser("~/.ted_email_config.json")

print()
print("Ted Email Setup")
print("─" * 44)
print()
print("Before continuing, make sure IMAP is enabled:")
print("  1. Go to outlook.com and sign in")
print("  2. Settings → View all Outlook settings")
print("  3. Mail → Sync email → POP and IMAP")
print("  4. Toggle IMAP on and Save")
print()
print("If you have 2-step verification, you need an App Password:")
print("  account.microsoft.com → Security → Advanced security → App passwords")
print()

email_addr = input("Outlook email address: ").strip()
if not email_addr:
    print("No email entered. Exiting.")
    sys.exit(1)

password = getpass.getpass("Password (hidden while typing): ")
if not password:
    print("No password entered. Exiting.")
    sys.exit(1)

print()
print("Testing connection...", end=" ", flush=True)
try:
    with imaplib.IMAP4_SSL("outlook.office365.com", 993) as imap:
        imap.login(email_addr, password)
        imap.select("INBOX")
        print("Connected!")
except imaplib.IMAP4.error as e:
    print(f"Failed.\n\nError: {e}")
    print()
    print("Common causes:")
    print("  - IMAP not enabled in Outlook.com settings (see step above)")
    print("  - Wrong password — if you use 2FA, use an App Password instead")
    print("  - Typo in email address")
    sys.exit(1)
except Exception as e:
    print(f"Failed.\n\nError: {e}")
    sys.exit(1)

with open(CONFIG, "w") as f:
    json.dump({"email": email_addr, "password": password}, f)
os.chmod(CONFIG, 0o600)

print(f"Credentials saved to {CONFIG}")
print()
print("Done! Restart Ted and say 'check my email'.")
