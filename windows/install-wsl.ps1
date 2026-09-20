#requires -Version 5.1
#requires -RunAsAdministrator

[CmdletBinding()]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$Distribution = "Ubuntu",

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$LinuxUser = "Chris",

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$LinuxPassword = "1",

    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$WindowsDocumentsPath = "$env:SystemDrive\Users\$env:USERNAME\Documents",

    [Parameter()]
    [switch]$WebDownload
)

$ErrorActionPreference = "Stop"

function Write-Status {
    param(
        [Parameter(Mandatory)]
        [ValidateSet("INFO", "OK", "WARN", "FAIL")]
        [string]$Level,

        [Parameter(Mandatory)]
        [string]$Message
    )

    $prefix = switch ($Level) {
        "INFO" { "[INFO]" }
        "OK"   { "[ OK ]" }
        "WARN" { "[WARN]" }
        "FAIL" { "[FAIL]" }
    }

    Write-Host "$prefix $Message"
}

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory)]
        [string]$FilePath,

        [Parameter()]
        [string[]]$ArgumentList = @(),

        [Parameter()]
        [int[]]$SuccessExitCodes = @(0)
    )

    & $FilePath @ArgumentList
    $exitCode = $LASTEXITCODE

    if ($SuccessExitCodes -notcontains $exitCode) {
        throw "Polecenie '$FilePath $($ArgumentList -join ' ')' zakonczylo sie kodem $exitCode."
    }

    return $exitCode
}

