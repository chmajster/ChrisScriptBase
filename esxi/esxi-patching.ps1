# ============================================================
# CONFIGURATION
# ============================================================

$vCenter       = "vcenter.domain.local"
$vCenterUser   = "administrator@vsphere.local"
$vCenterPass   = "Password123!"
$ClusterName   = "Production-Cluster"

# Exact baseline name from Lifecycle Manager / Update Manager
$BaselineName  = "ESXi 8 Critical Patches"

# Automatically attach baseline to cluster if it is not attached
$AttachBaseline = $true

# Leave host in Maintenance Mode when patching fails
$LeaveMaintenanceOnFailure = $true

# Log file
$LogFile = ".\esxi-patching-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"


# ============================================================
# FUNCTIONS
# ============================================================

function Write-Log {

    param(
        [string]$Message,
        [string]$Level = "INFO"
    )

    $Timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

    $Line = "[$Timestamp] [$Level] $Message"

    Write-Host $Line
    Add-Content -Path $LogFile -Value $Line
}


function Exit-WithError {

    param(
        [string]$Message
    )

    Write-Log $Message "ERROR"

    if ($global:DefaultVIServer) {
        Disconnect-VIServer -Server $global:DefaultVIServer `
            -Confirm:$false `
            -ErrorAction SilentlyContinue
    }

    exit 1
}


# ============================================================
# POWCLI CONFIGURATION
# ============================================================

$ErrorActionPreference = "Stop"

Write-Log "================================================="
Write-Log "Starting ESXi patching"
Write-Log "vCenter:  $vCenter"
Write-Log "Cluster:  $ClusterName"
Write-Log "Baseline: $BaselineName"
Write-Log "================================================="

try {

    Set-PowerCLIConfiguration `
        -InvalidCertificateAction Ignore `
        -Confirm:$false | Out-Null

}
catch {

    Write-Log "Unable to configure PowerCLI: $($_.Exception.Message)" "WARNING"
}


# ============================================================
# CONNECT VCENTER
# ============================================================

