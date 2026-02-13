# Worker Setup (Current)

## Active Programs
- `combined_worker.py`: crawl/extract/upload original chapter text
- `translate_worker_ollama.py`: translate with local Ollama and save to DB
- `run_desktop_workers.py`: run both workers together

## Run
```bash
cd /home/ubuntu/workspace/rabbit-system/worker
python run_desktop_workers.py
```

## Translator Tuning
- `OPTIMIZE_EVERY`: periodic optimization interval (default `10`, recommend `5` or `10`)
- `ERROR_LOG_MAX_MB`: max log size before rotation (default `20`)
- `ERROR_LOG_BACKUP_COUNT`: number of rotated backups (default `5`)
- `OLLAMA_MODEL`: model name (default `gemma3:12b`)

## Log Policy
- `translate_error.log`: errors only (`ERROR` level)
- rotation enabled to prevent unlimited file growth
