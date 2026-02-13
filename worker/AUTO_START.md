# Auto Start (Desktop, Python)

## Manual Run
1. Open terminal in `worker` folder.
2. Run:
   - `python run_desktop_workers.py`

This starts both:
- `combined_worker.py` (crawler)
- `translate_worker_ollama.py` (Ollama translator)

## Optional Env (Translator)
- `OPTIMIZE_EVERY=10` (or `5`)
- `ERROR_LOG_MAX_MB=20`
- `ERROR_LOG_BACKUP_COUNT=5`
- `OLLAMA_MODEL=gemma3:12b`

## Windows Task Scheduler
1. Open `taskschd.msc`.
2. Create Task.
3. Action:
   - Program: `python`
   - Arguments: `run_desktop_workers.py`
   - Start in: your `worker` folder

## Logs
- Translator error log: `translate_error.log` (error-only, rotating file)
