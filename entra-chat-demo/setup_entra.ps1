#Requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [guid]$SubscriptionId,
    [switch]$RotateSecret
)

$ErrorActionPreference = "Stop"
$PSNativeCommandUseErrorActionPreference = $false
if (-not $IsWindows) {
    throw "This local launcher uses Windows DPAPI to protect its credential."
}

function Invoke-Graph([string]$Method, [string]$Path, [hashtable]$Body) {
    $arguments = @("rest", "--method", $Method, "--url", "https://graph.microsoft.com/v1.0$Path",
        "--subscription", "$SubscriptionId", "--only-show-errors", "--output", "json")
    $bodyFile = $null
    try {
        if ($null -ne $Body) {
            $bodyFile = [IO.Path]::GetTempFileName()
            [IO.File]::WriteAllText($bodyFile, ($Body | ConvertTo-Json -Depth 20 -Compress))
            $arguments += @("--body", "@$bodyFile")
        }
        $output = & az @arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Microsoft Graph $Method $Path failed. Resolve the reported Entra permission/error before retrying."
        }
        if ($output) { return ($output | ConvertFrom-Json) }
    }
    finally {
        if ($bodyFile) { Remove-Item -LiteralPath $bodyFile }
    }
}

function Get-DemoApp([string]$Name, [hashtable]$Definition) {
    $filter = [uri]::EscapeDataString("displayName eq '$Name'")
    $apps = @( (Invoke-Graph GET "/applications?`$filter=$filter").value )
    if ($apps.Count -gt 1) { throw "Multiple registrations named $Name; resolve the duplicate explicitly." }
    if ($apps.Count -eq 1) {
        if ($apps[0].tags -notcontains "EntraMcpChatDemo") {
            throw "$Name exists but is not owned by this demo. Refusing to change it."
        }
        return $apps[0]
    }
    $Definition.displayName = $Name
    $Definition.signInAudience = "AzureADMyOrg"
    $Definition.tags = @("EntraMcpChatDemo")
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
    return Invoke-Graph POST "/servicePrincipals" @{ appId = $AppId; appRoleAssignmentRequired = $true }
}

function Grant-DemoRole([string]$PrincipalId, [string]$ResourceId, [string]$RoleId) {
    $assignments = @( (Invoke-Graph GET "/servicePrincipals/$ResourceId/appRoleAssignedTo").value )
    if (-not ($assignments | Where-Object { $_.principalId -eq $PrincipalId -and $_.appRoleId -eq $RoleId })) {
        $null = Invoke-Graph POST "/servicePrincipals/$ResourceId/appRoleAssignedTo" @{
            principalId = $PrincipalId; resourceId = $ResourceId; appRoleId = $RoleId
        }
    }
}

$accountJson = az account show --subscription "$SubscriptionId" --only-show-errors --output json
if ($LASTEXITCODE -ne 0) { throw "Sign in to the requested subscription using az login first." }
$account = $accountJson | ConvertFrom-Json
if ($account.user.type -ne "user") { throw "Run setup using your own Entra user, not a service principal." }
$user = Invoke-Graph GET "/me?`$select=id"
$suffix = "$SubscriptionId".Substring(0, 8)
$stateDirectory = Join-Path $PSScriptRoot ".entra"
$null = New-Item -ItemType Directory -Path $stateDirectory -Force
$statePath = Join-Path $stateDirectory "config.json"
$secretPath = Join-Path $stateDirectory "client-secret.xml"
$previous = if (Test-Path -LiteralPath $statePath) {
    Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
}
if ($previous -and ($previous.tenantId -ne $account.tenantId -or $previous.subscriptionId -ne "$SubscriptionId")) {
    throw "Local Entra configuration belongs to another subscription/tenant. Use a separate demo folder."
}

