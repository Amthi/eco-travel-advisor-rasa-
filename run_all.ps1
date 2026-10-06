# Windows PowerShell: train + produce ALL real outputs for the report.
# Run from the project folder with the venv active:   .\run_all.ps1
$ErrorActionPreference = "Continue"
New-Item -ItemType Directory -Force results | Out-Null

Write-Host "== 1/6 Validate data ==" 
rasa data validate 2>&1 | Tee-Object results\01_validate.txt

Write-Host "== 2/6 Train =="
rasa train 2>&1 | Tee-Object results\02_train.txt

Write-Host "== 3/6 NLU 5-fold cross-validation =="
rasa test nlu --nlu data/nlu.yml --cross-validation --folds 5 --out results/nlu_cv 2>&1 | Tee-Object results\03_nlu_cv.txt

Write-Host "== 4/6 NLU 80/20 split =="
rasa data split nlu --training-fraction 0.8 --out results/split
rasa train nlu --nlu results/split/training_data.yml --out results/nlu_model
rasa test nlu --nlu results/split/test_data.yml --model results/nlu_model --out results/nlu_split 2>&1 | Tee-Object results\04_nlu_split.txt

Write-Host "== 5/6 Dialogue tests =="
rasa test core --stories tests/test_stories.yml --out results/core 2>&1 | Tee-Object results\05_core.txt

Write-Host "== 6/6 Unit tests =="
python -m pytest tests/test_actions.py -v 2>&1 | Tee-Object results\06_pytest.txt

Write-Host "Optional 7/7: with Rasa + actions running, python scripts\measure_latency.py --runs 10 | Tee-Object results\07_latency.txt"
Write-Host "Done. Outputs and confusion matrices are in the results folder."
