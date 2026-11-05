# Shared helpers for the azd hooks. Dot-source this file; it is not run on its own.
# The Graph logic mirrors setup_entra.ps1 (the local launcher) but keys the registrations to the azd environment.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false

$script:OwnerTag = "EntraMcpChatDemo"
$script:PermissionErrorPattern = "Authorization_RequestDenied|Insufficient privileges"

function Get-HookContext {
    foreach ($name in "AZURE_ENV_NAME", "AZURE_SUBSCRIPTION_ID") {
        if (-not [Environment]::GetEnvironmentVariable($name)) {
            throw "$name is not set. Run this through azd (azd up, azd provision, azd down)."
        }
    }
    $json = az account show --subscription $env:AZURE_SUBSCRIPTION_ID --only-show-errors --output json
    if ($LASTEXITCODE -ne 0) {
        throw "az is not signed in to subscription $($env:AZURE_SUBSCRIPTION_ID). Run az login first (azd auth login is separate)."
    }
    $account = $json | ConvertFrom-Json
    if ($account.user.type -ne "user") {
        throw "Run as your own Entra user, not a service principal; the hooks assign the app roles to you."
    }
    $suffix = $env:AZURE_SUBSCRIPTION_ID.Substring(0, 8)
    [pscustomobject]@{
        TenantId = $account.tenantId
        ApiName  = "mcp-chat-demo-api-azd-$($env:AZURE_ENV_NAME)-$suffix"
        WebName  = "mcp-chat-demo-web-azd-$($env:AZURE_ENV_NAME)-$suffix"
    }
}

function Invoke-Graph([string]$Method, [string]$Path, [hashtable]$Body) {
    $arguments = @("rest", "--method", $Method, "--url", "https://graph.microsoft.com/v1.0$Path",
        "--subscription", $env:AZURE_SUBSCRIPTION_ID, "--only-show-errors", "--output", "json")
    $bodyFile = $null
    $errorFile = [IO.Path]::GetTempFileName()
    try {
        if ($null -ne $Body) {
            $bodyFile = [IO.Path]::GetTempFileName()
            [IO.File]::WriteAllText($bodyFile, ($Body | ConvertTo-Json -Depth 20 -Compress))
            $arguments += @("--body", "@$bodyFile")
        }
        $output = & az @arguments 2>$errorFile
        if ($LASTEXITCODE -ne 0) {
            $detail = (Get-Content -LiteralPath $errorFile -Raw)
            if ($detail.Length -gt 600) { $detail = $detail.Substring(0, 600) }
            $message = "Microsoft Graph $Method $Path failed: $($detail.Trim())"
            if ($detail -match $script:PermissionErrorPattern) {
                $message += " Your Entra account is not allowed to make this change. Activate the Cloud Application " +
                    "Administrator or Application Administrator role in Privileged Identity Management (PIM), run " +
                    "az logout and az login so the role is in your token, then rerun. An administrator can also do it."
            }
            throw $message
        }
        if ($output) { return ($output | ConvertFrom-Json) }
    }
    finally {
        Remove-Item -LiteralPath $errorFile -ErrorAction SilentlyContinue
        if ($bodyFile) { Remove-Item -LiteralPath $bodyFile -ErrorAction SilentlyContinue }
    }
}

# A new application takes a few seconds to replicate, so calls that reference it can briefly fail.
# A permission error never clears by waiting, so it is raised immediately.
function Invoke-GraphWithRetry([string]$Method, [string]$Path, [hashtable]$Body) {
    for ($attempt = 1; ; $attempt++) {
        try { return Invoke-Graph $Method $Path $Body }
        catch {
            if ($attempt -ge 6 -or $_.Exception.Message -match $script:PermissionErrorPattern) { throw }
            Write-Host "Waiting for Entra to replicate (attempt $attempt of 6)..."
            Start-Sleep -Seconds (4 * $attempt)
        }
    }
}

function Find-DemoApps([string]$Name) {
    $escaped = $Name.Replace("'", "''")
    $filter = [uri]::EscapeDataString("displayName eq '$escaped'")
    @( (Invoke-Graph GET "/applications?`$filter=$filter").value )
}

function Get-DemoApp([string]$Name, [hashtable]$Definition) {
    $apps = @(Find-DemoApps $Name)
    if ($apps.Count -gt 1) { throw "Multiple registrations named $Name; remove the duplicates in the Entra portal." }
    if ($apps.Count -eq 1) {
        if ($apps[0].tags -notcontains $script:OwnerTag) {
            throw "$Name exists but was not created by this demo. Refusing to change it."
        }
        if ($apps[0].uniqueName -and $apps[0].uniqueName -ne $Name) {
            throw "$Name has a different immutable uniqueName. Refusing to change it."
        }
        if (-not $apps[0].uniqueName) {
            $null = Invoke-GraphWithRetry PATCH "/applications/$($apps[0].id)" @{ uniqueName = $Name }
        }
        return $apps[0]
    }
    $Definition.displayName = $Name
    $Definition.uniqueName = $Name
    $Definition.signInAudience = "AzureADMyOrg"
    $Definition.tags = @($script:OwnerTag)
    return Invoke-Graph POST "/applications" $Definition
}

function Get-DemoPrincipal([string]$AppId) {
    $principals = @( (Invoke-Graph GET "/servicePrincipals?`$filter=appId%20eq%20'$AppId'").value )
    if ($principals.Count -gt 1) { throw "Multiple service principals for $AppId." }
    if ($principals.Count -eq 1) {
        $principal = $principals[0]
        if (-not $principal.appRoleAssignmentRequired) {
            $null = Invoke-Graph PATCH "/servicePrincipals/$($principal.id)" @{ appRoleAssignmentRequired = $true }
        }
        return $principal
    }
    return Invoke-GraphWithRetry POST "/servicePrincipals" @{ appId = $AppId; appRoleAssignmentRequired = $true }
}

function Grant-DemoRole([string]$PrincipalId, [string]$ResourceId, [string]$RoleId) {
    $assignments = @( (Invoke-GraphWithRetry GET "/servicePrincipals/$ResourceId/appRoleAssignedTo").value )
    if (-not ($assignments | Where-Object { $_.principalId -eq $PrincipalId -and $_.appRoleId -eq $RoleId })) {
        $null = Invoke-GraphWithRetry POST "/servicePrincipals/$ResourceId/appRoleAssignedTo" @{
            principalId = $PrincipalId; resourceId = $ResourceId; appRoleId = $RoleId
        }
    }
}

# "--" keeps a value that starts with "-" from being read as a flag.
function Set-AzdValue([string]$Key, [string]$Value) {
    azd env set -- $Key $Value
    if ($LASTEXITCODE -ne 0) { throw "azd env set $Key failed." }
}
