python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m spacy download en_core_web_md     
copy .env.example .env                      
rasa train
# terminal 1
rasa run actions
# terminal 2 (chat in the terminal)
rasa shell
```

Web UI: terminal 2 -> `rasa run --cors "*" -p 5005` , then open
`frontend/index.html` in a browser (REST channel, no extra install).
Optional Streamlit UI: separate venv, `pip install -r requirements-frontend.txt`,
`streamlit run streamlit_app.py`.


## Generate every output for the report
```powershell
.\run_all.ps1
```
## Tests
`python -m pytest tests/test_actions.py -v`.
`rasa test core --stories tests/test_stories.yml`  and
`rasa test nlu --cross-validation` 
run `run_all.ps1` to produce every result file.
