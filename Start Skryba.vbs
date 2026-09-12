' Double-click launcher: runs run.ps1 with no visible console window.
' Resolves its own folder so this works wherever the repo is cloned.
scriptDir = Left(WScript.ScriptFullName, Len(WScript.ScriptFullName) - Len(WScript.ScriptName))
Set objShell = CreateObject("WScript.Shell")
objShell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptDir & "run.ps1""", 0, False
