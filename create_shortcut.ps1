$ErrorActionPreference = 'Stop'

$launcher = Join-Path $PSScriptRoot 'start.bat'
$electron = Join-Path $PSScriptRoot 'runtime\electron.exe'
$iconDirectory = Join-Path $PSScriptRoot 'branding'
$iconPath = Join-Path $iconDirectory 'nova-orbit.ico'
$shortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'NOVA Orbit.lnk'
$oldShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'NOVA One.lnk'

if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "NOVA Orbit launcher not found: $launcher"
}
if (-not (Test-Path -LiteralPath $electron -PathType Leaf)) {
    throw "NOVA Orbit icon source not found: $electron"
}

Add-Type -AssemblyName System.Drawing
New-Item -ItemType Directory -Path $iconDirectory -Force | Out-Null
$bitmap = New-Object System.Drawing.Bitmap 64, 64
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.Clear([System.Drawing.Color]::FromArgb(9, 17, 27))
$cyan = [System.Drawing.Color]::FromArgb(108, 224, 239)
$mint = [System.Drawing.Color]::FromArgb(130, 247, 212)
$ring = New-Object System.Drawing.Pen($cyan, 2.2)
$orbit = New-Object System.Drawing.Pen([System.Drawing.Color]::FromArgb(170, $mint), 2.4)
$core = New-Object System.Drawing.SolidBrush($mint)
$glow = New-Object System.Drawing.SolidBrush([System.Drawing.Color]::FromArgb(45, $cyan))
try {
    $fill = New-Object System.Drawing.Drawing2D.LinearGradientBrush(
        (New-Object System.Drawing.Rectangle 0, 0, 64, 64),
        [System.Drawing.Color]::FromArgb(18, 49, 68),
        [System.Drawing.Color]::FromArgb(16, 31, 45),
        45
    )
    try { $graphics.FillEllipse($fill, 8, 8, 48, 48) } finally { $fill.Dispose() }
    $graphics.DrawEllipse($ring, 10, 10, 44, 44)
    $graphics.TranslateTransform(32, 32)
    $graphics.RotateTransform(-32)
    $graphics.DrawEllipse($orbit, -23, -9, 46, 18)
    $graphics.ResetTransform()
    $graphics.FillEllipse($glow, 22, 22, 20, 20)
    $graphics.FillEllipse($core, 27, 27, 10, 10)

    $png = New-Object System.IO.MemoryStream
    try {
        $bitmap.Save($png, [System.Drawing.Imaging.ImageFormat]::Png)
        $pngBytes = $png.ToArray()
    } finally { $png.Dispose() }

    $stream = [System.IO.File]::Create($iconPath)
    try {
        $writer = New-Object System.IO.BinaryWriter($stream)
        $writer.Write([UInt16]0)
        $writer.Write([UInt16]1)
        $writer.Write([UInt16]1)
        $writer.Write([Byte]64)
        $writer.Write([Byte]64)
        $writer.Write([Byte]0)
        $writer.Write([Byte]0)
        $writer.Write([UInt16]1)
        $writer.Write([UInt16]32)
        $writer.Write([UInt32]$pngBytes.Length)
        $writer.Write([UInt32]22)
        $writer.Write($pngBytes)
        $writer.Flush()
    } finally {
        if ($writer) { $writer.Dispose() } else { $stream.Dispose() }
    }
} finally {
    $graphics.Dispose()
    $bitmap.Dispose()
    $ring.Dispose()
    $orbit.Dispose()
    $core.Dispose()
    $glow.Dispose()
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'NOVA Orbit - Find your signal. Keep your space.'
$shortcut.IconLocation = "$iconPath,0"
$shortcut.Save()

if (Test-Path -LiteralPath $oldShortcutPath) {
    Remove-Item -LiteralPath $oldShortcutPath -Force
}
Write-Output "Created NOVA Orbit shortcut: $shortcutPath"