function ConvertTo-WslPath {
    param(
        [Parameter(Mandatory)]
        [string]$WindowsPath
    )

    $fullPath = [System.IO.Path]::GetFullPath($WindowsPath)

    if ($fullPath -notmatch '^[A-Za-z]:\\') {
        throw "Sciezka '$fullPath' nie jest lokalna sciezka dysku Windows."
    }

    $drive = $fullPath.Substring(0, 1).ToLowerInvariant()
    $rest = $fullPath.Substring(2).Replace("\", "/")

    return "/mnt/$drive$rest"
}

function ConvertTo-BashSingleQuoted {
    param(
        [Parameter(Mandatory)]
        [string]$Value
    )

    return "'" + $Value.Replace("'", "'\''") + "'"
}

Write-Host ""
Write-Host "============================================"
Write-Host " Instalator WSL 2 + Ubuntu"
Write-Host "============================================"
Write-Host ""

$restartRequired = $false
$windowsDocumentsFullPath = [System.IO.Path]::GetFullPath($WindowsDocumentsPath)
$wslHomePath = ConvertTo-WslPath -WindowsPath $windowsDocumentsFullPath

try {
    Write-Host "[1/7] Sprawdzanie systemu Windows..."

    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $build = [int]$os.BuildNumber

    Write-Status INFO "System: $($os.Caption), build $build"

    if ($build -lt 19041) {
        throw "WSL wymaga Windows 10 2004 (build 19041) lub nowszego albo Windows 11."
    }

    if (-not [Environment]::Is64BitOperatingSystem) {
        throw "WSL 2 wymaga 64-bitowego systemu Windows."
    }

    Write-Status OK "System spelnia minimalne wymagania."

    Write-Host ""
    Write-Host "[2/7] Sprawdzanie katalogu Dokumenty..."

    if (-not (Test-Path -LiteralPath $windowsDocumentsFullPath)) {
        New-Item -ItemType Directory -Path $windowsDocumentsFullPath -Force | Out-Null
        Write-Status INFO "Utworzono: $windowsDocumentsFullPath"
    }

    Write-Status INFO "Windows: $windowsDocumentsFullPath"
    Write-Status INFO "WSL HOME: $wslHomePath"
    Write-Status OK "Katalog domowy jest gotowy."

    Write-Host ""
    Write-Host "[3/7] Sprawdzanie wirtualizacji..."

    try {
        $computerInfo = Get-ComputerInfo -Property HyperVisorPresent, HyperVRequirementVirtualizationFirmwareEnabled -ErrorAction Stop

        if ($computerInfo.HyperVRequirementVirtualizationFirmwareEnabled -eq $false -and -not $computerInfo.HyperVisorPresent) {
            Write-Status WARN "Wirtualizacja sprzetowa moze byc wylaczona w BIOS/UEFI."
        }
        else {
            Write-Status OK "Wirtualizacja sprzetowa wyglada na dostepna."
        }
    }
    catch {
        Write-Status WARN "Nie udalo sie jednoznacznie sprawdzic wirtualizacji: $($_.Exception.Message)"
    }

    Write-Host ""
    Write-Host "[4/7] Wlaczanie funkcji Windows wymaganych przez WSL 2..."

    $wslFeatureCode = Invoke-NativeCommand -FilePath "dism.exe" -ArgumentList @(
        "/online",
        "/enable-feature",
        "/featurename:Microsoft-Windows-Subsystem-Linux",
        "/all",
        "/norestart"
    ) -SuccessExitCodes @(0, 3010)

    if ($wslFeatureCode -eq 3010) {
        $restartRequired = $true
    }

    Write-Status OK "Windows Subsystem for Linux jest wlaczony."

    $vmPlatformCode = Invoke-NativeCommand -FilePath "dism.exe" -ArgumentList @(
        "/online",
        "/enable-feature",
        "/featurename:VirtualMachinePlatform",
        "/all",
        "/norestart"
    ) -SuccessExitCodes @(0, 3010)

    if ($vmPlatformCode -eq 3010) {
        $restartRequired = $true
    }

    Write-Status OK "Virtual Machine Platform jest wlaczony."

    Write-Host ""
    Write-Host "[5/7] Instalacja i konfiguracja WSL 2..."

    if (-not (Get-Command "wsl.exe" -ErrorAction SilentlyContinue)) {
        throw "Nie znaleziono wsl.exe. Zaktualizuj Windows i uruchom skrypt ponownie."
    }

    try {
        Invoke-NativeCommand -FilePath "wsl.exe" -ArgumentList @("--set-default-version", "2") | Out-Null
        Write-Status OK "WSL 2 ustawiony jako domyslna wersja."
    }
    catch {
        if ($restartRequired) {
            Write-Status WARN "Pelna konfiguracja WSL 2 moze wymagac restartu."
        }
        else {
            throw
        }
    }

    $installedDistros = @(& wsl.exe --list --quiet 2>$null) |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ }

    if ($installedDistros -notcontains $Distribution) {
        Write-Status INFO "Instalowanie dystrybucji '$Distribution'..."

        $installArgs = @("--install", "--distribution", $Distribution, "--no-launch")

        if ($WebDownload) {
            $installArgs += "--web-download"
        }

        try {
            Invoke-NativeCommand -FilePath "wsl.exe" -ArgumentList $installArgs -SuccessExitCodes @(0, 3010) | Out-Null
        }
        catch {
            if ($restartRequired) {
                Write-Status WARN "Instalacja dystrybucji nie moze zostac zakonczona przed restartem."
                Write-Status INFO "Po restarcie uruchom ponownie ten sam skrypt."
                exit 3010
            }

            throw
        }

        $installedDistros = @(& wsl.exe --list --quiet 2>$null) |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ }
    }
    else {
        Write-Status OK "Dystrybucja '$Distribution' jest juz zainstalowana."
    }

    if ($installedDistros -notcontains $Distribution) {
        Write-Status WARN "Dystrybucja '$Distribution' zostala przygotowana, ale wymaga restartu Windows."
        Write-Status INFO "Po restarcie uruchom ponownie ten sam skrypt."
        exit 3010
    }

    Invoke-NativeCommand -FilePath "wsl.exe" -ArgumentList @("--set-default", $Distribution) | Out-Null
    Write-Status OK "'$Distribution' ustawiono jako domyslna dystrybucje."

    Write-Host ""
    Write-Host "[6/7] Tworzenie konta Linux '$LinuxUser'..."

    $quotedUser = ConvertTo-BashSingleQuoted -Value $LinuxUser
    $quotedPassword = ConvertTo-BashSingleQuoted -Value $LinuxPassword
    $quotedHome = ConvertTo-BashSingleQuoted -Value $wslHomePath

    $linuxSetupTemplate = @'
set -e

user=__USER__
password=__PASSWORD__
home_dir=__HOME__

if [ ! -d "$home_dir" ]; then
    echo "Katalog HOME nie jest widoczny w WSL: $home_dir" >&2
    exit 20
fi

if id -u "$user" >/dev/null 2>&1; then
    usermod -d "$home_dir" -s /bin/bash "$user"
