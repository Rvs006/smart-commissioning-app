[CmdletBinding()]
param(
    [string]$BaseUrl = 'http://127.0.0.1:8000',
    [string]$ProjectId,
    [string]$SiteId
)

<#!
.SYNOPSIS
Reads non-secret MQTT capture settings from a running local Smart Commissioning App.

.DESCRIPTION
Uses the app's read-only local Configuration API and prints only the values
needed to start the companion MQTT evidence collector. It intentionally never
reads or prints the MQTT username, password, private key, or certificate data.
#>

function Get-ConfigurationValue {
    param(
        [Parameter(Mandatory)] [object]$Values,
        [Parameter(Mandatory)] [string]$Name
    )

    $property = $Values.PSObject.Properties[$Name]
    if ($null -eq $property -or $null -eq $property.Value) {
        return ''
    }
    return [string]$property.Value
}

function Test-ConfiguredValue {
    param([string]$Value)

    return -not [string]::IsNullOrWhiteSpace($Value) -and $Value -notmatch '^\*+$'
}

$uri = "{0}/api/v1/configuration" -f $BaseUrl.TrimEnd('/')
$query = [System.Collections.Generic.List[string]]::new()
if (-not [string]::IsNullOrWhiteSpace($ProjectId)) {
    $query.Add('project_id=' + [uri]::EscapeDataString($ProjectId))
}
if (-not [string]::IsNullOrWhiteSpace($SiteId)) {
    $query.Add('site_id=' + [uri]::EscapeDataString($SiteId))
}
if ($query.Count -gt 0) {
    $uri += '?' + ($query -join '&')
}

try {
    $configuration = Invoke-RestMethod -Uri $uri -Method Get -Headers @{ Accept = 'application/json' } -ErrorAction Stop
}
catch {
    Write-Error (
        "Could not read the local app configuration at $uri. Keep the app open " +
        "and run this script on the same server. No credentials were requested or exposed. " +
        "Details: $($_.Exception.Message)"
    )
    exit 1
}

if ($null -eq $configuration.mqtt -or $null -eq $configuration.mqtt.values) {
    Write-Error 'The local app response did not contain an MQTT configuration section.'
    exit 1
}

$mqtt = $configuration.mqtt.values
$certificates = if ($null -ne $configuration.certificates) { $configuration.certificates.values } else { $null }

[pscustomobject]([ordered]@{
    'MQTT Broker Host' = Get-ConfigurationValue -Values $mqtt -Name 'MQTT Broker FQDN or IP Address'
    'Port' = Get-ConfigurationValue -Values $mqtt -Name 'Port'
    'TLS' = Get-ConfigurationValue -Values $mqtt -Name 'Use TLS'
    'QoS' = Get-ConfigurationValue -Values $mqtt -Name 'QoS'
    'Keep Alive Interval' = Get-ConfigurationValue -Values $mqtt -Name 'Keep Alive Interval'
    'Configured Client ID' = Get-ConfigurationValue -Values $mqtt -Name 'Client ID'
    'CA Certificate Present' = Test-ConfiguredValue (Get-ConfigurationValue -Values $certificates -Name 'CA Certificate')
    'Client Certificate Present' = Test-ConfiguredValue (Get-ConfigurationValue -Values $certificates -Name 'Client Certificate')
    'Private Key Present' = Test-ConfiguredValue (Get-ConfigurationValue -Values $certificates -Name 'Private Key')
}) | Format-List

Write-Host 'No username, password, certificate, or private-key content was read or printed.' -ForegroundColor Green
