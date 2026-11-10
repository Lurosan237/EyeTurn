# Creates "EyeTurn" shortcuts on the desktop and in the Start menu. They start
# EyeTurn without the black console window. Run again any time to recreate them.
#   powershell -ExecutionPolicy Bypass -File make_shortcut.ps1            desktop + Start menu
#   powershell -ExecutionPolicy Bypass -File make_shortcut.ps1 -Startup   also start EyeTurn with Windows
#   powershell -ExecutionPolicy Bypass -File make_shortcut.ps1 -NoStartup remove that again
param([switch]$Startup, [switch]$NoStartup)

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonw = Join-Path $root '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $pythonw)) { Write-Error "Not found: $pythonw (set up .venv first, see README)"; exit 1 }
$shell = New-Object -ComObject WScript.Shell

function New-EyeTurnShortcut($folder) {
    $lnk = $shell.CreateShortcut((Join-Path $folder 'EyeTurn.lnk'))
    $lnk.TargetPath = $pythonw
    $lnk.Arguments = '-m eyeturn'
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = (Join-Path $root 'eyeturn.ico') + ',0'
    $lnk.Description = 'EyeTurn: hands-free page turning and scrolling'
    $lnk.Save()
    Write-Output "Created $(Join-Path $folder 'EyeTurn.lnk')"
}

New-EyeTurnShortcut ([Environment]::GetFolderPath('Desktop'))
New-EyeTurnShortcut ([Environment]::GetFolderPath('Programs'))
$startupLnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'EyeTurn.lnk'
if ($Startup) { New-EyeTurnShortcut ([Environment]::GetFolderPath('Startup')) }
if ($NoStartup -and (Test-Path $startupLnk)) { Remove-Item $startupLnk; Write-Output "Removed $startupLnk" }
