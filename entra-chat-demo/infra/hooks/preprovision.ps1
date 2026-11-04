#Requires -Version 7.0
# azd preprovision hook: create or update the two Entra app registrations, assign the signed-in user,
# and publish their IDs (and the web client secret) to the azd environment for the Bicep parameters.
# Safe to re-run; it reuses what it already created.
. "$PSScriptRoot/common.ps1"

$context = Get-HookContext
$user = Invoke-Graph GET "/me?`$select=id"

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

# Redirect URIs are added by the postprovision hook once the app's URL exists.
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

# Only this client is preauthorized for the API scope. Assignment is still required.
$null = Invoke-GraphWithRetry PATCH "/applications/$($api.id)" @{
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
    $password = Invoke-Graph POST "/applications/$($web.id)/addPassword" @{
        passwordCredential = @{ displayName = "Entra MCP chat demo (azd $($env:AZURE_ENV_NAME))"; endDateTime = $expiry }
    }
    Set-AzdValue OIDC_CLIENT_SECRET $password.secretText
    Set-AzdValue OIDC_CLIENT_SECRET_KEY_ID $password.keyId
    Set-AzdValue OIDC_CLIENT_SECRET_EXPIRES_AT ([datetimeoffset]::Parse($password.endDateTime, [cultureinfo]::InvariantCulture).ToString("o", [cultureinfo]::InvariantCulture))
    $password = $null
    # Retire the previous key only after the new one is saved.
    if ($storedKeyId -and @($web.passwordCredentials | Where-Object { $_.keyId -eq $storedKeyId }).Count -eq 1) {
        $null = Invoke-Graph POST "/applications/$($web.id)/removePassword" @{ keyId = $storedKeyId }
    }
    Write-Host "Created a new web client secret (90 days)."
}
else {
    Write-Host "Reusing the existing web client secret (expires $($storedExpiry.ToString('u')))."
}

Set-AzdValue ENTRA_TENANT_ID $context.TenantId
Set-AzdValue OIDC_CLIENT_ID $web.appId
Set-AzdValue API_AUDIENCE $api.appId
Set-AzdValue API_APP_OBJECT_ID $api.id
Set-AzdValue WEB_APP_OBJECT_ID $web.id
Write-Host "Entra registrations ready: API $($api.appId), web $($web.appId). Access is assigned to the signed-in user."
