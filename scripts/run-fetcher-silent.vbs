' Visual-HN Residential Fetcher — silent VBS launcher
' Runs the PowerShell script without flashing a console window.
' Called by Task Scheduler instead of powershell.exe directly.

Dim shell, fso, scriptDir, launcher
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = scriptDir & "\start-fetcher.ps1"

shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & launcher & """", 0, False
