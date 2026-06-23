import os
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Request this exact scope
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
TOKEN_PATH = "tokens/calendar_token.json"
CREDENTIALS_PATH = "credentials.json"

def main():
    creds = None
    # Load credentials if they exist
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception:
            pass

    # If credentials are not valid (or do not exist), request them
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_PATH, SCOPES
            )
            # Run local server to authenticate (on localhost/127.0.0.1)
            creds = flow.run_local_server(port=0, host="127.0.0.1")

        # Save the credentials for the next run
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        with open(TOKEN_PATH, "w") as token_file:
            token_file.write(creds.to_json())
        print("✅ Calendar token saved to tokens/calendar_token.json")

if __name__ == "__main__":
    main()
