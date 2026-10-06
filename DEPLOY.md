- Python 3.10,Git.
- Two separate virtual environments (their dependencies clash):
  - `.venv-rasa`: `pip install -r requirements.txt` + `python -m spacy download en_core_web_md`
  - `.venv-app` (optional Streamlit UI): `pip install -r requirements-frontend.txt`
- `copy .env.example .env` and fill in keys (all optional; without keys the bot
  uses built-in emission factors and the demo hotel data set).

## 1. Local (no Docker)
```powershell
rasa train
rasa run actions                                   # terminal 1 (port 5055)
rasa run --cors "*" -p 5005                       # terminal 2 (REST channel only)
# open frontend/index.html in a browser
