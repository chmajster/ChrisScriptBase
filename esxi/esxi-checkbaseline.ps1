$VCenterServer = "vcenter.example.local"
$VCenterUser   = "administrator@vsphere.local"
$VCenterPass   = "CHANGE_ME"

# Opcjonalny eksport CSV
$ExportCsv = $true
$CsvFile   = ".\esxi-baseline-report.csv"

# Ignorowanie self-signed certyfikatów
$IgnoreInvalidCertificate = $true


# ============================================================
# POWERCli CONFIGURATION
# ============================================================

if ($IgnoreInvalidCertificate) {
    Set-PowerCLIConfiguration `
        -InvalidCertificateAction Ignore `
        -Confirm:$false | Out-Null
}


# ============================================================
# CONNECT TO VCENTER
# ============================================================

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host " ESXi Baseline Compliance Checker" -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "[INFO] Connecting to vCenter: $VCenterServer" -ForegroundColor Yellow

try {

    $SecurePassword = ConvertTo-SecureString `
        $VCenterPass `
        -AsPlainText `
        -Force

    $Credential = New-Object `
        System.Management.Automation.PSCredential `
        ($VCenterUser, $SecurePassword)

    Connect-VIServer `
        -Server $VCenterServer `
        -Credential $Credential `
        -ErrorAction Stop | Out-Null

}
catch {

    Write-Host "[ERROR] Cannot connect to vCenter." -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}


Write-Host "[OK] Connected to vCenter." -ForegroundColor Green
Write-Host ""


# ============================================================
# REPORT
# ============================================================

$Report = @()

$Clusters = Get-Cluster | Sort-Object Name


