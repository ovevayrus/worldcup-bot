# Local launcher. Use a separate test token. The app refuses an existing webhook
# unless an operator deliberately sets WORLDCUP_ALLOW_WEBHOOK_RESET=true.
$ErrorActionPreference = "Stop"
python "$PSScriptRoot\bot.py"
