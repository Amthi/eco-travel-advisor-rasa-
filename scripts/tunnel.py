"""Optional (development only): expose the local Rasa server with Pyngrok so
that a cloud-hosted client can reach it.

    pip install pyngrok
    set NGROK_AUTHTOKEN=<your token>      (PowerShell: $env:NGROK_AUTHTOKEN="...")
    python scripts/tunnel.py

Then open frontend/index.html?rasa=<printed https URL>.
Never leave a tunnel running with real personal data.
"""
import os
import time

from pyngrok import ngrok

token = os.environ.get("NGROK_AUTHTOKEN")
if token:
    ngrok.set_auth_token(token)
tunnel = ngrok.connect(5005, "http")
print("Public URL:", tunnel.public_url)
print("Press Ctrl+C to close the tunnel.")
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    ngrok.kill()
