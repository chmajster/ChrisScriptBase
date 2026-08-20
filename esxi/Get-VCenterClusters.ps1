
$VCenter = "vcenter01.domain.local"
$Username = "administrator@vsphere.local"
$Password = "YourPasswordHere"

param(
    [Parameter(Mandatory = $true)]
    [string]$VCenter
)

Import-Module VMware.PowerCLI

Set-PowerCLIConfiguration `
    -InvalidCertificateAction Ignore `
    -Confirm:$false | Out-Null

Connect-VIServer -Server $VCenter

$result = foreach ($cluster in Get-Cluster | Sort-Object Name) {

    foreach ($esxi in Get-VMHost -Location $cluster | Sort-Object Name) {

        $vmCount = (Get-VM -Location $esxi -ErrorAction SilentlyContinue).Count

        $uptime = if ($esxi.ExtensionData.Runtime.BootTime) {
            (Get-Date) - $esxi.ExtensionData.Runtime.BootTime
        }

        [PSCustomObject]@{
            Cluster       = $cluster.Name
            Host          = $esxi.Name
            Status        = $esxi.ConnectionState
            Power         = $esxi.PowerState
            Maintenance   = $esxi.ExtensionData.Runtime.InMaintenanceMode
            Version       = $esxi.Version
            Build         = $esxi.Build
            VMs           = $vmCount
            UptimeDays    = if ($uptime) {
                [math]::Floor($uptime.TotalDays)
            } else {
                "N/A"
            }
            CPUPercent    = if ($esxi.CpuTotalMhz -gt 0) {
                [math]::Round(
                    ($esxi.CpuUsageMhz / $esxi.CpuTotalMhz) * 100,
                    1
                )
            } else {
                0
            }
            MemoryPercent = if ($esxi.MemoryTotalGB -gt 0) {
                [math]::Round(
                    ($esxi.MemoryUsageGB / $esxi.MemoryTotalGB) * 100,
                    1
                )
            } else {
                0
            }
        }
    }
}

$result | Format-Table -AutoSize

Disconnect-VIServer -Server $VCenter -Confirm:$false