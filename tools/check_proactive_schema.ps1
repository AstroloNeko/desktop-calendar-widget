# Offline contract checks using PowerShell's built-in Draft 2020-12 validator.
# Requires PowerShell 7.4+ and uses only local references.
$ErrorActionPreference = 'Stop'
$schemaPath = Join-Path $PSScriptRoot '../docs/proactive-notifications-v1.schema.json'
$schema = Get-Content -LiteralPath $schemaPath -Raw | ConvertFrom-Json -AsHashtable
$seed = @{
    protocol_version = 1; type = 'brief'; channel = 'qq'; account_id = 'synthetic'
    notification_id = 'a' * 64; private = $false; count = 0; items = @()
    triggered_at = '2031-04-16T08:00:00+08:00'; expires_at = '2031-04-16T12:00:00+08:00'
    fallback_text = 'Synthetic brief'; actions = @()
}
$script:checked = 0

function Check-Schema($Name, $Payload, [bool] $Expected, $Definition) {
    $validator = if ($Definition) {
        @{ '$schema' = $schema['$schema']; '$defs' = $schema['$defs']; '$ref' = ('#/$defs/' + $Definition) } |
            ConvertTo-Json -Depth 50 -Compress
    } else {
        Get-Content -LiteralPath $schemaPath -Raw
    }
    $json = ConvertTo-Json -InputObject $Payload -Depth 20 -Compress
    $result = Test-Json -Json $json -Schema $validator -ErrorAction SilentlyContinue
    if ([bool] $result -ne $Expected) { throw "Unexpected schema result for $Name" }
    $script:checked++
}

function Check-Brief($Name, $Changes, [bool] $Expected) {
    $payload = $seed.Clone()
    foreach ($key in $Changes.Keys) { $payload[$key] = $Changes[$key] }
    Check-Schema $Name $payload $Expected
}

Check-Brief 'legacy payload' @{} $true
Check-Brief 'morning' @{brief_context=@{slot='morning'}} $true
Check-Brief 'evening without stats' @{brief_context=@{slot='evening'}} $true
Check-Brief 'private evening' @{private=$true; brief_context=@{slot='evening'}} $true
Check-Brief 'private morning' @{private=$true; brief_context=@{slot='morning'}} $true
Check-Brief 'manual test brief' @{is_test=$true; brief_context=@{slot='evening'}} $true
$stats = @{total=5000; completed=5000}
Check-Brief 'maximum evening stats' @{brief_context=@{slot='evening'; completion=$stats}} $true
Check-Brief 'zero evening stats' @{brief_context=@{slot='evening'; completion=@{total=0; completed=0}}} $true
Check-Brief 'morning stats rejected' @{brief_context=@{slot='morning'; completion=$stats}} $false
Check-Brief 'private stats rejected' @{private=$true; brief_context=@{slot='evening'; completion=$stats}} $false
Check-Brief 'private items rejected' @{private=$true; count=1; items=@(@{index=1; title='Synthetic'})} $false
Check-Brief 'private counts rejected' @{private=$true; brief_counts=@{timed=0; untimed=0; unfinished=0; upcoming=0; overdue=0}} $false
foreach ($kind in 'event','ddl','test') {
    $fields = @{type=$kind; brief_context=@{slot='evening'}}
    if ($kind -eq 'test') { $fields.private=$true; $fields.notification_id='a'*32 }
    Check-Brief "$kind context rejected" $fields $false
}
foreach ($bad in @(
    @{slot='noon'}, @{completion=$stats}, @{slot='evening'; extra=1},
    @{slot='evening'; completion=@{total=3}}, @{slot='evening'; completion=@{total=5001; completed=1}},
    @{slot='evening'; completion=@{total=4; completed=-1}}, @{slot='evening'; completion=@{total=4; completed=1.5}},
    @{slot='evening'; completion=@{total=4; completed=$true}},
    @{slot='evening'; completion=@{total=4; completed=1; percent=25}}
)) {
    Check-Brief 'invalid context or count' @{brief_context=$bad} $false
}
$offer = 'notificationSessionBriefCapabilityRequest'
Check-Schema 'legacy session request' @{} $true $offer
Check-Schema 'session advertises version 1' @{brief_context_version=1} $true $offer
Check-Schema 'unknown version is outside the contract' @{brief_context_version=2} $false $offer
Check-Schema 'Boolean version rejected' @{brief_context_version=$true} $false $offer
$response = @{enabled=$true; protocol_version=1; endpoint='/api/notifications/proactive'; supported_actions=@()}
$capability = 'proactiveNotificationCapability'
Check-Schema 'legacy session response' $response $true $capability
$response.brief_context_version=1
Check-Schema 'negotiated session response' $response $true $capability
$response.enabled=$false
Check-Schema 'disabled response cannot echo capability' $response $false $capability
Write-Output "Schema cases passed: $script:checked"
