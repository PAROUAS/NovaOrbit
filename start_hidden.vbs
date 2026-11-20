Option Explicit

Dim shell, files, root, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
command = "python.exe " & Chr(34) & files.BuildPath(root, "start_project.py") & Chr(34)
shell.Run command, 0, False
