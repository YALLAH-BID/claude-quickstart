#requires -Version 5.1
<#
.SYNOPSIS
Deep-dig audit of a single Tableau Server site (PowerShell port of tools/tableau_deep_dig.py).

.DESCRIPTION
Runs from inside a corporate network against an internal Tableau Server
(often plain http) and inventories one site: projects (as a tree), workbooks,
views with usage statistics, data sources, users, groups, subscriptions,
schedules, extract refresh tasks, flows, and - optionally - per-item
connections, per-workbook permissions, and Metadata-API lineage.

Outputs (written to -Out, default 'tableau_dig_output'):
  - site_inventory.json  everything collected, raw
  - report.md            human-readable report

Uses only built-in .NET / PowerShell features; runs on stock Windows
PowerShell 5.1 and on PowerShell 7 - nothing to install.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File tools\tableau_deep_dig.ps1 `
    -Server http://tableau.example.com -Site Automotive `
    -PatName my-token -PatSecret abc123

.EXAMPLE
powershell -ExecutionPolicy Bypass -File tools\tableau_deep_dig.ps1 `
    -Server http://tableau.example.com -User jane.doe   # prompts for the password

.NOTES
Exit code is 0 even when individual sections fail (failures are recorded in
the report and JSON); 2 only when sign-in itself fails or no auth method was
provided. Credentials and the auth token never reach the output files.
#>
[CmdletBinding()]
param(
    [string]$Server,
    [string]$Site,
    [string]$User,
    [string]$Password,
    [string]$PatName,
    [string]$PatSecret,
    [switch]$Connections,
    [switch]$Permissions,
    [switch]$Lineage,
    [string]$Out = 'tableau_dig_output',
    [int]$TimeoutSec = 30,
    [int]$PageSize = 100,
    [string]$ApiVersion,
    [switch]$Insecure,
    [switch]$UseEnvProxy
)

# ---------------------------------------------------------------------------
# Constants (mirroring tableau_deep_dig.py)
# ---------------------------------------------------------------------------

$TOOL_NAME = 'tableau_deep_dig'
$NEGOTIATION_API_VERSION = '2.4'  # /serverinfo exists from API 2.4 (Tableau 10.1)
$FALLBACK_API_VERSION = '2.3'     # safe floor for pre-10.1 servers
$MAX_PAGES = 10000                # hard stop for paging loops
$STALE_DAYS = 180
$ACTIVE_DAYS = 90
$TOP_VIEWS = 25
$TABLE_CAP = 500                  # report tables are capped; the JSON always has everything

# The source file is pure ASCII so Windows PowerShell 5.1 reads it correctly
# regardless of BOM; non-ASCII output characters are built explicitly.
$EmDash = [string][char]0x2014    # em dash
$Ellipsis = [string][char]0x2026  # horizontal ellipsis

$LINEAGE_QUERY = @'
query tableauDeepDigLineage {
  workbooks {
    name
    projectName
    upstreamDatasources {
      name
    }
    upstreamTables {
      name
      schema
      database {
        name
        connectionType
      }
    }
  }
}
'@

# Client state (script-scoped; set during the run)
$script:ServerUrl = ''
$script:RestApiVersion = $FALLBACK_API_VERSION
$script:Token = $null
$script:SiteId = $null
$script:AuthSiteContentUrl = ''
$script:UserId = $null
$script:ServerInfo = [ordered]@{}
$script:Sections = [ordered]@{}
$script:ErrorsList = New-Object System.Collections.Generic.List[object]
$script:TimeoutMs = 30000
$script:SitePrefix = ''
$script:UseEnvProxyFlag = $false

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

function Write-Log {
    # Progress and diagnostics go to stderr; stdout stays clean.
    param([string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Parse-IsoDate {
    # Parse an ISO-8601 timestamp defensively. Returns a UTC DateTime or $null.
    param($Value)
    if ($null -eq $Value) { return $null }
    $text = ([string]$Value).Trim()
    if (-not $text) { return $null }
    $dto = [System.DateTimeOffset]::MinValue
    $culture = [System.Globalization.CultureInfo]::InvariantCulture
    $styles = [System.Globalization.DateTimeStyles]::AssumeUniversal
    if ([System.DateTimeOffset]::TryParse($text, $culture, $styles, [ref]$dto)) {
        return $dto.UtcDateTime
    }
    $m = [regex]::Match($text, '^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})')
    if (-not $m.Success) { return $null }
    try {
        return (New-Object DateTime -ArgumentList @(
                [int]$m.Groups[1].Value, [int]$m.Groups[2].Value, [int]$m.Groups[3].Value,
                [int]$m.Groups[4].Value, [int]$m.Groups[5].Value, [int]$m.Groups[6].Value,
                ([System.DateTimeKind]::Utc)))
    } catch {
        # out-of-range components, e.g. "0000-00-00"
        return $null
    }
}

function Get-VersionParts {
    param($Version)
    $parts = New-Object System.Collections.Generic.List[int]
    foreach ($chunk in ([string]$Version).Split('.')) {
        $n = 0
        if ([int]::TryParse($chunk, [ref]$n)) { $parts.Add($n) } else { $parts.Add(0) }
    }
    if ($parts.Count -eq 0) { $parts.Add(0) }
    return ,$parts.ToArray()
}

function Compare-VersionParts {
    # Compare two int arrays like Python tuples: -1 / 0 / 1
    param($A, $B)
    $len = [Math]::Max($A.Count, $B.Count)
    for ($i = 0; $i -lt $len; $i++) {
        $av = 0
        if ($i -lt $A.Count) { $av = [int]$A[$i] }
        $bv = 0
        if ($i -lt $B.Count) { $bv = [int]$B[$i] }
        if ($av -lt $bv) { return -1 }
        if ($av -gt $bv) { return 1 }
    }
    return 0
}

# ---------------------------------------------------------------------------
# XML helpers (namespace-agnostic: Tableau responses live in the
# http://tableau.com/api default namespace, so match on LocalName only)
# ---------------------------------------------------------------------------

function ConvertTo-XmlDocument {
    param([string]$Text)
    try {
        $doc = New-Object System.Xml.XmlDocument
        $doc.XmlResolver = $null  # never fetch external DTDs/entities
        $doc.LoadXml($Text)
        return $doc
    } catch {
        $snippet = $Text
        if ($null -eq $snippet) { $snippet = '' }
        if ($snippet.Length -gt 120) { $snippet = $snippet.Substring(0, 120) }
        throw ('unparseable XML response: ' + $_.Exception.Message + " (response starts: '" + $snippet + "')")
    }
}

function Get-ChildElement {
    # First DIRECT child element with the given local name (like ET find()).
    param($Node, [string]$Name)
    if ($null -eq $Node) { return $null }
    foreach ($childNode in $Node.ChildNodes) {
        if ($childNode.NodeType -eq 'Element' -and $childNode.LocalName -eq $Name) {
            return $childNode
        }
    }
    return $null
}

function Get-ElementsByLocal {
    # Every descendant element with the given local name (like ET iter()).
    param($Node, [string]$Name)
    $found = New-Object System.Collections.Generic.List[object]
    if ($null -ne $Node) {
        foreach ($el in $Node.GetElementsByTagName('*')) {
            if ($el.LocalName -eq $Name) { $found.Add($el) }
        }
    }
    return ,$found.ToArray()
}

function Get-DirectText {
    # Text content before the first child element (like ElementTree .text).
    param($Node)
    foreach ($childNode in $Node.ChildNodes) {
        if ($childNode.NodeType -eq 'Text') {
            $textValue = [string]$childNode.Value
            return $textValue.Trim()
        }
    }
    return ''
}

function Get-TagLabels {
    param($TagsEl)
    $labels = New-Object System.Collections.Generic.List[object]
    foreach ($tagNode in $TagsEl.ChildNodes) {
        if ($tagNode.NodeType -eq 'Element' -and $tagNode.LocalName -eq 'tag') {
            $labels.Add([string]$tagNode.GetAttribute('label'))
        }
    }
    return ,$labels.ToArray()
}

function Test-IsXmlnsAttr {
    param($Attr)
    if ($Attr.Prefix -eq 'xmlns') { return $true }
    if ($Attr.Name -eq 'xmlns') { return $true }
    return $false
}

function ConvertTo-ItemDict {
    <#
    Flatten one list item: all attributes plus well-known children.
    Children like project, owner, usage become attribute dicts (with one extra
    level for grandchildren, e.g. task/extractRefresh/schedule); tags becomes
    a list of tag labels. Mirrors item_to_dict() in the Python tool.
    #>
    param($Element)
    $record = [ordered]@{}
    foreach ($attr in $Element.Attributes) {
        if (Test-IsXmlnsAttr $attr) { continue }
        $record[$attr.LocalName] = $attr.Value
    }
    foreach ($child in $Element.ChildNodes) {
        if ($child.NodeType -ne 'Element') { continue }
        $tag = $child.LocalName
        if ($tag -eq 'tags') {
            $record['tags'] = Get-TagLabels $child
            continue
        }
        $value = [ordered]@{}
        foreach ($attr in $child.Attributes) {
            if (Test-IsXmlnsAttr $attr) { continue }
            $value[$attr.LocalName] = $attr.Value
        }
        foreach ($sub in $child.ChildNodes) {
            if ($sub.NodeType -ne 'Element') { continue }
            $subTag = $sub.LocalName
            if ($subTag -eq 'tags') {
                $value['tags'] = Get-TagLabels $sub
                continue
            }
            $subHasAttrs = $false
            foreach ($attr in $sub.Attributes) {
                if (Test-IsXmlnsAttr $attr) { continue }
                $subHasAttrs = $true
                break
            }
            if ($subHasAttrs -and -not $value.Contains($subTag)) {
                $subDict = [ordered]@{}
                foreach ($attr in $sub.Attributes) {
                    if (Test-IsXmlnsAttr $attr) { continue }
                    $subDict[$attr.LocalName] = $attr.Value
                }
                $value[$subTag] = $subDict
            } else {
                $subText = Get-DirectText $sub
                if ($subText -and -not $value.Contains($subTag)) {
                    $value[$subTag] = $subText
                }
            }
        }
        if ($value.Count -eq 0) {
            $childText = Get-DirectText $child
            if ($childText) { $record[$tag] = $childText } else { $record[$tag] = $value }
        } else {
            $record[$tag] = $value
        }
    }
    return $record
}

function Format-TsError {
    param($ErrorEl)
    $code = [string]$ErrorEl.GetAttribute('code')
    if (-not $code) { $code = '?' }
    $summary = ''
    $detail = ''
    foreach ($childNode in $ErrorEl.ChildNodes) {
        if ($childNode.NodeType -ne 'Element') { continue }
        if ($childNode.LocalName -eq 'summary') { $summary = ([string]$childNode.InnerText).Trim() }
        if ($childNode.LocalName -eq 'detail') { $detail = ([string]$childNode.InnerText).Trim() }
    }
    $message = 'Tableau error code ' + $code
    if ($summary) { $message = $message + ': ' + $summary }
    if ($detail) { $message = $message + ' ' + $EmDash + ' ' + $detail }
    return $message
}

function Get-TsErrorMessage {
    # Best-effort parse of a Tableau error XML body; $null when not one.
    param([string]$BodyText)
    if (-not $BodyText) { return $null }
    $doc = $null
    try {
        $doc = New-Object System.Xml.XmlDocument
        $doc.XmlResolver = $null  # never fetch external DTDs/entities
        $doc.LoadXml($BodyText)
    } catch {
        return $null
    }
    $errorEl = Get-ChildElement $doc.DocumentElement 'error'
    if ($null -eq $errorEl) { return $null }
    return Format-TsError $errorEl
}

# ---------------------------------------------------------------------------
# HTTP layer - System.Net.HttpWebRequest directly, so behavior is identical
# on Windows PowerShell 5.1 and PowerShell 7 (Invoke-WebRequest is not).
# ---------------------------------------------------------------------------

function Invoke-TableauRequest {
    # One HTTP request; idempotent GETs are retried once on transport errors.
    # HTTP status errors (403/404/...) throw with the Tableau error detail and
    # are NOT retried - callers catch them per section.
    param(
        [string]$Method,
        [string]$Url,
        $Body = $null,
        $Headers = $null,
        $ContentType = $null,
        $Accept = $null
    )
    $attempts = 1
    if ($Method -eq 'GET' -and $null -eq $Body) { $attempts = 2 }
    $lastError = 'unknown error'
    for ($attempt = 1; $attempt -le $attempts; $attempt++) {
        $request = [System.Net.HttpWebRequest]::Create($Url)
        $request.Method = $Method
        $request.Timeout = $script:TimeoutMs
        $request.ReadWriteTimeout = $script:TimeoutMs
        if ($script:UseEnvProxyFlag) {
            # Match the Python tool: explicit HTTP(S)_PROXY env vars win (on
            # 5.1/.NET Framework GetSystemWebProxy ignores them); the system
            # proxy, with default credentials, is only the fallback.
            $envProxy = $env:HTTPS_PROXY
            if (-not $envProxy) { $envProxy = $env:HTTP_PROXY }
            if ($envProxy) {
                $request.Proxy = New-Object System.Net.WebProxy($envProxy)
            } else {
                $proxy = [System.Net.WebRequest]::GetSystemWebProxy()
                $proxy.Credentials = [System.Net.CredentialCache]::DefaultCredentials
                $request.Proxy = $proxy
            }
        } else {
            # Corporate proxies usually cannot reach intranet hosts, so the
            # default is to bypass all proxies entirely.
            $request.Proxy = $null
        }
        if ($script:Token) { $request.Headers['X-Tableau-Auth'] = $script:Token }
        if ($Headers) {
            foreach ($key in $Headers.Keys) { $request.Headers[$key] = [string]$Headers[$key] }
        }
        if ($Accept) { $request.Accept = [string]$Accept }
        $response = $null
        try {
            if ($null -ne $Body) {
                if ($ContentType) { $request.ContentType = [string]$ContentType }
                $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$Body)
                $request.ContentLength = $bytes.Length
                $requestStream = $request.GetRequestStream()
                try { $requestStream.Write($bytes, 0, $bytes.Length) } finally { $requestStream.Close() }
            } elseif ($Method -ne 'GET') {
                $request.ContentLength = 0
            }
            $response = $request.GetResponse()
            $reader = New-Object System.IO.StreamReader -ArgumentList @($response.GetResponseStream(), [System.Text.Encoding]::UTF8)
            try { $text = $reader.ReadToEnd() } finally { $reader.Close() }
            return $text
        } catch [System.Net.WebException] {
            $webEx = $_.Exception
            if ($null -ne $webEx.Response) {
                # An HTTP status error (e.g. 403): read the Tableau error XML
                # from the response body and throw a descriptive message.
                $status = 0
                try { $status = [int]$webEx.Response.StatusCode } catch { $status = 0 }
                $errorBody = ''
                try {
                    $errReader = New-Object System.IO.StreamReader -ArgumentList @($webEx.Response.GetResponseStream(), [System.Text.Encoding]::UTF8)
                    try { $errorBody = $errReader.ReadToEnd() } finally { $errReader.Close() }
                } catch { $errorBody = '' }
                try { $webEx.Response.Close() } catch { }
                $detail = Get-TsErrorMessage $errorBody
                if (-not $detail) {
                    $snippet = $errorBody
                    if ($snippet.Length -gt 200) { $snippet = $snippet.Substring(0, 200) }
                    $detail = $snippet.Trim()
                }
                if (-not $detail) { $detail = [string]$webEx.Message }
                throw ('HTTP ' + $status + ' for ' + $Method + ' ' + $Url + ': ' + $detail)
            }
            # Transport-level failure (timeout, connection refused/reset ...)
            $lastError = [string]$webEx.Message
            if ($attempt -lt $attempts) {
                Write-Log ('    retrying ' + $Method + ' ' + $Url + ' after: ' + $lastError)
            }
        } catch [System.IO.IOException] {
            $lastError = [string]$_.Exception.Message
            if ($attempt -lt $attempts) {
                Write-Log ('    retrying ' + $Method + ' ' + $Url + ' after: ' + $lastError)
            }
        } finally {
            if ($null -ne $response) {
                try { $response.Close() } catch { }
            }
        }
    }
    throw ($Method + ' ' + $Url + ' failed after ' + $attempts + " attempt(s): " + $lastError)
}

function Get-TableauXml {
    # GET /api/{version}/{path} and return the parsed tsResponse document.
    param([string]$Path, $Params = $null)
    $url = $script:ServerUrl + '/api/' + $script:RestApiVersion + '/' + $Path
    if ($Params -and $Params.Count -gt 0) {
        $pairs = New-Object System.Collections.Generic.List[string]
        foreach ($key in $Params.Keys) {
            $pairs.Add([uri]::EscapeDataString([string]$key) + '=' + [uri]::EscapeDataString([string]$Params[$key]))
        }
        $url = $url + '?' + ($pairs -join '&')
    }
    $text = Invoke-TableauRequest -Method 'GET' -Url $url
    $doc = ConvertTo-XmlDocument $text
    $errorEl = Get-ChildElement $doc.DocumentElement 'error'
    if ($null -ne $errorEl) { throw (Format-TsError $errorEl) }
    return $doc
}

function Get-PagedItems {
    <#
    Collect every page of a paged listing endpoint. Guards against
    totalAvailable=0 and against servers that ignore paging (stops when a page
    repeats or comes back empty); hard page cap MAX_PAGES.
    #>
    param([string]$Path, [string]$ItemTag, $Params = $null, [int]$Size = 100)
    $items = New-Object System.Collections.Generic.List[object]
    $previousIds = $null
    $pageNumber = 1
    while ($pageNumber -le $MAX_PAGES) {
        $query = [ordered]@{ pageSize = $Size; pageNumber = $pageNumber }
        if ($Params) {
            foreach ($key in $Params.Keys) { $query[$key] = $Params[$key] }
        }
        $doc = Get-TableauXml -Path $Path -Params $query
        $pageItems = New-Object System.Collections.Generic.List[object]
        $itemEls = Get-ElementsByLocal $doc.DocumentElement $ItemTag
        foreach ($el in $itemEls) {
            $pageItems.Add((ConvertTo-ItemDict $el))
        }
        $total = 0
        $paginationEl = Get-ChildElement $doc.DocumentElement 'pagination'
        if ($null -ne $paginationEl) {
            $parsedTotal = 0
            if ([int]::TryParse([string]$paginationEl.GetAttribute('totalAvailable'), [ref]$parsedTotal)) {
                $total = $parsedTotal
            }
        }
        if ($pageItems.Count -eq 0) { break }
        $idParts = New-Object System.Collections.Generic.List[string]
        foreach ($entry in $pageItems) { $idParts.Add([string]$entry['id']) }
        $ids = $idParts -join "`n"
        if ($null -ne $previousIds -and $ids -eq $previousIds) {
            break  # server ignored pageNumber; avoid double-counting
        }
        $previousIds = $ids
        foreach ($entry in $pageItems) { $items.Add($entry) }
        if ($total -le 0 -or $items.Count -ge $total) { break }
        $pageNumber++
    }
    return ,$items.ToArray()
}

