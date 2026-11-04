#Requires -Version 7.0
# azd postdown hook: azd down removes the Azure resources but not Entra objects, so delete the two app
# registrations this deployment created. Only apps carrying the demo's owner tag are touched.
. "$PSScriptRoot/common.ps1"

$context = Get-HookContext

function Remove-DemoApp([string]$ObjectId, [string]$Name) {
    $app = $null
    if ($ObjectId) {
        try { $app = Invoke-Graph GET "/applications/$ObjectId?`$select=id,displayName,tags" }
        catch { $app = $null }
    }
    if (-not $app) {
        $app = @(Find-DemoApps $Name)[0]
    }
    if (-not $app) {
        Write-Host "No registration found for $Name; nothing to delete."
        return
    }
    if ($app.tags -notcontains $script:OwnerTag) {
        Write-Warning "$($app.displayName) was not created by this demo; leaving it in place."
        return
    }
    $null = Invoke-Graph DELETE "/applications/$($app.id)"
    Write-Host "Deleted app registration $($app.displayName)."
}

Remove-DemoApp $env:WEB_APP_OBJECT_ID $context.WebName
Remove-DemoApp $env:API_APP_OBJECT_ID $context.ApiName
Write-Host "Entra registrations removed. They stay in the Entra recycle bin for 30 days and can be restored there."
