# =============================================================================
#  Emergency-Gamma.ps1 - manual gamma/levels for Tarkov when Python is broken.
#
#  Same idea as the app (lift shadows, do not turn the picture into grey milk),
#  but by hand and without any screen capture: no Python, no pip, nothing to
#  install - PowerShell is already in Windows 11.
#
#  Usage (normal PowerShell, no admin needed):
#      powershell -ExecutionPolicy Bypass -File Emergency-Gamma.ps1 -Level 45
#      powershell -ExecutionPolicy Bypass -File Emergency-Gamma.ps1 -Restore
#
#  -Level    0..100 how much to brighten the shadows       (default 40)
#  -Shadow   0..100 extra black-level lift, anti-grey       (default 25)
#  -Red/-Green/-Blue  channel trim in percent, 100 = neutral (fix a blue cast)
#  -Restore  put the factory ramp back
#
#  Uses the same system-wide gamma ramp as the app, so it is safe for Easy
#  Anti-Cheat (no injection, no overlay, no FPS cost). If Auto HDR or Night light
#  is on, Windows may ignore or override the ramp - turn them off.
#
#  Layout note (this is what makes SetDeviceGammaRamp refuse): the array must be
#  WORD Ramp[3][256] - 256 RED entries, then 256 GREEN, then 256 BLUE, each
#  block monotonically non-decreasing, values 0..65535. Interleaving r,g,b works
#  by accident on a neutral ramp and is refused on a tinted one.
# =============================================================================
param(
    [int]    $Level   = 40,
    [int]    $Shadow  = 25,
    [int]    $Red     = 100,
    [int]    $Green   = 100,
    [int]    $Blue    = 100,
    [switch] $Restore
)

$ErrorActionPreference = 'Stop'

if ($Level  -lt 0)   { $Level  = 0 }
if ($Level  -gt 100) { $Level  = 100 }
if ($Shadow -lt 0)   { $Shadow = 0 }
if ($Shadow -gt 100) { $Shadow = 100 }

Add-Type -Namespace TarkovBright -Name Gamma -MemberDefinition @'
[DllImport("gdi32.dll", SetLastError = true)] public static extern bool SetDeviceGammaRamp(IntPtr hdc, ushort[] ramp);
[DllImport("user32.dll", SetLastError = true)] public static extern IntPtr GetDC(IntPtr hWnd);
[DllImport("gdi32.dll",  SetLastError = true)] public static extern IntPtr CreateDC(string driver, string device, IntPtr output, IntPtr devMode);
[DllImport("user32.dll")]                        public static extern int  ReleaseDC(IntPtr hWnd, IntPtr hdc);
[DllImport("gdi32.dll")]                          public static extern int  DeleteDC(IntPtr hdc);
[DllImport("user32.dll")]                         public static extern int  GetSystemMetrics(int index);
'@