function Invoke-Negotiate {
    # Discover product/REST versions via /serverinfo; degrade gracefully.
    param($Override = $null)
    $info = [ordered]@{}
    try {
        $url = $script:ServerUrl + '/api/' + $NEGOTIATION_API_VERSION + '/serverinfo'
        $text = Invoke-TableauRequest -Method 'GET' -Url $url
        $doc = ConvertTo-XmlDocument $text
        $errorEl = Get-ChildElement $doc.DocumentElement 'error'
        if ($null -ne $errorEl) { throw (Format-TsError $errorEl) }
        $serverInfoEl = Get-ChildElement $doc.DocumentElement 'serverInfo'
        if ($null -ne $serverInfoEl) {
            $productEl = Get-ChildElement $serverInfoEl 'productVersion'
            $restEl = Get-ChildElement $serverInfoEl 'restApiVersion'
            if ($null -ne $productEl) {
                $info['product_version'] = ([string]$productEl.InnerText).Trim()
                $buildAttr = [string]$productEl.GetAttribute('build')
                if ($buildAttr) { $info['build'] = $buildAttr }
            }
            if ($null -ne $restEl) {
                $restText = ([string]$restEl.InnerText).Trim()
                if ($restText) { $info['rest_api_version'] = $restText }
            }
        }
    } catch {
        $info['negotiation_error'] = [string]$_.Exception.Message
    }
    if ($Override) {
        $script:RestApiVersion = [string]$Override
    } elseif ($info.Contains('rest_api_version')) {
        $script:RestApiVersion = [string]$info['rest_api_version']
    } else {
        $script:RestApiVersion = $FALLBACK_API_VERSION
    }
    $info['negotiated_api_version'] = $script:RestApiVersion
    $script:ServerInfo = $info
    return $info
}

