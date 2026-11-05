#Requires -Version 7.0
# Create the Entra registrations using the signed-in az user, then pass their IDs and the web secret to Bicep.
# azd up runs this as preup (azd reads the Bicep parameters before preprovision fires) and again as preprovision;
# azd provision runs it once, as preprovision.
. "$PSScriptRoot\..\..\entra-chat-demo\infra\hooks\common.ps1"

# AWS side. The mock Cognito inside APIM needs two client secrets that only APIM and the bridge ever see. They are
# generated once and kept in the azd environment, so a rerun does not rotate them. A real AWS stack brings its own
# settings; they are checked here, before anything is created.
$mockSecretLength = 32
if ("$env:DEPLOY_MOCK_AWS" -ne "false") {
    foreach ($name in "MOCK_FOUNDRY_CLIENT_SECRET", "MOCK_COPILOT_CLIENT_SECRET") {
        if ("$([Environment]::GetEnvironmentVariable($name))".Length -lt $mockSecretLength) {
            $bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes($mockSecretLength)
            Set-AzdValue $name ([Convert]::ToHexString($bytes).ToLowerInvariant())
            Write-Host "Generated $name for the mock AWS credentials; the value was not displayed."
        }
    }
}
else {
    $realAwsSettings = "COGNITO_TOKEN_URL", "COGNITO_FOUNDRY_CLIENT_ID", "COGNITO_FOUNDRY_CLIENT_SECRET",
        "COGNITO_COPILOT_CLIENT_ID", "COGNITO_COPILOT_CLIENT_SECRET", "AWS_MCP_ORIGIN"
    $missing = @($realAwsSettings | Where-Object { -not [Environment]::GetEnvironmentVariable($_) })
    if ($missing.Count -gt 0) {
        throw ("DEPLOY_MOCK_AWS is false, so the real AWS settings are required. Set them with " +
            "'azd env set <name> <value>': $($missing -join ', '). See the README section 'Connecting a real AWS server'.")
    }
}

# Skip a repeat run right after a successful one. A Graph filter query can lag behind a create and would then
# try to create the same registration again.
$setupAt = [datetimeoffset]::MinValue
if ([datetimeoffset]::TryParse("$env:ENTRA_SETUP_AT", [cultureinfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None, [ref]$setupAt)) {
    $age = [datetimeoffset]::UtcNow - $setupAt
    if ($age -ge [timespan]::Zero -and $age -lt [timespan]::FromMinutes(5) -and
        $env:ENTRA_TENANT_ID -and $env:ENTRA_GATEWAY_API_CLIENT_ID -and $env:ENTRA_CHAT_API_CLIENT_ID -and
        $env:ENTRA_CHAT_WEB_CLIENT_ID -and $env:OIDC_CLIENT_SECRET) {
        Write-Host "Entra registrations were prepared moments ago; nothing to do."
        return
    }
}

# On a new environment azd asks for the subscription only after preup, but the Entra setup needs it now. Default to
# the Azure CLI's current subscription; its tenant is where the registrations are created.
if (-not $env:AZURE_SUBSCRIPTION_ID) {
    $current = az account show --only-show-errors --output json 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $current) {
        throw ("AZURE_SUBSCRIPTION_ID is not set and az is not signed in. Run az login (or " +
            "'azd env set AZURE_SUBSCRIPTION_ID <subscription-id>') and rerun.")
    }
    $account = $current | ConvertFrom-Json
    $env:AZURE_SUBSCRIPTION_ID = $account.id
    Set-AzdValue AZURE_SUBSCRIPTION_ID $account.id
    Write-Host ("AZURE_SUBSCRIPTION_ID was not set; using the Azure CLI's current subscription " +
        "'$($account.name)' ($($account.id)). To use another, run " +
        "'azd env set AZURE_SUBSCRIPTION_ID <subscription-id>' and rerun.")
}

$context = Get-HookContext
$user = Invoke-Graph GET "/me?`$select=id,mail,userPrincipalName"
if (-not $env:APIM_PUBLISHER_EMAIL) {
    $contact = if ($user.PSObject.Properties["mail"] -and $user.mail) { $user.mail } else { $user.userPrincipalName }
    Set-AzdValue APIM_PUBLISHER_EMAIL $contact
    Write-Host "APIM_PUBLISHER_EMAIL was not set; using $contact. Change it with: azd env set APIM_PUBLISHER_EMAIL <address>"
}
$gatewayName = "mcp-m2m-gateway-api-azd-$($env:AZURE_ENV_NAME)-$($env:AZURE_SUBSCRIPTION_ID.Substring(0, 8).ToLowerInvariant())"
$defaultAccessRoleId = [guid]::Empty.ToString()

