' Welcome to Hell - completely silent launcher.
' Double-click this instead of run_bot.bat if you don't want the setup
' console window to flash on screen. The control panel opens as usual.
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
shell.Run """" & here & "\run_bot.bat""", 0, False