function Invoke-SignIn {
    param(
        [string]$SiteContentUrl,
        $UserName = $null,
        $UserPassword = $null,
        $TokenName = $null,
        $TokenSecret = $null
    )
    $doc = New-Object System.Xml.XmlDocument
    $tsRequest = $doc.CreateElement('tsRequest')
    $credentialsEl = $doc.CreateElement('credentials')
    if ($TokenName -and $TokenSecret) {
        $credentialsEl.SetAttribute('personalAccessTokenName', [string]$TokenName)
        $credentialsEl.SetAttribute('personalAccessTokenSecret', [string]$TokenSecret)
    } else {
        $credentialsEl.SetAttribute('name', [string]$UserName)
        $credentialsEl.SetAttribute('password', [string]$UserPassword)
    }
    $siteEl = $doc.CreateElement('site')
    $siteEl.SetAttribute('contentUrl', [string]$SiteContentUrl)
    $null = $credentialsEl.AppendChild($siteEl)
    $null = $tsRequest.AppendChild($credentialsEl)
    $null = $doc.AppendChild($tsRequest)
    $body = $doc.OuterXml

    $url = $script:ServerUrl + '/api/' + $script:RestApiVersion + '/auth/signin'
    $text = Invoke-TableauRequest -Method 'POST' -Url $url -Body $body -ContentType 'application/xml'
    $respDoc = ConvertTo-XmlDocument $text
    $errorEl = Get-ChildElement $respDoc.DocumentElement 'error'
    if ($null -ne $errorEl) { throw (Format-TsError $errorEl) }
    $credsEl = Get-ChildElement $respDoc.DocumentElement 'credentials'
    $tokenValue = $null
    if ($null -ne $credsEl) { $tokenValue = [string]$credsEl.GetAttribute('token') }
    if (-not $tokenValue) { throw 'sign-in response did not include an auth token' }
    $script:Token = $tokenValue
    $siteRespEl = Get-ChildElement $credsEl 'site'
    if ($null -ne $siteRespEl) {
        $script:SiteId = [string]$siteRespEl.GetAttribute('id')
        $script:AuthSiteContentUrl = [string]$siteRespEl.GetAttribute('contentUrl')
    }
    $userEl = Get-ChildElement $credsEl 'user'
    if ($null -ne $userEl) { $script:UserId = [string]$userEl.GetAttribute('id') }
    if (-not $script:SiteId) { throw 'sign-in response did not include a site id' }
}

function Invoke-SignOut {
    # Best-effort sign-out; never throws.
    if (-not $script:Token) { return }
    $url = $script:ServerUrl + '/api/' + $script:RestApiVersion + '/auth/signout'
    try { $null = Invoke-TableauRequest -Method 'POST' -Url $url -Body '' } catch { }
    $script:Token = $null
}

function Invoke-TableauGraphQL {
    # POST a query to the Metadata API (Tableau 2019.3+).
    param([string]$Query)
    $url = $script:ServerUrl + '/api/metadata/graphql'
    $body = ConvertTo-Json -InputObject @{ query = $Query } -Depth 5 -Compress
    $text = Invoke-TableauRequest -Method 'POST' -Url $url -Body $body -ContentType 'application/json' -Accept 'application/json'
    try {
        return ConvertFrom-Json -InputObject $text
    } catch {
        throw ('Metadata API returned a non-JSON response: ' + $_.Exception.Message)
    }
}

# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

function Record-Error {
    param([string]$SectionName, $Message)
    $entry = [ordered]@{ section = $SectionName; error = [string]$Message }
    $script:ErrorsList.Add($entry)
    Write-Log ('[!] ' + $SectionName + ': ' + [string]$Message)
}

function Invoke-Section {
    # Fetch one section; a failure (403/404/old version) records an error
    # entry, stores $null, and the dig continues.
    param([string]$SectionName, [scriptblock]$Func, $MinVersion = $null)
    if ($null -ne $MinVersion) {
        $current = Get-VersionParts $script:RestApiVersion
        if ((Compare-VersionParts $current $MinVersion) -lt 0) {
            $needed = ($MinVersion -join '.')
            Record-Error $SectionName ('skipped: requires REST API ' + $needed + '+ (negotiated ' + $script:RestApiVersion + ')')
            $script:Sections[$SectionName] = $null
            return $null
        }
    }
    Write-Log ('[.] fetching ' + $SectionName + ' ...')
    $result = $null
    $failed = $false
    try {
        $result = & $Func
    } catch {
        Record-Error $SectionName $_.Exception.Message
        $failed = $true
    }
    if ($failed) {
        $script:Sections[$SectionName] = $null
        return $null
    }
    $script:Sections[$SectionName] = $result
    $count = '?'
    if ($result -is [array]) { $count = [string]$result.Count }
    Write-Log ('[+] ' + $SectionName + ': ' + $count + ' item(s)')
    if ($null -eq $result) { return $null }
    return ,$result
}