$api = Get-DemoApp "mcp-chat-demo-api-$suffix" @{
    api = @{
        requestedAccessTokenVersion = 2
        oauth2PermissionScopes = @(@{
            id = [guid]::NewGuid().ToString()
            value = "inventory.read"; type = "User"; isEnabled = $true
            adminConsentDisplayName = "Read fictional demo inventory"
            adminConsentDescription = "Read fictional inventory as the signed-in user. No production data."
            userConsentDisplayName = "Read fictional demo inventory"
            userConsentDescription = "Read fictional inventory as you. No production data."
        })
    }
    appRoles = @(@{
        id = [guid]::NewGuid().ToString()
        value = "Inventory.Reader"; displayName = "Inventory Reader"
        description = "Read fictional inventory through the Entra MCP chat demo."
        allowedMemberTypes = @("User"); isEnabled = $true
    })
}
$scope = @($api.api.oauth2PermissionScopes | Where-Object { $_.value -eq "inventory.read" -and $_.isEnabled })
$role = @($api.appRoles | Where-Object { $_.value -eq "Inventory.Reader" -and $_.isEnabled })
if ($scope.Count -ne 1 -or $role.Count -ne 1 -or $api.signInAudience -ne "AzureADMyOrg") {
    throw "Existing API registration does not match the demo's single-tenant scope/role contract."
}
$webUrl = "http://localhost:9102"
$web = Get-DemoApp "mcp-chat-demo-web-$suffix" @{
    web = @{
        redirectUris = @("$webUrl/auth/callback", "$webUrl/")
        implicitGrantSettings = @{ enableAccessTokenIssuance = $false; enableIdTokenIssuance = $false }
    }
    requiredResourceAccess = @(@{
        resourceAppId = $api.appId
        resourceAccess = @(@{ id = $scope[0].id; type = "Scope" })
    })
}
if ($web.signInAudience -ne "AzureADMyOrg" -or
    $web.web.redirectUris -notcontains "$webUrl/auth/callback" -or
    $web.web.redirectUris -notcontains "$webUrl/") {
    throw "Existing web registration does not match the demo's tenant and redirect URIs."
}

# Only this client is preauthorized for this API scope. Assignment is still required.
$null = Invoke-Graph PATCH "/applications/$($api.id)" @{
    identifierUris = @("api://$($api.appId)")
    api = @{
        requestedAccessTokenVersion = 2
        preAuthorizedApplications = @(@{ appId = $web.appId; delegatedPermissionIds = @($scope[0].id) })
    }
}
$apiPrincipal = Get-DemoPrincipal $api.appId
$webPrincipal = Get-DemoPrincipal $web.appId
Grant-DemoRole $user.id $apiPrincipal.id $role[0].id
Grant-DemoRole $user.id $webPrincipal.id ([guid]::Empty.ToString())

$state = @{
    subscriptionId = "$SubscriptionId"; subscriptionName = $account.name; tenantId = $account.tenantId
    apiAppId = $api.appId; apiObjectId = $api.id; apiServicePrincipalId = $apiPrincipal.id
    webAppId = $web.appId; webObjectId = $web.id; webServicePrincipalId = $webPrincipal.id
    scopeId = $scope[0].id; roleId = $role[0].id
    webUrl = $webUrl; webPort = 9102; mcpPort = 9101
}
$reuse = $previous -and $previous.webAppId -eq $web.appId -and
    (Test-Path -LiteralPath $secretPath) -and $previous.secretExpiresAt -and
    ([datetimeoffset]$previous.secretExpiresAt -gt [datetimeoffset]::UtcNow.AddDays(1))
if ($reuse -and -not $RotateSecret) {
    $credential = Import-Clixml -LiteralPath $secretPath
    if ($credential.UserName -ne $web.appId) { throw "Protected credential belongs to another web app." }
    $state.secretKeyId = $previous.secretKeyId
    $state.secretExpiresAt = $previous.secretExpiresAt
}
else {
    $expiry = [datetimeoffset]::UtcNow.AddDays(30).ToString("o")
    $password = Invoke-Graph POST "/applications/$($web.id)/addPassword" @{
        passwordCredential = @{ displayName = "Entra MCP chat demo (local)"; endDateTime = $expiry }
    }
    $secure = ConvertTo-SecureString $password.secretText -AsPlainText -Force
    $credential = [pscredential]::new($web.appId, $secure)
    $credential | Export-Clixml -LiteralPath $secretPath
    $state.secretKeyId = $password.keyId
    $state.secretExpiresAt = $password.endDateTime
    $password = $null
}
$state | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $statePath -Encoding utf8
if ($previous -and $previous.webAppId -eq $web.appId -and $previous.secretKeyId -and
    $previous.secretKeyId -ne $state.secretKeyId) {
    $null = Invoke-Graph POST "/applications/$($web.id)/removePassword" @{ keyId = $previous.secretKeyId }
}
Write-Host "Configured tenant $($account.tenantId) for subscription $($account.name)."
Write-Host "API: $($api.appId); web: $($web.appId). Access is assigned only to the setup user."
Write-Host "Web credential expires $($state.secretExpiresAt); protected with Windows DPAPI, never printed."
Write-Host "Run .\run_entra.ps1 and open $webUrl. No Azure resources or Graph API permissions were granted to the demo."
