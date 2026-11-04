#Requires -Version 7.0
# azd postprovision hook: register the deployed app's URL as the web app's sign-in redirect URI.
. "$PSScriptRoot/common.ps1"

$null = Get-HookContext
foreach ($name in "WEB_URL", "WEB_APP_OBJECT_ID") {
    if (-not [Environment]::GetEnvironmentVariable($name)) {
        throw "$name is not set. Provisioning did not finish, or the preprovision hook did not run."
    }
}

$webUrl = $env:WEB_URL.TrimEnd("/")
$required = @("$webUrl/auth/callback", "$webUrl/")
$web = Invoke-Graph GET "/applications/$($env:WEB_APP_OBJECT_ID)?`$select=id,tags,web"
if ($web.tags -notcontains $script:OwnerTag) {
    throw "Web registration $($env:WEB_APP_OBJECT_ID) was not created by this demo. Refusing to change it."
}

# Keep any redirect URIs already registered and add the two the deployed app uses.
$uris = @($web.web.redirectUris) + $required | Where-Object { $_ } | Select-Object -Unique
$null = Invoke-Graph PATCH "/applications/$($web.id)" @{
    web = @{
        redirectUris = @($uris)
        implicitGrantSettings = @{ enableAccessTokenIssuance = $false; enableIdTokenIssuance = $false }
    }
}
Write-Host "Registered sign-in redirect URI $($required[0])."
Write-Host "Open $webUrl and sign in. Assign more users to the app in the Entra portal (Enterprise applications > Users and groups)."
