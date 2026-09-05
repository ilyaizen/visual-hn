# One-shot elevated re-registration of the VHN tasks from the new repo path.
# Wrapper exists because Start-Process -Verb RunAs mangles nested quoting.
$ErrorActionPreference = 'Continue'
Start-Transcript -Path "D:\Projects\visual-hn\scripts\re-register.log" -Force
& "D:\Projects\visual-hn\scripts\register-task.ps1"
Stop-Transcript