else
    if useradd --help 2>&1 | grep -q -- '--badname'; then
        useradd --badname -M -d "$home_dir" -s /bin/bash "$user"
    else
        useradd -M -d "$home_dir" -s /bin/bash "$user"
    fi
fi

printf '%s:%s\n' "$user" "$password" | chpasswd

if getent group sudo >/dev/null 2>&1; then
    usermod -aG sudo "$user"
elif getent group wheel >/dev/null 2>&1; then
    usermod -aG wheel "$user"
fi

conf=/etc/wsl.conf
tmp=$(mktemp)

if [ -f "$conf" ]; then
    awk -v user="$user" '
        BEGIN {
            in_user = 0
            saw_user = 0
            wrote_default = 0
        }
        /^\[user\][[:space:]]*$/ {
            if (in_user && !wrote_default) {
                print "default=" user
            }
            in_user = 1
            saw_user = 1
            wrote_default = 0
            print
            next
        }
        /^\[[^]]+\][[:space:]]*$/ {
            if (in_user && !wrote_default) {
                print "default=" user
                wrote_default = 1
            }
            in_user = 0
            print
            next
        }
        {
            if (in_user && $0 ~ /^[[:space:]]*default[[:space:]]*=/) {
                if (!wrote_default) {
                    print "default=" user
                    wrote_default = 1
                }
                next
            }
            print
        }
        END {
            if (in_user && !wrote_default) {
                print "default=" user
            }
            if (!saw_user) {
                print ""
                print "[user]"
                print "default=" user
            }
        }
    ' "$conf" > "$tmp"
else
    printf '[user]\ndefault=%s\n' "$user" > "$tmp"
fi

cat "$tmp" > "$conf"
rm -f "$tmp"

echo "USER=$user"
echo "HOME=$home_dir"
'@

    $linuxSetup = $linuxSetupTemplate.
        Replace("__USER__", $quotedUser).
        Replace("__PASSWORD__", $quotedPassword).
        Replace("__HOME__", $quotedHome)

    Invoke-NativeCommand -FilePath "wsl.exe" -ArgumentList @(
        "--distribution", $Distribution,
        "--user", "root",
        "--",
        "bash", "-lc", $linuxSetup
    ) | Out-Null

    Write-Status OK "Konto '$LinuxUser' zostalo utworzone lub zaktualizowane."
    Write-Status INFO "Haslo konta '$LinuxUser': $LinuxPassword"
    Write-Status INFO "HOME: $wslHomePath"

    Write-Host ""
    Write-Host "[7/7] Weryfikacja konfiguracji..."

    & wsl.exe --terminate $Distribution 2>$null | Out-Null
    Start-Sleep -Seconds 1

    $whoAmI = (& wsl.exe --distribution $Distribution -- bash -lc "whoami" 2>$null | Select-Object -First 1).Trim()
    $reportedHome = (& wsl.exe --distribution $Distribution -- bash -lc 'printf "%s" "$HOME"' 2>$null | Select-Object -First 1).Trim()

    if ($whoAmI -ne $LinuxUser) {
        throw "Domyslny uzytkownik WSL to '$whoAmI', oczekiwano '$LinuxUser'."
    }

    if ($reportedHome -ne $wslHomePath) {
        throw "HOME ma wartosc '$reportedHome', oczekiwano '$wslHomePath'."
    }

    Write-Status OK "Domyslny uzytkownik: $whoAmI"
    Write-Status OK "HOME: $reportedHome"

    Write-Host ""
    Write-Host "============================================"
    Write-Host " Instalacja zakonczona"
    Write-Host "============================================"
    Write-Host ""
    Write-Status INFO "Uruchom WSL poleceniem: wsl"
    Write-Status INFO "Sprawdz dystrybucje: wsl -l -v"

    if ($restartRequired) {
        Write-Status WARN "Windows zglosil, ze czesc zmian systemowych wymaga restartu."
    }

    Write-Status WARN "Domyslne haslo '1' jest bardzo slabe. Zmien je poleceniem 'passwd' po pierwszym uruchomieniu, jesli srodowisko nie jest wyłącznie testowe."
}
catch {
    Write-Host ""
    Write-Status FAIL $_.Exception.Message
    exit 1
}