function Get-ItemConnections {
    # N+1 fetch of /.../{id}/connections for workbooks or datasources.
    param($Items, [string]$Collection, [string]$SectionName)
    if ($null -eq $Items) {
        Record-Error $SectionName ('skipped: ' + $Collection + ' list unavailable')
        return $null
    }
    $itemArr = @($Items)
    Write-Log ('[.] fetching ' + $SectionName + ' for ' + $itemArr.Count + ' item(s) ...')
    $results = [ordered]@{}
    $failures = New-Object System.Collections.Generic.List[string]
    foreach ($item in $itemArr) {
        $itemId = [string]$item['id']
        if (-not $itemId) { continue }
        $path = $script:SitePrefix + '/' + $Collection + '/' + $itemId + '/connections'
        $doc = $null
        try {
            $doc = Get-TableauXml -Path $path
        } catch {
            $label = [string]$item['name']
            if (-not $label) { $label = $itemId }
            $failures.Add($label + ': ' + $_.Exception.Message)
            continue
        }
        $connList = New-Object System.Collections.Generic.List[object]
        $connEls = Get-ElementsByLocal $doc.DocumentElement 'connection'
        foreach ($el in $connEls) {
            $connList.Add((ConvertTo-ItemDict $el))
        }
        $results[$itemId] = $connList.ToArray()
    }
    if ($failures.Count -gt 0) {
        $previewItems = New-Object System.Collections.Generic.List[string]
        for ($i = 0; $i -lt [Math]::Min(3, $failures.Count); $i++) { $previewItems.Add($failures[$i]) }
        $preview = $previewItems -join '; '
        Record-Error $SectionName ([string]$failures.Count + ' of ' + $itemArr.Count + ' item(s) failed (e.g. ' + $preview + ')')
    }
    Write-Log ('[+] ' + $SectionName + ': connections for ' + $results.Count + ' item(s)')
    return $results
}

function Get-WorkbookPermissions {
    # N+1 fetch of per-workbook permissions (usually needs admin).
    param($Workbooks)
    $sectionName = 'workbook_permissions'
    if ($null -eq $Workbooks) {
        Record-Error $sectionName 'skipped: workbook list unavailable'
        return $null
    }
    $wbArr = @($Workbooks)
    Write-Log ('[.] fetching ' + $sectionName + ' for ' + $wbArr.Count + ' workbook(s) ...')
    $results = [ordered]@{}
    $failures = New-Object System.Collections.Generic.List[string]
    foreach ($workbook in $wbArr) {
        $workbookId = [string]$workbook['id']
        if (-not $workbookId) { continue }
        $path = $script:SitePrefix + '/workbooks/' + $workbookId + '/permissions'
        $doc = $null
        try {
            $doc = Get-TableauXml -Path $path
        } catch {
            $label = [string]$workbook['name']
            if (-not $label) { $label = $workbookId }
            $failures.Add($label + ': ' + $_.Exception.Message)
            continue
        }
        $grants = New-Object System.Collections.Generic.List[object]
        $granteeEls = Get-ElementsByLocal $doc.DocumentElement 'granteeCapabilities'
        foreach ($granteeEl in $granteeEls) {
            $entry = [ordered]@{}
            $groupEl = Get-ChildElement $granteeEl 'group'
            $userEl = Get-ChildElement $granteeEl 'user'
            if ($null -ne $groupEl) {
                $grantee = [ordered]@{ type = 'group' }
                foreach ($attr in $groupEl.Attributes) {
                    if (Test-IsXmlnsAttr $attr) { continue }
                    $grantee[$attr.LocalName] = $attr.Value
                }
                $entry['grantee'] = $grantee
            } elseif ($null -ne $userEl) {
                $grantee = [ordered]@{ type = 'user' }
                foreach ($attr in $userEl.Attributes) {
                    if (Test-IsXmlnsAttr $attr) { continue }
                    $grantee[$attr.LocalName] = $attr.Value
                }
                $entry['grantee'] = $grantee
            }
            $caps = New-Object System.Collections.Generic.List[object]
            $capEls = Get-ElementsByLocal $granteeEl 'capability'
            foreach ($capEl in $capEls) {
                $capDict = [ordered]@{}
                foreach ($attr in $capEl.Attributes) {
                    if (Test-IsXmlnsAttr $attr) { continue }
                    $capDict[$attr.LocalName] = $attr.Value
                }
                $caps.Add($capDict)
            }
            $entry['capabilities'] = $caps.ToArray()
            $grants.Add($entry)
        }
        $results[$workbookId] = $grants.ToArray()
    }
    if ($failures.Count -gt 0) {
        $previewItems = New-Object System.Collections.Generic.List[string]
        for ($i = 0; $i -lt [Math]::Min(3, $failures.Count); $i++) { $previewItems.Add($failures[$i]) }
        $preview = $previewItems -join '; '
        Record-Error $sectionName ([string]$failures.Count + ' of ' + $wbArr.Count + ' workbook(s) failed (e.g. ' + $preview + ')')
    }
    Write-Log ('[+] ' + $sectionName + ': permissions for ' + $results.Count + ' workbook(s)')
    return $results
}

function Collect-Inventory {
    # Fetch every section; each is individually wrapped so one failure
    # records an error entry and the dig continues.
    $script:SitePrefix = 'sites/' + $script:SiteId

    $null = Invoke-Section 'projects' {
        Get-PagedItems -Path ($script:SitePrefix + '/projects') -ItemTag 'project' -Size $script:PageSize
    }
    $workbooks = Invoke-Section 'workbooks' {
        Get-PagedItems -Path ($script:SitePrefix + '/workbooks') -ItemTag 'workbook' -Size $script:PageSize
    }
    $null = Invoke-Section 'views' {
        Get-PagedItems -Path ($script:SitePrefix + '/views') -ItemTag 'view' -Params ([ordered]@{ includeUsageStatistics = 'true' }) -Size $script:PageSize
    }
    $datasources = Invoke-Section 'datasources' {
        Get-PagedItems -Path ($script:SitePrefix + '/datasources') -ItemTag 'datasource' -Size $script:PageSize
    }
    $null = Invoke-Section 'users' {
        Get-PagedItems -Path ($script:SitePrefix + '/users') -ItemTag 'user' -Size $script:PageSize
    }
    $null = Invoke-Section 'groups' {
        Get-PagedItems -Path ($script:SitePrefix + '/groups') -ItemTag 'group' -Size $script:PageSize
    }
    $null = Invoke-Section 'subscriptions' {
        Get-PagedItems -Path ($script:SitePrefix + '/subscriptions') -ItemTag 'subscription' -Size $script:PageSize
    }
    # Server-level; needs server admin - expect 403 for most analysts.
    $null = Invoke-Section 'schedules' {
        Get-PagedItems -Path 'schedules' -ItemTag 'schedule' -Size $script:PageSize
    }
    # Site-level list of extract refresh tasks; admin only - expect 403.
    $null = Invoke-Section 'extract_refresh_tasks' {
        Get-PagedItems -Path ($script:SitePrefix + '/tasks/extractRefreshes') -ItemTag 'task' -Size $script:PageSize
    } -MinVersion @(2, 6)
    $null = Invoke-Section 'flows' {
        Get-PagedItems -Path ($script:SitePrefix + '/flows') -ItemTag 'flow' -Size $script:PageSize
    } -MinVersion @(3, 3)

    if ($Connections) {
        $script:Sections['workbook_connections'] = Get-ItemConnections $workbooks 'workbooks' 'workbook_connections'
        $script:Sections['datasource_connections'] = Get-ItemConnections $datasources 'datasources' 'datasource_connections'
    }

    if ($Permissions) {
        $script:Sections['workbook_permissions'] = Get-WorkbookPermissions $workbooks
    }

    if ($Lineage) {
        Write-Log '[.] querying the Metadata API for lineage ...'
        $payload = $null
        $lineageFailed = $false
        try {
            $payload = Invoke-TableauGraphQL $LINEAGE_QUERY
        } catch {
            Record-Error 'lineage' ('Metadata API unavailable: ' + $_.Exception.Message)
            $script:Sections['lineage'] = $null
            $lineageFailed = $true
        }
        if (-not $lineageFailed) {
            $gqlErrors = $null
            try { $gqlErrors = $payload.errors } catch { $gqlErrors = $null }
            if ($gqlErrors) {
                $shown = ConvertTo-Json -InputObject $gqlErrors -Depth 10 -Compress
                if ($shown.Length -gt 300) { $shown = $shown.Substring(0, 300) }
                Record-Error 'lineage' ('GraphQL errors: ' + $shown)
            }
            $lineageData = $null
            try { $lineageData = $payload.data } catch { $lineageData = $null }
            $script:Sections['lineage'] = $lineageData
            $nodeCount = 0
            if ($null -ne $lineageData) {
                $wbNodes = $null
                try { $wbNodes = $lineageData.workbooks } catch { $wbNodes = $null }
                if ($null -ne $wbNodes) { $nodeCount = @($wbNodes).Count }
            }
            Write-Log ('[+] lineage: ' + $nodeCount + ' workbook node(s)')
        }
    }
}

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

function Escape-Md {
    param($Value)
    $text = ''
    if ($null -ne $Value) { $text = [string]$Value }
    return $text.Replace('|', '\|').Replace("`n", ' ').Trim()
}

function Add-TableLines {
    param($Lines, $Headers, $Rows)
    $Lines.Add('| ' + ($Headers -join ' | ') + ' |')
    $dashes = New-Object System.Collections.Generic.List[string]
    foreach ($h in $Headers) { $dashes.Add('---') }
    $Lines.Add('| ' + ($dashes -join ' | ') + ' |')
    foreach ($row in $Rows) {
        $cells = New-Object System.Collections.Generic.List[string]
        foreach ($cell in $row) { $cells.Add((Escape-Md $cell)) }
        $Lines.Add('| ' + ($cells -join ' | ') + ' |')
    }
}