try {

    Write-Log "Connecting to vCenter $vCenter"

    $SecurePassword = ConvertTo-SecureString `
        $vCenterPass `
        -AsPlainText `
        -Force

    $Credential = New-Object `
        System.Management.Automation.PSCredential `
        ($vCenterUser, $SecurePassword)

    Connect-VIServer `
        -Server $vCenter `
        -Credential $Credential | Out-Null

    Write-Log "Connected to vCenter."

}
catch {

    Exit-WithError "vCenter connection failed: $($_.Exception.Message)"
}


# ============================================================
# GET CLUSTER
# ============================================================

try {

    $Cluster = Get-Cluster -Name $ClusterName

    Write-Log "Cluster found: $($Cluster.Name)"

}
catch {

    Exit-WithError "Cluster '$ClusterName' not found."
}


# ============================================================
# GET BASELINE
# ============================================================

try {

    $Baselines = @(
        Get-Baseline `
            -Name $BaselineName `
            -TargetType Host
    )

    if ($Baselines.Count -eq 0) {
        throw "Baseline not found."
    }

    if ($Baselines.Count -gt 1) {
        throw "More than one baseline named '$BaselineName' was found."
    }

    $Baseline = $Baselines[0]

    Write-Log "Baseline found: $($Baseline.Name)"

}
catch {

    Exit-WithError "Unable to get baseline '$BaselineName': $($_.Exception.Message)"
}


# ============================================================
# ATTACH BASELINE
# ============================================================

if ($AttachBaseline) {

    try {

        $AttachedBaselines = @(
            Get-Baseline `
                -Entity $Cluster `
                -Inherit
        )

        $AlreadyAttached = $AttachedBaselines |
            Where-Object {
                $_.Name -eq $BaselineName
            }

        if (-not $AlreadyAttached) {

            Write-Log "Attaching baseline '$BaselineName' to cluster '$ClusterName'"

            Add-EntityBaseline `
                -Entity $Cluster `
                -Baseline $Baseline | Out-Null

            Write-Log "Baseline attached."

        }
        else {

            Write-Log "Baseline is already attached to cluster."

        }

    }
    catch {

        Exit-WithError "Unable to attach baseline: $($_.Exception.Message)"

    }
}


# ============================================================
# GET ESXI HOSTS
# ============================================================

$Hosts = @(
    Get-VMHost -Location $Cluster |
        Where-Object {
            $_.ConnectionState -in @(
                "Connected",
                "Maintenance"
            )
        } |
        Sort-Object Name
)

if ($Hosts.Count -eq 0) {

    Exit-WithError "No available ESXi hosts found in cluster."
}


Write-Log "Hosts selected for patching: $($Hosts.Count)"

foreach ($HostEntry in $Hosts) {
    Write-Log "  $($HostEntry.Name)"
}


# ============================================================
# PATCH HOSTS SEQUENTIALLY
# ============================================================

$Results = @()

foreach ($VMHost in $Hosts) {

    $HostName = $VMHost.Name

    $StartTime = Get-Date

    $Status = "UNKNOWN"

    $OriginallyMaintenance =
        $VMHost.ConnectionState -eq "Maintenance"

    Write-Log ""
    Write-Log "================================================="
    Write-Log "Processing ESXi: $HostName"
    Write-Log "================================================="

    try {

        # ----------------------------------------------------
        # REFRESH HOST
        # ----------------------------------------------------

        $VMHost = Get-VMHost -Name $HostName

        Write-Log "Current state: $($VMHost.ConnectionState)"


        # ----------------------------------------------------
        # PRE-PATCH COMPLIANCE
        # ----------------------------------------------------

        Write-Log "Running pre-patch compliance scan."

        Test-Compliance `
            -Entity $VMHost `
            -UpdateType HostPatch | Out-Null

        $PreCompliance = @(
            Get-Compliance -Entity $VMHost |
                Where-Object {
                    $_.Baseline.Name -eq $BaselineName
                }
        )

        foreach ($Compliance in $PreCompliance) {

            Write-Log "Pre-patch compliance: $($Compliance.Status)"

        }


        # ----------------------------------------------------
        # MAINTENANCE MODE
        # ----------------------------------------------------

        if ($VMHost.ConnectionState -ne "Maintenance") {

            Write-Log "Entering Maintenance Mode: $HostName"

            Set-VMHost `
                -VMHost $VMHost `
                -State Maintenance `
                -Evacuate `
                -Confirm:$false | Out-Null

            $VMHost = Get-VMHost -Name $HostName

            if ($VMHost.ConnectionState -ne "Maintenance") {
                throw "Host failed to enter Maintenance Mode."
            }

            Write-Log "$HostName is now in Maintenance Mode."

        }
        else {

            Write-Log "$HostName was already in Maintenance Mode."

        }


        # ----------------------------------------------------
        # CHECK RUNNING VMs
        # ----------------------------------------------------

        $RunningVMs = @(
            Get-VM -Location $VMHost |
                Where-Object {
                    $_.PowerState -eq "PoweredOn"
                }
        )

        if ($RunningVMs.Count -gt 0) {

            Write-Log "WARNING: $($RunningVMs.Count) powered-on VMs detected on $HostName." "WARNING"

            foreach ($VM in $RunningVMs) {

                Write-Log "Running VM: $($VM.Name)" "WARNING"

            }

            throw "Powered-on VMs remain on ESXi host after entering Maintenance Mode."
        }


        # ----------------------------------------------------
        # REMEDIATE BASELINE
        # ----------------------------------------------------

        Write-Log "Starting remediation."

        Write-Log "Baseline: $BaselineName"

        Update-Entity `
            -Entity $VMHost `
            -Baseline $Baseline `
            -HostNumberOfRetries 2 `
            -HostRetryDelaySeconds 60 `
            -Confirm:$false | Out-Null

        Write-Log "Baseline remediation completed."


        # ----------------------------------------------------
        # WAIT FOR HOST
        # ----------------------------------------------------

        Write-Log "Refreshing ESXi status."

        $VMHost = Get-VMHost -Name $HostName

        Write-Log "Host state after remediation: $($VMHost.ConnectionState)"


        # ----------------------------------------------------
        # POST-PATCH COMPLIANCE
        # ----------------------------------------------------

        Write-Log "Running post-patch compliance scan."

        Test-Compliance `
            -Entity $VMHost `
            -UpdateType HostPatch | Out-Null

        $PostCompliance = @(
            Get-Compliance -Entity $VMHost |
                Where-Object {
                    $_.Baseline.Name -eq $BaselineName
                }
        )


        $Compliant = $false

        foreach ($Compliance in $PostCompliance) {

            Write-Log "Post-patch compliance: $($Compliance.Status)"

            if ($Compliance.Status -eq "Compliant") {
                $Compliant = $true
            }

        }


        if (-not $Compliant) {

            throw "Host is not compliant with baseline '$BaselineName' after remediation."

        }


        # ----------------------------------------------------
        # EXIT MAINTENANCE
        # ----------------------------------------------------

        if (-not $OriginallyMaintenance) {

            Write-Log "Exiting Maintenance Mode."

            Set-VMHost `
                -VMHost $VMHost `
                -State Connected `
                -Confirm:$false | Out-Null

            $VMHost = Get-VMHost -Name $HostName

            if ($VMHost.ConnectionState -ne "Connected") {

                throw "Host failed to exit Maintenance Mode."

            }

            Write-Log "$HostName returned to Connected state."

        }
        else {

            Write-Log "Host was in Maintenance Mode before patching."
            Write-Log "Leaving host in Maintenance Mode."

        }


        $Status = "SUCCESS"

        Write-Log "Patching completed successfully for $HostName."

    }
    catch {

        $Status = "FAILED"

        Write-Log "Patching failed for $HostName" "ERROR"
        Write-Log "$($_.Exception.Message)" "ERROR"

        if ($LeaveMaintenanceOnFailure) {

            Write-Log "Host will remain in Maintenance Mode for investigation." "WARNING"

        }
        else {

            try {

                $VMHost = Get-VMHost -Name $HostName

                if ($VMHost.ConnectionState -eq "Maintenance") {

                    Write-Log "Attempting to exit Maintenance Mode after failure."

                    Set-VMHost `
                        -VMHost $VMHost `
                        -State Connected `
                        -Confirm:$false | Out-Null

                }

            }
            catch {

                Write-Log "Unable to exit Maintenance Mode: $($_.Exception.Message)" "ERROR"

            }

        }

    }


    # ========================================================
    # RESULT
    # ========================================================

    $EndTime = Get-Date

    $Duration = New-TimeSpan `
        -Start $StartTime `
        -End $EndTime

    $CurrentHost = Get-VMHost `
        -Name $HostName `
        -ErrorAction SilentlyContinue

    $Results += [PSCustomObject]@{

        Host            = $HostName
        Status          = $Status
        ESXiVersion     = $CurrentHost.Version
        Build           = $CurrentHost.Build
        ConnectionState = $CurrentHost.ConnectionState
        Start           = $StartTime
        End             = $EndTime
        DurationMinutes = [math]::Round(
            $Duration.TotalMinutes,
            2
        )

    }

}


# ============================================================
# SUMMARY
# ============================================================

Write-Log ""
Write-Log "================================================="
Write-Log "PATCHING SUMMARY"
Write-Log "================================================="

$Results |
    Format-Table `
        Host,
        Status,
        ESXiVersion,
        Build,
        ConnectionState,
        DurationMinutes `
        -AutoSize


# CSV REPORT

$CsvFile = $LogFile.Replace(
    ".log",
    ".csv"
)

$Results |
    Export-Csv `
        -Path $CsvFile `
        -NoTypeInformation


Write-Log "CSV report: $CsvFile"


# ============================================================
# DISCONNECT VCENTER
# ============================================================

Disconnect-VIServer `
    -Server $vCenter `
    -Confirm:$false

Write-Log "Disconnected from vCenter."
Write-Log "ESXi patching finished."