foreach ($Cluster in $Clusters) {

    Write-Host ""
    Write-Host "==================================================" -ForegroundColor DarkCyan
    Write-Host "Cluster: $($Cluster.Name)" -ForegroundColor Cyan
    Write-Host "==================================================" -ForegroundColor DarkCyan

    $VMHosts = Get-VMHost `
        -Location $Cluster `
        | Sort-Object Name


    foreach ($VMHost in $VMHosts) {

        Write-Host ""
        Write-Host "Host: $($VMHost.Name)" -ForegroundColor White

        # ----------------------------------------------------
        # GENERAL HOST INFORMATION
        # ----------------------------------------------------

        $ConnectionState = $VMHost.ConnectionState
        $PowerState      = $VMHost.PowerState

        $MaintenanceMode = if ($VMHost.ExtensionData.Runtime.InMaintenanceMode) {
            "Yes"
        }
        else {
            "No"
        }


        # ----------------------------------------------------
        # ESXi VERSION
        # ----------------------------------------------------

        $ESXiVersion = $VMHost.Version
        $ESXiBuild   = $VMHost.Build


        # ----------------------------------------------------
        # UPTIME
        # ----------------------------------------------------

        $BootTime = $VMHost.ExtensionData.Runtime.BootTime

        if ($BootTime) {

            $Uptime = New-TimeSpan `
                -Start $BootTime `
                -End (Get-Date)

            $UptimeString = "{0}d {1}h {2}m" -f `
                $Uptime.Days,
                $Uptime.Hours,
                $Uptime.Minutes

        }
        else {

            $UptimeString = "N/A"

        }


        # ----------------------------------------------------
        # NUMBER OF VMs
        # ----------------------------------------------------

        try {

            $VMCount = (
                Get-VM `
                    -Location $VMHost `
                    -ErrorAction SilentlyContinue
            ).Count

        }
        catch {

            $VMCount = 0

        }


        # ----------------------------------------------------
        # BASELINES
        # ----------------------------------------------------

        try {

            $Baselines = Get-Baseline `
                -Entity $VMHost `
                -ErrorAction Stop

        }
        catch {

            $Baselines = @()

        }


        # ----------------------------------------------------
        # NO BASELINE ATTACHED
        # ----------------------------------------------------

        if (-not $Baselines) {

            Write-Host "  Baseline: NONE" -ForegroundColor DarkYellow

            $Report += [PSCustomObject]@{

                Cluster          = $Cluster.Name
                Host             = $VMHost.Name
                ConnectionState  = $ConnectionState
                PowerState       = $PowerState
                MaintenanceMode  = $MaintenanceMode

                ESXiVersion      = $ESXiVersion
                ESXiBuild        = $ESXiBuild

                Uptime           = $UptimeString
                VMCount          = $VMCount

                Baseline         = "NONE"
                ComplianceStatus = "N/A"
            }

            continue

        }


        # ----------------------------------------------------
        # CHECK EVERY BASELINE
        # ----------------------------------------------------

        foreach ($Baseline in $Baselines) {

            Write-Host "  Baseline: $($Baseline.Name)" -NoNewline

            try {

                # Refresh compliance information
                Test-Compliance `
                    -Entity $VMHost `
                    -UpdateType HostPatch `
                    -ErrorAction SilentlyContinue | Out-Null


                $Compliance = Get-Compliance `
                    -Entity $VMHost `
                    -Baseline $Baseline `
                    -ErrorAction Stop


                if ($Compliance) {

                    $Status = $Compliance.Status

                }
                else {

                    $Status = "Unknown"

                }

            }
            catch {

                $Status = "Error"

            }


            # ------------------------------------------------
            # CONSOLE COLOR
            # ------------------------------------------------

            switch -Wildcard ($Status.ToString()) {

                "Compliant" {

                    Write-Host " -> COMPLIANT" -ForegroundColor Green

                }

                "NotCompliant" {

                    Write-Host " -> NOT COMPLIANT" -ForegroundColor Red

                }

                "*Incompatible*" {

                    Write-Host " -> INCOMPATIBLE" -ForegroundColor Magenta

                }

                "Unknown" {

                    Write-Host " -> UNKNOWN" -ForegroundColor Yellow

                }

                "Error" {

                    Write-Host " -> ERROR" -ForegroundColor Red

                }

                default {

                    Write-Host " -> $Status" -ForegroundColor Yellow

                }

            }


            # ------------------------------------------------
            # REPORT OBJECT
            # ------------------------------------------------

            $Report += [PSCustomObject]@{

                Cluster          = $Cluster.Name
                Host             = $VMHost.Name
                ConnectionState  = $ConnectionState
                PowerState       = $PowerState
                MaintenanceMode  = $MaintenanceMode

                ESXiVersion      = $ESXiVersion
                ESXiBuild        = $ESXiBuild

                Uptime           = $UptimeString
                VMCount          = $VMCount

                Baseline         = $Baseline.Name
                ComplianceStatus = $Status
            }

        }

    }

}


# ============================================================
# DISPLAY REPORT
# ============================================================

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " COMPLIANCE REPORT" -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

$Report |
    Sort-Object Cluster, Host, Baseline |
    Format-Table `
        Cluster,
        Host,
        ConnectionState,
        MaintenanceMode,
        ESXiVersion,
        ESXiBuild,
        Uptime,
        VMCount,
        Baseline,
        ComplianceStatus `
        -AutoSize


# ============================================================
# SUMMARY
# ============================================================

$TotalHosts = (
    $Report |
    Select-Object -ExpandProperty Host -Unique
).Count

$Compliant = (
    $Report |
    Where-Object {
        $_.ComplianceStatus -eq "Compliant"
    }
).Count

$NotCompliant = (
    $Report |
    Where-Object {
        $_.ComplianceStatus -eq "NotCompliant"
    }
).Count

$Unknown = (
    $Report |
    Where-Object {
        $_.ComplianceStatus -notin @(
            "Compliant",
            "NotCompliant"
        )
    }
).Count


Write-Host ""
Write-Host "================ SUMMARY =================" -ForegroundColor Cyan

Write-Host "Hosts          : $TotalHosts"
Write-Host "Compliant      : $Compliant" -ForegroundColor Green
Write-Host "Not compliant  : $NotCompliant" -ForegroundColor Red
Write-Host "Other/Unknown  : $Unknown" -ForegroundColor Yellow

Write-Host "==========================================" -ForegroundColor Cyan


# ============================================================
# CSV EXPORT
# ============================================================

if ($ExportCsv) {

    try {

        $Report |
            Export-Csv `
                -Path $CsvFile `
                -NoTypeInformation `
                -Encoding UTF8

        Write-Host ""
        Write-Host "[OK] Report exported to:" -ForegroundColor Green
        Write-Host "     $CsvFile"

    }
    catch {

        Write-Host "[ERROR] CSV export failed." -ForegroundColor Red
        Write-Host $_.Exception.Message -ForegroundColor Red

    }

}


# ============================================================
# DISCONNECT
# ============================================================

Disconnect-VIServer `
    -Server $VCenterServer `
    -Confirm:$false

Write-Host ""
Write-Host "[OK] Disconnected from vCenter." -ForegroundColor Green