function Get-LastWin32Error {
    [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
}

function Test-Ramp($ramp, [int]$per) {
    # ровно то, за что драйвер отвечает ERROR_INVALID_PARAMETER
    for ($c = 0; $c -lt 3; $c++) {
        $prev = -1
        for ($i = 0; $i -lt $per; $i++) {
            $v = [int]$ramp[$c * $per + $i]
            if ($v -lt $prev -or $v -lt 0 -or $v -gt 65535) { return $false }
            $prev = $v
        }
    }
    return $true
}

# --- DC: DC of the virtual screen, иначе - DC устройства вывода --------------
$hdc   = [TarkovBright.Gamma]::GetDC([IntPtr]::Zero)
$dcSrc = 'GetDC(весь экран)'
if ($hdc -eq [IntPtr]::Zero) {
    $hdc   = [TarkovBright.Gamma]::CreateDC('DISPLAY', $null, [IntPtr]::Zero, [IntPtr]::Zero)
    $dcSrc = 'CreateDC(DISPLAY)'
}
if ($hdc -eq [IntPtr]::Zero) {
    Write-Output 'не удалось получить DC экрана - нет активного сеанса с монитором?'
    exit 1
}
$ownDc = ($dcSrc -eq 'CreateDC(DISPLAY)')      # CreateDC надо закрывать самим

try {
    # ВАЖНО: New-Object 'ushort[]' не работает — PowerShell не резолвит алиас типа
    # в составном имени (ошибка TypeNotFound). Нужно полное имя типа.
    $ramp = New-Object 'System.UInt16[]' 768

    if ($Restore) {
        for ($i = 0; $i -lt 256; $i++) {
            $v    = [uint16]($i * 257)          # 0..65535 ровно, без ступенек
            $ramp[$i] = $v; $ramp[256 + $i] = $v; $ramp[512 + $i] = $v
        }
        $ok = [TarkovBright.Gamma]::SetDeviceGammaRamp($hdc, $ramp)
        if ($ok) { 'factory gamma ramp restored' }
        else     { 'SetDeviceGammaRamp отказал (код Windows {0})' -f (Get-LastWin32Error) }
        exit 0
    }

    # --- та же цепочка формул, что в app/correction.py ------------------------
    $gamma = 1.0 + ($Level / 100.0) * 1.60            # 1.00 .. 2.60
    $lift  = ($Shadow / 100.0) * 0.020                # максимум +2% на чистом чёрном
    $g0 = [math]::Max(0.2, $Red   / 100.0)
    $g1 = [math]::Max(0.2, $Green / 100.0)
    $g2 = [math]::Max(0.2, $Blue  / 100.0)
    $geo = [math]::Pow($g0 * $g1 * $g2, 1.0 / 3.0)    # геометр. среднее = 1: светлее, но не бледнее
    if ($geo -lt 0.2) { $geo = 0.2 }
    $gains = @(($g0 / $geo), ($g1 / $geo), ($g2 / $geo))

    for ($c = 0; $c -lt 3; $c++) {
        $f = $gamma * [math]::Pow($gains[$c], 1.30)
        if ($f -lt 0.2) { $f = 0.2 }
        for ($i = 0; $i -lt 256; $i++) {
            $x   = $i / 255.0
            $lin = [math]::Pow($x, 2.2)                       # в линейный свет
            if ($lin -gt 1.0) { $lin = 1.0 }
            $y = [math]::Pow($lin, 1.0 / 2.2)                  # обратно в sRGB
            $y = $y + $lift * [math]::Pow(1.0 - $y, 2)         # подъём чёрных
            if ($y -gt 1.0) { $y = 1.0 }
            $y = [math]::Pow($y, 1.0 / $f)                     # гамма + трим канала
            $v = [int][math]::Round($y * 65535.0)
            if ($v -lt 0)     { $v = 0 }
            if ($v -gt 65535) { $v = 65535 }
            $ramp[$c * 256 + $i] = [uint16]$v
        }
    }

    if (-not (Test-Ramp $ramp 256)) {
        Write-Output 'таблица немонотонна - уменьшите -Shadow или тримы каналов; ничего не меняем'
        exit 1
    }

    $ok = [TarkovBright.Gamma]::SetDeviceGammaRamp($hdc, $ramp)
    $err = 0
    if (-not $ok) { $err = Get-LastWin32Error }

    # Some drivers (and 10-bit output) want the extended 3x1024 table only.
    if (-not $ok) {
        $big = New-Object 'System.UInt16[]' 3072
        for ($c = 0; $c -lt 3; $c++) {
            for ($i = 0; $i -lt 1024; $i++) {
                $pos  = $i * 255.0 / 1023.0
                $lo   = [int][math]::Floor($pos)
                $hi   = [math]::Min($lo + 1, 255)
                $frac = $pos - $lo
                $v = [int][math]::Round($ramp[$c * 256 + $lo] * (1.0 - $frac) + $ramp[$c * 256 + $hi] * $frac)
                if ($v -gt 65535) { $v = 65535 }
                $big[$c * 1024 + $i] = [uint16]$v
            }
        }
        if (Test-Ramp $big 1024) {
            $ok = [TarkovBright.Gamma]::SetDeviceGammaRamp($hdc, $big)
            if (-not $ok) { $err = Get-LastWin32Error }
        }
    }

    if (-not $ok) {
        $hint = 'Отказ SetDeviceGammaRamp (код Windows {0}).' -f $err
        if (([TarkovBright.Gamma]::GetSystemMetrics(78)) -ne 0) {
            $hint += ' Судя по всему это удалённый сеанс (RDP) - там программная гамма недоступна.'
        }
        $hint += ' Выключите Auto HDR / HDR и «Ночной свет» (f.lux), обновите драйвер GPU.'
        Write-Output $hint
        exit 1
    }

    'gamma applied: level {0}/100 (gamma {1:N2}), shadow lift {2}/100, trims R{3}% G{4}% B{5}%  [DC: {6}]' -f `
        $Level, $gamma, $Shadow, $Red, $Green, $Blue, $dcSrc
    'To undo:  powershell -ExecutionPolicy Bypass -File Emergency-Gamma.ps1 -Restore'
}
finally {
    if ($ownDc) { [void][TarkovBright.Gamma]::DeleteDC($hdc) }
    else        { [void][TarkovBright.Gamma]::ReleaseDC([IntPtr]::Zero, $hdc) }
}
