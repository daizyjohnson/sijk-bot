"""One-time LOCAL Google OAuth authorization for SIJK CHECK.

Run this on your own computer, not on the Discord bot host:
  python scripts/sijk_check_google_auth.py path/to/client_secret.json

The Google Cloud OAuth client must be of type Desktop app.
Keep client_secret.json and generated secrets private; never commit them.
"""
import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPE = "https://www.googleapis.com/auth/spreadsheets"


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/sijk_check_google_auth.py client_secret.json")
    source = Path(sys.argv[1]).expanduser()
    config = json.loads(source.read_text(encoding="utf-8"))
    client = config.get("installed")
    if not client:
        raise SystemExit("Create an OAuth client of type 'Desktop app' in Google Cloud.")
    flow = InstalledAppFlow.from_client_config(config, scopes=[SCOPE])
    flow.redirect_uri = "http://localhost:8765/"
    credentials = flow.run_local_server(
        host="localhost", port=8765, open_browser=True,
        authorization_prompt_message="Open this URL in your browser: {url}",
        access_type="offline", prompt="consent",
    )
    if not credentials.refresh_token:
        raise SystemExit("Google did not return a refresh token. Revoke prior consent and retry.")
    output = source.parent / "sijk_check_oauth_secrets.txt"
    if output.exists():
        raise SystemExit("Secrets file already exists; move it to a secure place first.")
    output.write_text(
        "SIJK_CHECK_GOOGLE_CLIENT_ID=" + client["client_id"] + "\n"
        "SIJK_CHECK_GOOGLE_CLIENT_SECRET=" + client["client_secret"] + "\n"
        "SIJK_CHECK_GOOGLE_REFRESH_TOKEN=" + credentials.refresh_token + "\n",
        encoding="utf-8",
    )
    try:
        output.chmod(0o600)
    except OSError:
        pass
    print("Authorization complete. Copy the 3 values from the LOCAL secrets file to Bothost.")
    print("Do not upload or commit the file; delete local copies when finished.")


if __name__ == "__main__":
    main()