function Format-DateCell {
    param($Value)
    $parsed = Parse-IsoDate $Value
    if ($null -ne $parsed) {
        return $parsed.ToString(
            'yyyy-MM-dd', [System.Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value) { return [string]$Value }
    return ''
}

function Get-SortDate {
    param($Value)
    $parsed = Parse-IsoDate $Value
    if ($null -ne $parsed) { return $parsed }
    return (New-Object DateTime -ArgumentList @(1970, 1, 1, 0, 0, 0, ([System.DateTimeKind]::Utc)))
}

function Get-ChildId {
    param($Record, [string]$Child)
    $value = $Record[$Child]
    if ($value -is [System.Collections.IDictionary]) {
        $idVal = $value['id']
        if ($idVal) { return [string]$idVal }
    }
    return ''
}

function Get-ChildName {
    param($Record, [string]$Child, $IdToName)
    $value = $Record[$Child]
    if ($value -is [System.Collections.IDictionary]) {
        $nameVal = $value['name']
        if ($nameVal) { return [string]$nameVal }
        $idVal = [string]$value['id']
        if ($idVal -and $IdToName.Contains($idVal)) {
            $mapped = [string]$IdToName[$idVal]
            if ($mapped) { return $mapped }
        }
        if ($idVal) { return $idVal }
        return ''
    }
    return ''
}

function Get-UsageCount {
    param($View)
    $usage = $View['usage']
    if ($usage -is [System.Collections.IDictionary]) {
        $n = 0
        if ([int]::TryParse([string]$usage['totalViewCount'], [ref]$n)) { return $n }
        return 0
    }
    return 0
}

function Walk-ProjectNode {
    param($Node, [int]$Depth, $Children, $Lines, $Seen, $WorkbookCounts, $DatasourceCounts)
    $nodeId = [string]$Node['id']
    if ($Seen.Contains($nodeId)) { return }  # cycle guard
    $null = $Seen.Add($nodeId)
    $indent = '  ' * $Depth
    $nameVal = $Node['name']
    if (-not $nameVal) { $nameVal = '(unnamed project)' }
    $name = Escape-Md $nameVal
    $wbCount = 0
    if ($WorkbookCounts.ContainsKey($nodeId)) { $wbCount = [int]$WorkbookCounts[$nodeId] }
    $dsCount = 0
    if ($DatasourceCounts.ContainsKey($nodeId)) { $dsCount = [int]$DatasourceCounts[$nodeId] }
    $Lines.Add($indent + '- **' + $name + '** (workbooks: ' + $wbCount + ', datasources: ' + $dsCount + ')')
    $kids = @()
    if ($Children.ContainsKey($nodeId)) { $kids = $Children[$nodeId].ToArray() }
    foreach ($kid in @($kids | Sort-Object { ([string]$_['name']).ToLower() })) {
        Walk-ProjectNode $kid ($Depth + 1) $Children $Lines $Seen $WorkbookCounts $DatasourceCounts
    }
}

function Get-ProjectTreeLines {
    param($Projects, $Workbooks, $Datasources)
    $workbookCounts = @{}
    foreach ($wb in @($Workbooks)) {
        if ($null -eq $wb) { continue }
        $projId = Get-ChildId $wb 'project'
        if ($workbookCounts.ContainsKey($projId)) { $workbookCounts[$projId] = [int]$workbookCounts[$projId] + 1 }
        else { $workbookCounts[$projId] = 1 }
    }
    $datasourceCounts = @{}
    foreach ($ds in @($Datasources)) {
        if ($null -eq $ds) { continue }
        $projId = Get-ChildId $ds 'project'
        if ($datasourceCounts.ContainsKey($projId)) { $datasourceCounts[$projId] = [int]$datasourceCounts[$projId] + 1 }
        else { $datasourceCounts[$projId] = 1 }
    }
    $byId = @{}
    foreach ($proj in @($Projects)) {
        if ($null -eq $proj) { continue }
        $projId = [string]$proj['id']
        if ($projId) { $byId[$projId] = $proj }
    }
    $children = @{}
    $roots = New-Object System.Collections.Generic.List[object]
    foreach ($proj in @($Projects)) {
        if ($null -eq $proj) { continue }
        $parentId = [string]$proj['parentProjectId']
        if ($parentId -and $byId.ContainsKey($parentId)) {
            if (-not $children.ContainsKey($parentId)) {
                $children[$parentId] = New-Object System.Collections.Generic.List[object]
            }
            $children[$parentId].Add($proj)
        } else {
            $roots.Add($proj)
        }
    }
    $lines = New-Object System.Collections.Generic.List[string]
    $seen = New-Object System.Collections.Generic.HashSet[string]
    foreach ($root in @($roots | Sort-Object { ([string]$_['name']).ToLower() })) {
        Walk-ProjectNode $root 0 $children $lines $seen $workbookCounts $datasourceCounts
    }
    return ,$lines.ToArray()
}

function Build-Report {
    param($Inventory)
    $sections = $Inventory['sections']
    $errorEntries = @($Inventory['errors'])
    $options = $Inventory['options']
    $serverDict = $Inventory['server']
    $siteDict = $Inventory['site']
    $now = Parse-IsoDate $Inventory['generated_at']
    if ($null -eq $now) { $now = [DateTime]::UtcNow }

    $projects = $sections['projects']
    $workbooks = $sections['workbooks']
    $views = $sections['views']
    $datasources = $sections['datasources']
    $users = $sections['users']
    $groups = $sections['groups']
    $subscriptions = $sections['subscriptions']
    $schedules = $sections['schedules']
    $refreshTasks = $sections['extract_refresh_tasks']
    $flows = $sections['flows']

    $userNames = @{}
    foreach ($u in @($users)) {
        if ($null -eq $u) { continue }
        $uid = [string]$u['id']
        if (-not $uid) { continue }
        $label = $u['name']
        if (-not $label) { $label = $u['fullName'] }
        if (-not $label) { $label = $uid }
        $userNames[$uid] = [string]$label
    }
    $projectNames = @{}
    foreach ($p in @($projects)) {
        if ($null -eq $p) { continue }
        $keyId = [string]$p['id']
        if (-not $keyId) { continue }
        $projectNames[$keyId] = [string]$p['name']
    }
    $workbookNames = @{}
    foreach ($w in @($workbooks)) {
        if ($null -eq $w) { continue }
        $keyId = [string]$w['id']
        if (-not $keyId) { continue }
        $workbookNames[$keyId] = [string]$w['name']
    }
    $datasourceNames = @{}
    foreach ($d in @($datasources)) {
        if ($null -eq $d) { continue }
        $keyId = [string]$d['id']
        if (-not $keyId) { continue }
        $datasourceNames[$keyId] = [string]$d['name']
    }

    $lines = New-Object System.Collections.Generic.List[string]

    $siteLabel = [string]$siteDict['content_url']
    if (-not $siteLabel) { $siteLabel = '(Default)' }
    $lines.Add('# Tableau Site Deep-Dig Report')
    $lines.Add('')
    $lines.Add('Site **' + (Escape-Md $siteLabel) + '** on `' + [string]$serverDict['url'] + '` ' + $EmDash + ' generated ' + $now.ToString('yyyy-MM-dd HH:mm', [System.Globalization.CultureInfo]::InvariantCulture) + ' UTC by `' + $TOOL_NAME + '`.')
    $lines.Add('')

    # -- Overview ----------------------------------------------------------
    $lines.Add('## Overview')
    $lines.Add('')
    $product = [string]$serverDict['product_version']
    if (-not $product) { $product = 'unknown' }
    $buildVal = [string]$serverDict['build']
    $productLabel = $product
    if ($buildVal) { $productLabel = $product + ' (build ' + $buildVal + ')' }
    $lines.Add('- Server product version: ' + (Escape-Md $productLabel))
    $negotiated = [string]$serverDict['negotiated_api_version']
    if (-not $negotiated) { $negotiated = '?' }
    $lines.Add('- REST API version used: ' + $negotiated)
    if ($serverDict['negotiation_error']) {
        $lines.Add('- Note: /serverinfo was unavailable (old server?); fell back to REST API ' + $FALLBACK_API_VERSION)
    }
    $lines.Add('- Site contentUrl: ' + (Escape-Md $siteLabel) + ' (id `' + [string]$siteDict['id'] + '`)')
    $lines.Add('')
    $countRows = New-Object System.Collections.Generic.List[object]
    $countDefs = @(
        @('projects', 'Projects'),
        @('workbooks', 'Workbooks'),
        @('views', 'Views'),
        @('datasources', 'Data sources'),
        @('users', 'Users'),
        @('groups', 'Groups'),
        @('subscriptions', 'Subscriptions'),
        @('schedules', 'Schedules (server-wide)'),
        @('extract_refresh_tasks', 'Extract refresh tasks'),
        @('flows', 'Flows')
    )
    foreach ($def in $countDefs) {
        $data = $sections[$def[0]]
        $countText = 'unavailable'
        if ($data -is [array]) { $countText = [string]$data.Count }
        $countRows.Add(@($def[1], $countText))
    }
    $optionalDefs = @(
        @('workbook_connections', 'Workbooks w/ connection details', 'connections'),
        @('datasource_connections', 'Datasources w/ connection details', 'connections'),
        @('workbook_permissions', 'Workbooks w/ permission details', 'permissions')
    )
    foreach ($def in $optionalDefs) {
        if ($options[$def[2]]) {
            $data = $sections[$def[0]]
            $countText = 'unavailable'
            if ($data -is [System.Collections.IDictionary]) { $countText = [string]$data.Count }
            $countRows.Add(@($def[1], $countText))
        }
    }
    Add-TableLines $lines @('Content type', 'Count') $countRows
    $lines.Add('')

    # -- Project tree ------------------------------------------------------
    $lines.Add('## Project tree')
    $lines.Add('')
    if ($null -eq $projects) {
        $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
    } elseif ($projects.Count -eq 0) {
        $lines.Add('_No projects returned._')
    } else {
        $treeLines = Get-ProjectTreeLines $projects $workbooks $datasources
        foreach ($treeLine in $treeLines) {
            $lines.Add([string]$treeLine)
        }
    }
    $lines.Add('')

    # -- Workbooks ---------------------------------------------------------
    $lines.Add('## Workbooks')
    $lines.Add('')
    if ($null -eq $workbooks) {
        $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
    } elseif ($workbooks.Count -eq 0) {
        $lines.Add('_No workbooks returned._')
    } else {
        $orderedWb = @($workbooks | Sort-Object { Get-SortDate $_['updatedAt'] } -Descending)
        if ($orderedWb.Count -gt $TABLE_CAP) {
            $lines.Add('Showing the ' + $TABLE_CAP + ' most recently updated of ' + $orderedWb.Count + ' workbooks (full list in site_inventory.json).')
            $lines.Add('')
        }
        $rows = New-Object System.Collections.Generic.List[object]
        $limit = [Math]::Min($TABLE_CAP, $orderedWb.Count)
        for ($i = 0; $i -lt $limit; $i++) {
            $wb = $orderedWb[$i]
            $nameVal = $wb['name']
            if (-not $nameVal) { $nameVal = $wb['id'] }
            if (-not $nameVal) { $nameVal = '' }
            $sizeVal = $wb['size']
            if (-not $sizeVal) { $sizeVal = '' }
            $tagsJoined = ''
            if ($wb['tags'] -is [array]) { $tagsJoined = ($wb['tags'] -join ', ') }
            $rows.Add(@(
                    $nameVal,
                    (Get-ChildName $wb 'project' $projectNames),
                    (Get-ChildName $wb 'owner' $userNames),
                    $sizeVal,
                    (Format-DateCell $wb['createdAt']),
                    (Format-DateCell $wb['updatedAt']),
                    $tagsJoined
                ))
        }
        Add-TableLines $lines @('Workbook', 'Project', 'Owner', 'Size (MB)', 'Created', 'Updated', 'Tags') $rows
    }
    $lines.Add('')

    # -- Views by usage ----------------------------------------------------
    $lines.Add('## Top ' + $TOP_VIEWS + ' views by usage')
    $lines.Add('')
    if ($null -eq $views) {
        $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
    } elseif ($views.Count -eq 0) {
        $lines.Add('_No views returned._')
    } else {
        $haveUsage = $false
        foreach ($v in $views) {
            if ($v['usage'] -is [System.Collections.IDictionary]) { $haveUsage = $true; break }
        }
        if (-not $haveUsage) {
            $lines.Add('_This server did not return usage statistics (includeUsageStatistics unsupported?)._')
            $lines.Add('')
        }
        $topViews = @($views | Sort-Object { Get-UsageCount $_ } -Descending | Select-Object -First $TOP_VIEWS)
        $rows = New-Object System.Collections.Generic.List[object]
        foreach ($v in $topViews) {
            $nameVal = $v['name']
            if (-not $nameVal) { $nameVal = $v['id'] }
            if (-not $nameVal) { $nameVal = '' }
            $rows.Add(@(
                    $nameVal,
                    (Get-ChildName $v 'workbook' $workbookNames),
                    ([string](Get-UsageCount $v))
                ))
        }
        Add-TableLines $lines @('View', 'Workbook', 'Total views') $rows
        $zero = 0
        foreach ($v in $views) {
            if ((Get-UsageCount $v) -eq 0) { $zero++ }
        }
        $lines.Add('')
        $lines.Add('**' + $zero + '** of ' + $views.Count + ' views have zero recorded usage.')
    }
    $lines.Add('')

    # -- Data sources ------------------------------------------------------
    $lines.Add('## Data sources')
    $lines.Add('')
    if ($null -eq $datasources) {
        $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
    } elseif ($datasources.Count -eq 0) {
        $lines.Add('_No data sources returned._')
    } else {
        $orderedDs = @($datasources | Sort-Object { Get-SortDate $_['updatedAt'] } -Descending)
        if ($orderedDs.Count -gt $TABLE_CAP) {
            $lines.Add('Showing the ' + $TABLE_CAP + ' most recently updated of ' + $orderedDs.Count + ' data sources (full list in site_inventory.json).')
            $lines.Add('')
        }
        $rows = New-Object System.Collections.Generic.List[object]
        $limit = [Math]::Min($TABLE_CAP, $orderedDs.Count)
        for ($i = 0; $i -lt $limit; $i++) {
            $ds = $orderedDs[$i]
            $nameVal = $ds['name']
            if (-not $nameVal) { $nameVal = $ds['id'] }
            if (-not $nameVal) { $nameVal = '' }
            $typeVal = $ds['type']
            if (-not $typeVal) { $typeVal = '' }
            $certified = 'no'
            if (([string]$ds['isCertified']).ToLower() -eq 'true') { $certified = 'yes' }
            $rows.Add(@(
                    $nameVal,
                    $typeVal,
                    $certified,
                    (Get-ChildName $ds 'owner' $userNames),
                    (Get-ChildName $ds 'project' $projectNames),
                    (Format-DateCell $ds['updatedAt'])
                ))
        }
        Add-TableLines $lines @('Data source', 'Type', 'Certified', 'Owner', 'Project', 'Updated') $rows
    }
    $lines.Add('')

    # -- Users -------------------------------------------------------------
    $lines.Add('## Users')
    $lines.Add('')
    if ($null -eq $users) {
        $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
    } elseif ($users.Count -eq 0) {
        $lines.Add('_No users returned._')
    } else {
        $roleCounts = [ordered]@{}
        foreach ($u in $users) {
            $role = [string]$u['siteRole']
            if (-not $role) { $role = '(unknown)' }
            if ($roleCounts.Contains($role)) { $roleCounts[$role] = [int]$roleCounts[$role] + 1 }
            else { $roleCounts[$role] = 1 }
        }
        $rows = New-Object System.Collections.Generic.List[object]
        foreach ($roleEntry in @($roleCounts.GetEnumerator() | Sort-Object -Property Value -Descending)) {
            $rows.Add(@([string]$roleEntry.Key, [string]$roleEntry.Value))
        }
        Add-TableLines $lines @('Site role', 'Users') $rows
        $cutoff = $now.AddDays(-1 * $ACTIVE_DAYS)
        $active = 0
        $never = 0
        foreach ($u in $users) {
            $lastLogin = Parse-IsoDate $u['lastLogin']
            if ($null -eq $lastLogin) { $never++ }
            elseif ($lastLogin -ge $cutoff) { $active++ }
        }
        $lines.Add('')
        $lines.Add('**' + $active + '** of ' + $users.Count + ' users signed in within the last ' + $ACTIVE_DAYS + ' days; ' + $never + ' have no recorded last login.')
    }
    $lines.Add('')

    # -- Groups ------------------------------------------------------------
    $lines.Add('## Groups')
    $lines.Add('')
    if ($null -eq $groups) {
        $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
    } elseif ($groups.Count -eq 0) {
        $lines.Add('_No groups returned._')
    } else {
        $lines.Add([string]$groups.Count + ' group(s):')
        $lines.Add('')
        $sortedGroups = @($groups | Sort-Object { ([string]$_['name']).ToLower() })
        $limit = [Math]::Min($TABLE_CAP, $sortedGroups.Count)
        for ($i = 0; $i -lt $limit; $i++) {
            $grp = $sortedGroups[$i]
            $domainVal = $grp['domain']
            $domainName = ''
            if ($domainVal -is [System.Collections.IDictionary]) { $domainName = [string]$domainVal['name'] }
            $suffix = ''
            if ($domainName) { $suffix = ' (domain: ' + (Escape-Md $domainName) + ')' }
            $nameVal = $grp['name']
            if (-not $nameVal) { $nameVal = $grp['id'] }
            $lines.Add('- ' + (Escape-Md $nameVal) + $suffix)
        }
        if ($groups.Count -gt $TABLE_CAP) {
            $lines.Add('- ' + $Ellipsis + ' and ' + ($groups.Count - $TABLE_CAP) + ' more')
        }
    }
    $lines.Add('')

    # -- Schedules / refresh tasks / subscriptions -------------------------
    $lines.Add('## Schedules, extract refreshes and subscriptions')
    $lines.Add('')
    $lines.Add('### Schedules (server-wide)')
    $lines.Add('')
    if ($null -eq $schedules) {
        $lines.Add('_Not available (server-admin only) ' + $EmDash + ' see the Errors section._')
    } elseif ($schedules.Count -eq 0) {
        $lines.Add('_No schedules returned._')
    } else {
        $rows = New-Object System.Collections.Generic.List[object]
        $limit = [Math]::Min($TABLE_CAP, $schedules.Count)
        for ($i = 0; $i -lt $limit; $i++) {
            $sched = $schedules[$i]
            $nameVal = $sched['name']
            if (-not $nameVal) { $nameVal = $sched['id'] }
            if (-not $nameVal) { $nameVal = '' }
            $typeVal = $sched['type']
            if (-not $typeVal) { $typeVal = '' }
            $freqVal = $sched['frequency']
            if (-not $freqVal) { $freqVal = '' }
            $stateVal = $sched['state']
            if (-not $stateVal) { $stateVal = '' }
            $rows.Add(@($nameVal, $typeVal, $freqVal, $stateVal, (Format-DateCell $sched['nextRunAt'])))
        }
        Add-TableLines $lines @('Schedule', 'Type', 'Frequency', 'State', 'Next run') $rows
    }
    $lines.Add('')
    $lines.Add('### Extract refresh tasks')
    $lines.Add('')
    if ($null -eq $refreshTasks) {
        $lines.Add('_Not available (admin only or old server) ' + $EmDash + ' see the Errors section._')
    } elseif ($refreshTasks.Count -eq 0) {
        $lines.Add('_No extract refresh tasks returned._')
    } else {
        $rows = New-Object System.Collections.Generic.List[object]
        $limit = [Math]::Min($TABLE_CAP, $refreshTasks.Count)
        for ($i = 0; $i -lt $limit; $i++) {
            $task = $refreshTasks[$i]
            $refresh = $task['extractRefresh']
            if ($refresh -isnot [System.Collections.IDictionary]) { $refresh = [ordered]@{} }
            $target = ''
            $wbRef = $refresh['workbook']
            $dsRef = $refresh['datasource']
            if ($wbRef -is [System.Collections.IDictionary]) {
                $refId = [string]$wbRef['id']
                $refName = $refId
                if ($refId -and $workbookNames.Contains($refId) -and $workbookNames[$refId]) { $refName = [string]$workbookNames[$refId] }
                $target = 'workbook: ' + $refName
            } elseif ($dsRef -is [System.Collections.IDictionary]) {
                $refId = [string]$dsRef['id']
                $refName = $refId
                if ($refId -and $datasourceNames.Contains($refId) -and $datasourceNames[$refId]) { $refName = [string]$datasourceNames[$refId] }
                $target = 'datasource: ' + $refName
            }
            $schedRef = $refresh['schedule']
            $schedName = ''
            if ($schedRef -is [System.Collections.IDictionary]) { $schedName = [string]$schedRef['name'] }
            $typeVal = $refresh['type']
            if (-not $typeVal) { $typeVal = '' }
            $rows.Add(@($typeVal, $target, $schedName))
        }
        Add-TableLines $lines @('Refresh type', 'Target', 'Schedule') $rows
    }
    $lines.Add('')
    $lines.Add('### Subscriptions')
    $lines.Add('')
    if ($null -eq $subscriptions) {
        $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
    } elseif ($subscriptions.Count -eq 0) {
        $lines.Add('_No subscriptions returned._')
    } else {
        $rows = New-Object System.Collections.Generic.List[object]
        $limit = [Math]::Min($TABLE_CAP, $subscriptions.Count)
        for ($i = 0; $i -lt $limit; $i++) {
            $sub = $subscriptions[$i]
            $content = $sub['content']
            $contentType = ''
            if ($content -is [System.Collections.IDictionary]) { $contentType = [string]$content['type'] }
            $subjectVal = $sub['subject']
            if (-not $subjectVal) { $subjectVal = '' }
            $rows.Add(@(
                    $subjectVal,
                    $contentType,
                    (Get-ChildName $sub 'user' $userNames),
                    (Get-ChildName $sub 'schedule' @{})
                ))
        }
        Add-TableLines $lines @('Subject', 'Content type', 'Subscriber', 'Schedule') $rows
    }
    $lines.Add('')
    if ($null -ne $flows) {
        $lines.Add('### Flows: ' + $flows.Count + ' on this site')
        $lines.Add('')
    }

    # -- Stale content -----------------------------------------------------
    $lines.Add('## Stale content (not updated in ' + $STALE_DAYS + '+ days)')
    $lines.Add('')
    if ($null -eq $workbooks) {
        $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
    } else {
        $staleCutoff = $now.AddDays(-1 * $STALE_DAYS)
        $stale = New-Object System.Collections.Generic.List[object]
        foreach ($wb in $workbooks) {
            $updated = Parse-IsoDate $wb['updatedAt']
            if ($null -ne $updated -and $updated -le $staleCutoff) {
                $stale.Add(@{ updated = $updated; wb = $wb })
            }
        }
        $staleSorted = @($stale | Sort-Object { $_['updated'] })
        if ($staleSorted.Count -eq 0) {
            $lines.Add('No workbooks are older than ' + $STALE_DAYS + ' days (by updatedAt).')
        } else {
            $suffix = ':'
            if ($staleSorted.Count -gt $TABLE_CAP) {
                $suffix = ' (oldest ' + [Math]::Min($TABLE_CAP, $staleSorted.Count) + ' shown):'
            }
            $lines.Add('**' + $staleSorted.Count + '** of ' + $workbooks.Count + ' workbooks were last updated ' + $STALE_DAYS + '+ days ago' + $suffix)
            $lines.Add('')
            $rows = New-Object System.Collections.Generic.List[object]
            $limit = [Math]::Min($TABLE_CAP, $staleSorted.Count)
            for ($i = 0; $i -lt $limit; $i++) {
                $wb = $staleSorted[$i]['wb']
                $nameVal = $wb['name']
                if (-not $nameVal) { $nameVal = $wb['id'] }
                if (-not $nameVal) { $nameVal = '' }
                $rows.Add(@(
                        $nameVal,
                        (Get-ChildName $wb 'project' $projectNames),
                        (Get-ChildName $wb 'owner' $userNames),
                        (Format-DateCell $wb['updatedAt'])
                    ))
            }
            Add-TableLines $lines @('Workbook', 'Project', 'Owner', 'Last updated') $rows
        }
    }
    $lines.Add('')

    # -- Lineage -----------------------------------------------------------
    if ($options['lineage']) {
        $lines.Add('## Lineage summary (Metadata API)')
        $lines.Add('')
        $lineageData = $sections['lineage']
        $nodes = $null
        if ($null -ne $lineageData) {
            $wbNodes = $null
            try { $wbNodes = $lineageData.workbooks } catch { $wbNodes = $null }
            if ($null -ne $wbNodes) { $nodes = @($wbNodes) }
        }
        if ($null -eq $nodes -or $nodes.Count -eq 0) {
            $lines.Add('_Not available ' + $EmDash + ' see the Errors section._')
        } else {
            $withTables = 0
            $dbCounter = [ordered]@{}
            $dbSep = [string][char]31
            foreach ($node in $nodes) {
                $tables = $null
                try { $tables = $node.upstreamTables } catch { $tables = $null }
                $tableArr = @()
                if ($null -ne $tables) { $tableArr = @($tables) }
                if ($tableArr.Count -gt 0) { $withTables++ }
                foreach ($tbl in $tableArr) {
                    if ($null -eq $tbl) { continue }
                    $db = $null
                    try { $db = $tbl.database } catch { $db = $null }
                    $dbName = '(unknown)'
                    $connType = ''
                    if ($null -ne $db) {
                        if ($db.name) { $dbName = [string]$db.name }
                        if ($db.connectionType) { $connType = [string]$db.connectionType }
                    }
                    $key = $dbName + $dbSep + $connType
                    if ($dbCounter.Contains($key)) { $dbCounter[$key] = [int]$dbCounter[$key] + 1 }
                    else { $dbCounter[$key] = 1 }
                }
            }
            $lines.Add('- Workbooks returned by the Metadata API: ' + $nodes.Count)
            $lines.Add('- Workbooks with resolved upstream tables: ' + $withTables)
            $lines.Add('- Distinct upstream databases: ' + $dbCounter.Count)
            $lines.Add('')
            if ($dbCounter.Count -gt 0) {
                $rows = New-Object System.Collections.Generic.List[object]
                foreach ($dbEntry in @($dbCounter.GetEnumerator() | Sort-Object -Property Value -Descending | Select-Object -First 15)) {
                    $keyParts = ([string]$dbEntry.Key).Split([char]31)
                    $partName = $keyParts[0]
                    $partType = ''
                    if ($keyParts.Count -gt 1) { $partType = $keyParts[1] }
                    $rows.Add(@($partName, $partType, [string]$dbEntry.Value))
                }
                Add-TableLines $lines @('Database', 'Connection type', 'Table references') $rows
            }
        }
        $lines.Add('')
    }

    # -- Errors ------------------------------------------------------------
    $lines.Add('## Errors')
    $lines.Add('')
    if ($errorEntries.Count -eq 0) {
        $lines.Add('All sections completed successfully.')
    } else {
        $lines.Add([string]$errorEntries.Count + ' section(s) failed or were skipped; everything else completed:')
        $lines.Add('')
        foreach ($entry in $errorEntries) {
            $lines.Add('- **' + (Escape-Md $entry['section']) + '**: ' + (Escape-Md $entry['error']))
        }
    }
    $lines.Add('')

    $lines.Add('---')
    $lines.Add('')
    $lines.Add('*Share this `report.md` (and `site_inventory.json` for data-level questions) back to Claude for follow-up analysis ' + $EmDash + ' e.g. cleanup candidates, ownership gaps, refresh-schedule overlaps, or usage trends.*')
    $lines.Add('')
    return ($lines -join "`n")
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Best-effort: make sure TLS 1.2 is enabled (old .NET defaults may lack it).
try {
    [System.Net.ServicePointManager]::SecurityProtocol = ([System.Net.ServicePointManager]::SecurityProtocol -bor [System.Net.SecurityProtocolType]::Tls12)
} catch { }

if (-not $Server) { $Server = [string]$env:TABLEAU_SERVER }
if (-not $Server) {
    Write-Log 'error: -Server is required (or set env TABLEAU_SERVER)'
    exit 2
}
if ($Server.IndexOf('://') -lt 0) { $Server = 'http://' + $Server }
$script:ServerUrl = $Server.TrimEnd('/')

if (-not $PSBoundParameters.ContainsKey('Site')) {
    $Site = [string]$env:TABLEAU_SITE
}
if ($null -eq $Site) { $Site = '' }
if (-not $User) { $User = [string]$env:TABLEAU_USER }
if (-not $Password) { $Password = [string]$env:TABLEAU_PASSWORD }
if (-not $PatName) { $PatName = [string]$env:TABLEAU_PAT_NAME }
if (-not $PatSecret) { $PatSecret = [string]$env:TABLEAU_PAT_SECRET }

if ($User -and -not $Password -and -not ($PatName -and $PatSecret)) {
    # Prompt securely; in non-interactive sessions this fails and the
    # password stays empty (mirrors the Python getpass behavior).
    try {
        $securePassword = Read-Host -Prompt ('Tableau password for ' + $User) -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
        try {
            $Password = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        } finally {
            [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    } catch {
        $Password = ''
    }
}

$hasPasswordAuth = [bool]($User -and $Password)
$hasPatAuth = [bool]($PatName -and $PatSecret)
if (-not $hasPasswordAuth -and -not $hasPatAuth) {
    Write-Log ('error: no auth method provided ' + $EmDash + ' use -User/-Password or -PatName/-PatSecret (or the TABLEAU_* env vars)')
    exit 2
}

if ($PageSize -lt 1) { $PageSize = 1 }
if ($PageSize -gt 1000) { $PageSize = 1000 }
$script:TimeoutMs = $TimeoutSec * 1000
$script:UseEnvProxyFlag = [bool]$UseEnvProxy

if ($Insecure) {
    # NOTE: this disables TLS certificate validation for the WHOLE PowerShell
    # process (ServicePointManager is process-wide), not just this script's
    # requests. That is the price of 5.1 compatibility without modules.
    # A compiled delegate is required: a scriptblock callback needs a runspace
    # and throws when .NET validates the certificate on a threadpool thread.
    Add-Type -TypeDefinition @'
using System.Net;
using System.Net.Security;
using System.Security.Cryptography.X509Certificates;
public static class TableauDeepDigTrustAll
{
    public static void Enable()
    {
        ServicePointManager.ServerCertificateValidationCallback =
            new RemoteCertificateValidationCallback(delegate(
                object s, X509Certificate c, X509Chain ch, SslPolicyErrors e)
            { return true; });
    }
}
'@
    [TableauDeepDigTrustAll]::Enable()
}

Write-Log ('[.] negotiating API version with ' + $script:ServerUrl + ' ...')
$negotiationInfo = Invoke-Negotiate $ApiVersion
if ($negotiationInfo['negotiation_error']) {
    Write-Log ('[!] /serverinfo failed (' + $negotiationInfo['negotiation_error'] + '); assuming an old server, REST API ' + $script:RestApiVersion)
} else {
    $productShown = [string]$negotiationInfo['product_version']
    if (-not $productShown) { $productShown = '?' }
    Write-Log ('[+] server ' + $productShown + ' ' + $EmDash + ' using REST API ' + $script:RestApiVersion)
}

$usePat = $hasPatAuth
if ($usePat) {
    $currentVersion = Get-VersionParts $script:RestApiVersion
    if ((Compare-VersionParts $currentVersion @(3, 6)) -lt 0) {
        if ($hasPasswordAuth) {
            Write-Log '[!] personal access tokens need REST API 3.6+; falling back to username/password'
            $usePat = $false
        } else {
            Write-Log ('error: personal access tokens require REST API 3.6+ but this server negotiated ' + $script:RestApiVersion + '; use -User/-Password instead')
            exit 2
        }
    }
}

$siteShown = $Site
if (-not $siteShown) { $siteShown = '(Default)' }
Write-Log ("[.] signing in to site '" + $siteShown + "' ...")
try {
    if ($usePat) {
        Invoke-SignIn -SiteContentUrl $Site -TokenName $PatName -TokenSecret $PatSecret
    } else {
        Invoke-SignIn -SiteContentUrl $Site -UserName $User -UserPassword $Password
    }
} catch {
    Write-Log ('error: sign-in failed: ' + $_.Exception.Message)
    exit 2
}
Write-Log ('[+] signed in (site id ' + $script:SiteId + ')')

try {
    $null = Collect-Inventory
} finally {
    Invoke-SignOut
}

$generatedAt = [System.DateTimeOffset]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ss.ffffffzzz', [System.Globalization.CultureInfo]::InvariantCulture)
$serverSection = [ordered]@{ url = $script:ServerUrl }
foreach ($key in $script:ServerInfo.Keys) { $serverSection[$key] = $script:ServerInfo[$key] }
$siteContentUrlOut = $script:AuthSiteContentUrl
if (-not $siteContentUrlOut) { $siteContentUrlOut = $Site }

$inventory = [ordered]@{
    tool = $TOOL_NAME
    generated_at = $generatedAt
    server = $serverSection
    site = [ordered]@{ id = $script:SiteId; content_url = $siteContentUrlOut }
    options = [ordered]@{
        connections = [bool]$Connections
        permissions = [bool]$Permissions
        lineage = [bool]$Lineage
        page_size = [int]$PageSize
        timeout = [int]$TimeoutSec
        insecure = [bool]$Insecure
        use_env_proxy = [bool]$UseEnvProxy
    }
    sections = $script:Sections
    errors = $script:ErrorsList.ToArray()
}

$outDir = $Out
if (-not [System.IO.Path]::IsPathRooted($outDir)) {
    $outDir = Join-Path (Get-Location).Path $outDir
}
$null = [System.IO.Directory]::CreateDirectory($outDir)
$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$jsonPath = Join-Path $outDir 'site_inventory.json'
# ConvertTo-Json's default -Depth of 2 silently truncates; always be explicit.
$jsonText = ConvertTo-Json -InputObject $inventory -Depth 20
[System.IO.File]::WriteAllText($jsonPath, ($jsonText + "`n"), $utf8NoBom)
$reportPath = Join-Path $outDir 'report.md'
$reportText = Build-Report $inventory
[System.IO.File]::WriteAllText($reportPath, $reportText, $utf8NoBom)

Write-Log ('[+] wrote ' + $jsonPath)
Write-Log ('[+] wrote ' + $reportPath)
if ($script:ErrorsList.Count -gt 0) {
    Write-Log ('[!] ' + $script:ErrorsList.Count + ' section(s) recorded errors ' + $EmDash + ' see the Errors section of the report')
}
Write-Log ('[+] done ' + $EmDash + ' share report.md (and site_inventory.json) with Claude')
exit 0
