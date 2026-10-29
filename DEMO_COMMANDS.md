# Demo Commands

## Launch Interactive Visual Demo Interface (All 4 Members / All Demos)

```powershell
.\run_demo_ui.bat
```
*(Or run `.\make.bat ui`, or open `demo\index.html` directly in your browser)*

## Demo 1: Run Full Automated Test Suite (123 Tests)

```powershell

.\make.bat


## Demo 2: Run the 3 Live Correctness Proofs (Idempotency, Saga Crash Recovery, Concurrency)
```powershell

.\make.bat proofs


## Demo 3: Run Tamper-Evident Ledger & Append-Only Security Test
```powershell

.\make.bat test -k "append_only or hash_chain"


## Demo 4: Run AWS Cloud Cost Optimization Model
```powershell

.\.venv\Scripts\python.exe scripts\cost_model.py

