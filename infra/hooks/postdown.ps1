#Requires -Version 7.0
# azd postdown hook. `azd down` deletes the resource group but not the Microsoft Entra app registrations that
# infra/hooks/preprovision.ps1 created, so this script removes them. It finds each one by uniqueName and deletes it
# only if it carries the owner tag, so an app that merely has a similar name is never touched. It then purges the
# deleted app from the Entra recycle bin: a soft-deleted app keeps its uniqueName reserved for 30 days, which
# would make the next `azd up` with the same environment name fail.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $false

$ownerTag = 'EntraMcpChatDemo'

foreach ($required in 'AZURE_ENV_NAME', 'AZURE_SUBSCRIPTION_ID') {
    if (-not [Environment]::GetEnvironmentVariable($required)) {
        throw "$required is not set. Run this through azd (azd down)."
    }
}

# Keep Graph paths free of '&' and parentheses: az is a .cmd file on Windows and cmd.exe misparses both.
function Invoke-Graph([string]$Method, [string]$Path) {
    $errorFile = [IO.Path]::GetTempFileName()
    try {
        $output = & az rest --method $Method --url "https://graph.microsoft.com/v1.0$Path" `
            --subscription $env:AZURE_SUBSCRIPTION_ID --only-show-errors --output json 2>$errorFile
        if ($LASTEXITCODE -ne 0) {
            $detail = (Get-Content -LiteralPath $errorFile -Raw)
            if ($null -eq $detail) { $detail = '' }
            $detail = $detail.Trim()
            if ($detail.Length -gt 600) { $detail = $detail.Substring(0, 600) }
            throw "Microsoft Graph $Method $Path failed: $detail"
        }
        if ($output) { return ($output | ConvertFrom-Json) }
    }
    finally {
        Remove-Item -LiteralPath $errorFile -ErrorAction SilentlyContinue
    }
}

# A deleted app can take a few seconds to show up in the recycle bin, so retry before giving up.
function Remove-FromRecycleBin([string]$ObjectId, [string]$Name) {
    $lastError = ''
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            $null = Invoke-Graph DELETE "/directory/deletedItems/$ObjectId"
            Write-Host "Permanently removed $Name from the Entra recycle bin."
            return
        }
        catch {
            $lastError = $_.Exception.Message
            if ($attempt -lt 5) { Start-Sleep -Seconds 3 }
        }
    }
    Write-Warning ("$Name is still in the Entra recycle bin, which keeps its uniqueName reserved for 30 days. " +
        "To redeploy with the same environment name, permanently delete it (Entra admin center > App registrations > " +
        "Deleted applications) or pick a new environment name. Last error: $lastError")
}

$suffix = "azd-$($env:AZURE_ENV_NAME)-$($env:AZURE_SUBSCRIPTION_ID.Substring(0, 8).ToLowerInvariant())"
$names = "mcp-chat-demo-api-$suffix", "mcp-chat-demo-web-$suffix", "mcp-m2m-gateway-api-$suffix"

az account show --subscription $env:AZURE_SUBSCRIPTION_ID --only-show-errors --output none 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "az is not signed in to subscription $($env:AZURE_SUBSCRIPTION_ID). Run az login (azd auth login is separate), then delete these app registrations by hand: $($names -join ', ')."
}

foreach ($name in $names) {
    $filter = [uri]::EscapeDataString("uniqueName eq '$($name.Replace("'", "''"))'")
    $found = @((Invoke-Graph GET "/applications?`$filter=$filter").value)
    $app = if ($found.Count -gt 0) { $found[0] } else { $null }
    if (-not $app) {
        Write-Host "No app registration named $name; nothing to delete."
        continue
    }
    if ($app.tags -notcontains $ownerTag) {
        Write-Warning "$name was not created by this template (no $ownerTag tag); leaving it in place."
        continue
    }
    $null = Invoke-Graph DELETE "/applications/$($app.id)"
    Write-Host "Deleted app registration $name."
    Remove-FromRecycleBin $app.id $name
}
Write-Host 'Entra cleanup finished.'