$api = Get-DemoApp $context.ApiName @{
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

$web = Get-DemoApp $context.WebName @{
    web = @{
        implicitGrantSettings = @{ enableAccessTokenIssuance = $false; enableIdTokenIssuance = $false }
    }
    requiredResourceAccess = @(@{
        resourceAppId = $api.appId
        resourceAccess = @(@{ id = $scope[0].id; type = "Scope" })
    })
}
if ($web.signInAudience -ne "AzureADMyOrg") {
    throw "Existing web registration is not single-tenant."
}

$gateway = Get-DemoApp $gatewayName @{
    api = @{ requestedAccessTokenVersion = 2 }
    appRoles = @(@{
        id = [guid]::NewGuid().ToString()
        value = "Mcp.Invoke"; displayName = "Invoke the MCP gateway"
        description = "Call the M2M MCP gateway as an application or managed identity."
        allowedMemberTypes = @("Application"); isEnabled = $true
    })
}
$invokeRole = @($gateway.appRoles | Where-Object { $_.value -eq "Mcp.Invoke" -and $_.isEnabled })
if ($invokeRole.Count -ne 1 -or $gateway.signInAudience -ne "AzureADMyOrg") {
    throw "Existing gateway registration does not match the single-tenant Mcp.Invoke role contract."
}

# Set app ID URIs only after the app IDs exist; Graph's normal user-authenticated API handles this update.
$null = Invoke-GraphWithRetry PATCH "/applications/$($api.id)" @{
    identifierUris = @("api://$($api.appId)")
    api = @{
        requestedAccessTokenVersion = 2
        preAuthorizedApplications = @(@{ appId = $web.appId; delegatedPermissionIds = @($scope[0].id) })
    }
}
$null = Invoke-GraphWithRetry PATCH "/applications/$($gateway.id)" @{
    identifierUris = @("api://$($gateway.appId)")
}

$apiPrincipal = Get-DemoPrincipal $api.appId
$webPrincipal = Get-DemoPrincipal $web.appId
$null = Get-DemoPrincipal $gateway.appId
Grant-DemoRole $user.id $apiPrincipal.id $role[0].id
Grant-DemoRole $user.id $webPrincipal.id $defaultAccessRoleId

$storedKeyId = $env:OIDC_CLIENT_SECRET_KEY_ID
$storedExpiry = $null
if ($env:OIDC_CLIENT_SECRET_EXPIRES_AT) {
    $storedExpiry = [datetimeoffset]::Parse($env:OIDC_CLIENT_SECRET_EXPIRES_AT, [cultureinfo]::InvariantCulture)
}
$secretStillGood = $env:OIDC_CLIENT_SECRET -and $storedKeyId -and $storedExpiry -and
    ($storedExpiry -gt [datetimeoffset]::UtcNow.AddDays(14)) -and
    (@($web.passwordCredentials | Where-Object { $_.keyId -eq $storedKeyId }).Count -eq 1)

if (-not $secretStillGood) {
    $expiry = [datetimeoffset]::UtcNow.AddDays(90).ToString("o")
    $password = Invoke-GraphWithRetry POST "/applications/$($web.id)/addPassword" @{
        passwordCredential = @{ displayName = "Entra MCP chat demo (azd $($env:AZURE_ENV_NAME))"; endDateTime = $expiry }
    }
    Set-AzdValue OIDC_CLIENT_SECRET $password.secretText
    Set-AzdValue OIDC_CLIENT_SECRET_KEY_ID $password.keyId
    Set-AzdValue OIDC_CLIENT_SECRET_EXPIRES_AT ([datetimeoffset]::Parse(
        $password.endDateTime, [cultureinfo]::InvariantCulture).ToString("o", [cultureinfo]::InvariantCulture))
    $password = $null
    if ($storedKeyId -and @($web.passwordCredentials | Where-Object { $_.keyId -eq $storedKeyId }).Count -eq 1) {
        $null = Invoke-GraphWithRetry POST "/applications/$($web.id)/removePassword" @{ keyId = $storedKeyId }
    }
    Write-Host "Created a new web client secret (90 days); the secret value was not displayed."
}
else {
    Write-Host "Reusing the existing web client secret (expires $($storedExpiry.ToString('u')))."
}

Set-AzdValue ENTRA_TENANT_ID $context.TenantId
Set-AzdValue ENTRA_GATEWAY_API_CLIENT_ID $gateway.appId
Set-AzdValue ENTRA_CHAT_API_CLIENT_ID $api.appId
Set-AzdValue ENTRA_CHAT_WEB_CLIENT_ID $web.appId
Set-AzdValue ENTRA_SETUP_AT ([datetimeoffset]::UtcNow.ToString("o", [cultureinfo]::InvariantCulture))
Write-Host "Entra registrations are ready. The signed-in user owns and manages these registrations."
