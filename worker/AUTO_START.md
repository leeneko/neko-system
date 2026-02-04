# Auto Start (Windows Task Scheduler)

## Build
1. Open CMD in the `worker` folder.
2. Run `build.bat`.
3. Use the generated `dist\RabbitWorker.exe`.

## Auto Start (Recommended: Task Scheduler)
1. Press `Win + R`, type `taskschd.msc`, press Enter.
2. Click `Create Task...`.
3. **General** tab:
   - Name: `RabbitWorker`
   - Check `Run whether user is logged on or not`
   - Check `Run with highest privileges`
4. **Triggers** tab:
   - Click `New...`
   - Begin the task: `At startup`
   - OK
5. **Actions** tab:
   - Click `New...`
   - Action: `Start a program`
   - Program/script: path to `RabbitWorker.exe`
   - Start in: folder that contains `RabbitWorker.exe`
6. **Conditions** tab:
   - Optional: uncheck `Start the task only if the computer is on AC power`
7. Click **OK** and enter your Windows credentials if prompted.

## Error Log
Errors are written to `worker_error.log` in the same folder as the EXE.
