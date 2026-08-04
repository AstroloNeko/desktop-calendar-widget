Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
appPath = fso.BuildPath(fso.GetParentFolderName(WScript.ScriptFullName), "app.py")
pywPath = fso.BuildPath(shell.ExpandEnvironmentStrings("%SystemRoot%"), "pyw.exe")
shell.Run """" & pywPath & """ -3 """ & appPath & """", 0, False
