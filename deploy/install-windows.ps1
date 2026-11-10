# הרצת השירות על מחשב Windows שדולק תמיד (במקום שרת): עולה אוטומטית בכניסה ל-Windows, ברקע בלי חלון.
# הרצה:  powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1
# עצירה: Stop-ScheduledTask "IG Tracker"   הסרה: Unregister-ScheduledTask "IG Tracker" -Confirm:$false
$dir = Split-Path $PSScriptRoot -Parent
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\pythonw.exe" `
    -Argument "`"$dir\bot.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# בלי הגבלת זמן, וחוזר לרוץ לבד אם קרס
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "IG Tracker" -Action $action -Trigger $trigger `
    -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName "IG Tracker"
Write-Host "IG Tracker is running in the background. Log: $dir\tracker.log"
