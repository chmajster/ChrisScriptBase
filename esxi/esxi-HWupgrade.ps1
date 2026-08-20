$vCenter = "vcenter.example.local"
$VCUser     = "administrator@vsphere.local"
$VCPassword = "Password123!"

# Docelowa wersja Virtual Hardware
$TargetHardwareVersion = "vmx-21"

# Lista hostów ESXi
$ESXiHosts = @(
    "esxi01.example.local",
    "esxi02.example.local",
    "esxi03.example.local"
)

# Log
$LogFile = ".\esxi-HWupgrade-$(Get-Date -Format 'yyyyMMdd-HHmmss').csv"


# ============================================================
# POWERCLI
# ============================================================

Import-Module VMware.PowerCLI -ErrorAction Stop

# Opcjonalnie dla środowisk z self-signed certificate
Set-PowerCLIConfiguration `
    -InvalidCertificateAction Ignore `
    -Confirm:$false | Out-Null


# ============================================================
# FUNCTIONS
# ============================================================

function Get-HardwareVersionNumber {
    param(
        [string]$HardwareVersion
    )

    if ($HardwareVersion -match "vmx-(\d+)") {
        return [int]$Matches[1]
    }

    return 0
}


# ============================================================
# CONNECT VCENTER
# ============================================================

Write-Host ""
Write-Host "============================================================"
Write-Host " ESXi VM Hardware Upgrade"
Write-Host "============================================================"
Write-Host ""

Write-Host "[INFO] Connecting to vCenter: $vCenter"

try {

    Connect-VIServer `
        -Server $vCenter `
        -User $VCUser `
        -Password $VCPassword `
        -ErrorAction Stop | Out-Null

    Write-Host "[OK] Connected to vCenter."

}
catch {

    Write-Host "[ERROR] Cannot connect to vCenter."
    Write-Host $_.Exception.Message
    exit 1

}


# ============================================================
# RESULTS
# ============================================================

$Results = @()

$TargetVersionNumber = Get-HardwareVersionNumber `
    -HardwareVersion $TargetHardwareVersion


# ============================================================
# HOST LOOP
# ============================================================

foreach ($ESXiName in $ESXiHosts) {

    Write-Host ""
    Write-Host "------------------------------------------------------------"
    Write-Host "Host: $ESXiName"
    Write-Host "------------------------------------------------------------"

    try {

        $VMHost = Get-VMHost `
            -Name $ESXiName `
            -ErrorAction Stop

    }
    catch {

        Write-Host "[ERROR] Host not found: $ESXiName"

        $Results += [PSCustomObject]@{
            Timestamp          = Get-Date
            ESXiHost           = $ESXiName
            VM                 = ""
            PowerState         = ""
            OldHardwareVersion = ""
            NewHardwareVersion = ""
            Status             = "HOST_NOT_FOUND"
            Message            = $_.Exception.Message
        }

        continue
    }


    # --------------------------------------------------------
    # Host information
    # --------------------------------------------------------

    Write-Host "[INFO] ESXi version: $($VMHost.Version)"
    Write-Host "[INFO] Connection:   $($VMHost.ConnectionState)"
    Write-Host "[INFO] Power state:  $($VMHost.PowerState)"


    # --------------------------------------------------------
    # Get VMs
    # --------------------------------------------------------

    $VMs = Get-VM -Location $VMHost |
        Sort-Object Name


    if (-not $VMs) {

        Write-Host "[INFO] No VMs found on host."
        continue
    }


    Write-Host "[INFO] Found $($VMs.Count) VM(s)."


    # ========================================================
    # VM LOOP
    # ========================================================

    foreach ($VM in $VMs) {

        Write-Host ""
        Write-Host "VM: $($VM.Name)"

        $CurrentHW = $VM.HardwareVersion

        $CurrentVersionNumber = Get-HardwareVersionNumber `
            -HardwareVersion $CurrentHW


        Write-Host "    Power State : $($VM.PowerState)"
        Write-Host "    Current HW  : $CurrentHW"
        Write-Host "    Target HW   : $TargetHardwareVersion"


        # ----------------------------------------------------
        # Already upgraded
        # ----------------------------------------------------

        if ($CurrentVersionNumber -ge $TargetVersionNumber) {

            Write-Host "    [SKIP] VM already uses target/newer HW."

            $Results += [PSCustomObject]@{
                Timestamp          = Get-Date
                ESXiHost           = $ESXiName
                VM                 = $VM.Name
                PowerState         = $VM.PowerState
                OldHardwareVersion = $CurrentHW
                NewHardwareVersion = $CurrentHW
                Status             = "SKIPPED"
                Message            = "Already target/newer version"
            }

            continue
        }


        # ----------------------------------------------------
        # Powered-on VM
        # ----------------------------------------------------

        if ($VM.PowerState -ne "PoweredOff") {

            Write-Host "    [SKIP] VM must be PoweredOff."

            $Results += [PSCustomObject]@{
                Timestamp          = Get-Date
                ESXiHost           = $ESXiName
                VM                 = $VM.Name
                PowerState         = $VM.PowerState
                OldHardwareVersion = $CurrentHW
                NewHardwareVersion = $CurrentHW
                Status             = "SKIPPED"
                Message            = "VM is not powered off"
            }

            continue
        }


        # ----------------------------------------------------
        # Hardware upgrade
        # ----------------------------------------------------

        Write-Host "    [UPGRADE] $CurrentHW -> $TargetHardwareVersion"

        try {

            Set-VM `
                -VM $VM `
                -HardwareVersion $TargetHardwareVersion `
                -Confirm:$false `
                -ErrorAction Stop | Out-Null


            # Refresh VM object
            $UpdatedVM = Get-VM -Id $VM.Id

            Write-Host "    [OK] Upgrade completed."
            Write-Host "    New HW: $($UpdatedVM.HardwareVersion)"


            $Results += [PSCustomObject]@{
                Timestamp          = Get-Date
                ESXiHost           = $ESXiName
                VM                 = $VM.Name
                PowerState         = $VM.PowerState
                OldHardwareVersion = $CurrentHW
                NewHardwareVersion = $UpdatedVM.HardwareVersion
                Status             = "SUCCESS"
                Message            = "Hardware upgrade completed"
            }

        }
        catch {

            Write-Host "    [ERROR] Hardware upgrade failed."
            Write-Host "    $($_.Exception.Message)"


            $Results += [PSCustomObject]@{
                Timestamp          = Get-Date
                ESXiHost           = $ESXiName
                VM                 = $VM.Name
                PowerState         = $VM.PowerState
                OldHardwareVersion = $CurrentHW
                NewHardwareVersion = $CurrentHW
                Status             = "FAILED"
                Message            = $_.Exception.Message
            }

        }

    }

}


# ============================================================
# SUMMARY
# ============================================================

Write-Host ""
Write-Host "============================================================"
Write-Host " SUMMARY"
Write-Host "============================================================"

$Results |
    Format-Table `
        ESXiHost,
        VM,
        PowerState,
        OldHardwareVersion,
        NewHardwareVersion,
        Status `
        -AutoSize


# ============================================================
# EXPORT LOG
# ============================================================

$Results |
    Export-Csv `
        -Path $LogFile `
        -NoTypeInformation `
        -Encoding UTF8


Write-Host ""
Write-Host "[INFO] Log saved:"
Write-Host $LogFile


# ============================================================
# DISCONNECT
# ============================================================

Disconnect-VIServer `
    -Server $vCenter `
    -Confirm:$false

Write-Host ""
Write-Host "[DONE]"