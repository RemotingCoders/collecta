<#
.SYNOPSIS
    Smoke test for the Collecta PayPal connector: calls every connector operation against the PayPal SANDBOX.

.DESCRIPTION
    Talks to https://api-m.sandbox.paypal.com directly (not through Power Platform) and mirrors what the connector's
    script.csx does: Client ID / Secret -> OAuth token -> Bearer call, "Prefer: return=representation" on create
    calls and "{}" as the body of POSTs that have no body.

    Credentials are read from the environment variables PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET.
    If they are not set, the script loads them from a .env file in the repository root (see .env.example).
    Credentials and tokens are never printed.

    The run creates sandbox data: one invoice (created, sent, reminded, then cancelled), one checkout order
    (left unapproved, it expires on its own) and, unless -SkipPayout is used, one payout of 1.00.

    Works on Windows PowerShell 5.1 and PowerShell 7+.

.PARAMETER RecipientEmail
    Email of the customer billed by the test invoice. Use the email of a sandbox Personal account.

.PARAMETER PayoutReceiver
    Email that receives the test payout. Use the email of a sandbox Personal account.

.PARAMETER Currency
    ISO-4217 currency code used for all amounts. Default: USD.

.PARAMETER ApprovedOrderId
    Optional ID of an order that a sandbox buyer has already approved. When supplied, CaptureOrder captures it and
    must return 201. Otherwise CaptureOrder is called on the fresh, unapproved order and the expected answer is
    422 ORDER_NOT_APPROVED (which still proves that the endpoint and the credentials work).

.PARAMETER SkipPayout
    Skips CreatePayout and GetPayoutBatch (for REST apps without the Payouts feature).

.EXAMPLE
    $env:PAYPAL_CLIENT_ID = '<client id>'; $env:PAYPAL_CLIENT_SECRET = '<secret>'
    ./tests/smoke-test.ps1 -RecipientEmail sb-buyer@personal.example.com -PayoutReceiver sb-buyer@personal.example.com
#>
[CmdletBinding()]
param(
    [string]$RecipientEmail = 'collecta-customer@example.com',
    [string]$PayoutReceiver = 'collecta-payee@example.com',
    [string]$Currency = 'USD',
    [string]$ApprovedOrderId,
    [switch]$SkipPayout
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

# Sandbox only, on purpose: this script creates invoices and moves (fake) money.
$BaseUrl = 'https://api-m.sandbox.paypal.com'

# ---------------------------------------------------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------------------------------------------------
function Import-DotEnv {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $name = $Matches[1]
            $value = $Matches[2].Trim('"').Trim("'")
            # Real environment variables win over the .env file.
            if (-not [Environment]::GetEnvironmentVariable($name)) {
                [Environment]::SetEnvironmentVariable($name, $value)
            }
        }
    }
}

Import-DotEnv -Path (Join-Path (Split-Path -Parent $PSScriptRoot) '.env')

$clientId = $env:PAYPAL_CLIENT_ID
$clientSecret = $env:PAYPAL_CLIENT_SECRET
if ([string]::IsNullOrWhiteSpace($clientId) -or [string]::IsNullOrWhiteSpace($clientSecret)) {
    Write-Host 'PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET are not set.' -ForegroundColor Red
    Write-Host 'Set them as environment variables, or copy .env.example to .env and fill in your sandbox credentials.'
    exit 2
}

# ---------------------------------------------------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------------------------------------------------
$script:Token = $null

function Invoke-Http {
    # Returns @{ Status; Json; Raw } for success AND error responses (never throws on HTTP errors).
    param(
        [string]$Method,
        [string]$Uri,
        [hashtable]$Headers,
        [string]$Body,
        [string]$ContentType
    )
    $req = @{ Method = $Method; Uri = $Uri; Headers = $Headers; UseBasicParsing = $true; ErrorAction = 'Stop' }
    if ($ContentType) { $req.ContentType = $ContentType }
    if ($Body) { $req.Body = $Body }

    $status = 0
    $raw = $null
    try {
        $resp = Invoke-WebRequest @req
        $status = [int]$resp.StatusCode
        $raw = $resp.Content
    }
    catch {
        $errResp = $null
        if ($_.Exception.PSObject.Properties['Response']) { $errResp = $_.Exception.Response }
        if ($null -eq $errResp) { throw }   # network / DNS / TLS problem, not an HTTP error
        $status = [int]$errResp.StatusCode
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            $raw = $_.ErrorDetails.Message
        }
        elseif ($errResp -is [System.Net.HttpWebResponse]) {
            # Windows PowerShell 5.1
            $reader = New-Object System.IO.StreamReader($errResp.GetResponseStream())
            try { $raw = $reader.ReadToEnd() } finally { $reader.Dispose() }
        }
    }

    if ($raw -is [byte[]]) { $raw = [Text.Encoding]::UTF8.GetString($raw) }
    $json = $null
    if ($raw) {
        try { $json = $raw | ConvertFrom-Json } catch { $json = $null }
    }
    return [pscustomobject]@{ Status = $status; Json = $json; Raw = $raw }
}

function Invoke-PayPal {
    param(
        [string]$Method,
        [string]$Path,
        $Body = $null,
        [hashtable]$Headers = @{}
    )
    $h = @{ Authorization = "Bearer $($script:Token)"; Accept = 'application/json' }
    foreach ($k in $Headers.Keys) { $h[$k] = $Headers[$k] }

    if ($Method -eq 'POST') {
        # Same as script.csx: a POST without a body is sent as "{}" with a JSON content type.
        $json = '{}'
        if ($null -ne $Body) { $json = $Body | ConvertTo-Json -Depth 12 -Compress }
        return Invoke-Http -Method POST -Uri "$BaseUrl$Path" -Headers $h -Body $json -ContentType 'application/json'
    }
    return Invoke-Http -Method $Method -Uri "$BaseUrl$Path" -Headers $h
}

function Get-Prop {
    # Safe nested property access under strict mode: Get-Prop $obj 'detail.metadata.recipient_view_url'
    param($Object, [string]$Path)
    $current = $Object
    foreach ($part in $Path.Split('.')) {
        if ($null -eq $current) { return $null }
        $p = $current.PSObject.Properties[$part]
        if ($null -eq $p) { return $null }
        $current = $p.Value
    }
    return $current
}

# ---------------------------------------------------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------------------------------------------------
$results = New-Object System.Collections.Generic.List[object]

function Add-Result {
    param([string]$Operation, [string]$Outcome, [string]$Http, [string]$Detail)
    $results.Add([pscustomobject]@{ Operation = $Operation; Result = $Outcome; Http = $Http; Detail = $Detail })
    $color = 'Green'
    if ($Outcome -eq 'FAIL') { $color = 'Red' } elseif ($Outcome -eq 'SKIP') { $color = 'Yellow' }
    Write-Host ('[{0}] {1,-26} {2,-4} {3}' -f $Outcome, $Operation, $Http, $Detail) -ForegroundColor $color
}

function Get-ErrorText {
    param($Response)
    $name = Get-Prop $Response.Json 'name'
    if (-not $name) { $name = Get-Prop $Response.Json 'error' }
    $message = Get-Prop $Response.Json 'message'
    if (-not $message) { $message = Get-Prop $Response.Json 'error_description' }
    $issue = $null
    $details = Get-Prop $Response.Json 'details'
    if ($details) { $issue = Get-Prop (@($details)[0]) 'issue' }
    $debugId = Get-Prop $Response.Json 'debug_id'
    $text = (@($name, $issue, $message) | Where-Object { $_ }) -join ' - '
    if (-not $text -and $Response.Raw) { $text = ([string]$Response.Raw) }
    if ($text -and $text.Length -gt 300) { $text = $text.Substring(0, 300) + '...' }
    if ($debugId) { $text = "$text (debug_id $debugId)" }
    if ($Response.Status -eq 403) { $text = "$text | Check that this feature is enabled for the sandbox REST app." }
    return $text
}

function Test-Call {
    # Records PASS when the HTTP status is one of $Expected, FAIL otherwise. Returns $true on PASS.
    param([string]$Operation, $Response, [int[]]$Expected, [string]$Detail = '')
    if ($Expected -contains $Response.Status) {
        Add-Result $Operation 'PASS' ([string]$Response.Status) $Detail
        return $true
    }
    Add-Result $Operation 'FAIL' ([string]$Response.Status) (Get-ErrorText $Response)
    return $false
}

# ---------------------------------------------------------------------------------------------------------------------
# 0. OAuth token (what script.csx does on every connector call)
# ---------------------------------------------------------------------------------------------------------------------
Write-Host "PayPal sandbox smoke test -> $BaseUrl" -ForegroundColor Cyan

$basic = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${clientId}:${clientSecret}"))
$tokenResponse = Invoke-Http -Method POST -Uri "$BaseUrl/v1/oauth2/token" `
    -Headers @{ Authorization = "Basic $basic"; Accept = 'application/json' } `
    -Body 'grant_type=client_credentials' -ContentType 'application/x-www-form-urlencoded'

$script:Token = Get-Prop $tokenResponse.Json 'access_token'
if ($tokenResponse.Status -ne 200 -or -not $script:Token) {
    Add-Result 'OAuthToken' 'FAIL' ([string]$tokenResponse.Status) (Get-ErrorText $tokenResponse)
    Write-Host 'Could not get an access token. Check the sandbox Client ID and Secret.' -ForegroundColor Red
    exit 1
}
Add-Result 'OAuthToken' 'PASS' '200' 'access token received'

$runId = (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
$representation = @{ Prefer = 'return=representation' }

# ---------------------------------------------------------------------------------------------------------------------
# Invoicing v2
# ---------------------------------------------------------------------------------------------------------------------
$r = Invoke-PayPal POST '/v2/invoicing/generate-next-invoice-number'
$invoiceNumber = Get-Prop $r.Json 'invoice_number'
[void](Test-Call 'GenerateNextInvoiceNumber' $r @(200) "invoice_number=$invoiceNumber")
if (-not $invoiceNumber) { $invoiceNumber = "COLLECTA-$runId" }

$invoiceBody = @{
    detail             = @{
        currency_code  = $Currency
        invoice_number = $invoiceNumber
        invoice_date   = (Get-Date).ToString('yyyy-MM-dd')
        payment_term   = @{ term_type = 'NET_30' }
        note           = 'Collecta smoke test invoice. Thank you for your business.'
        memo           = "Collecta smoke test $runId"
    }
    primary_recipients = @(
        @{
            billing_info = @{
                name          = @{ given_name = 'Casey'; surname = 'Customer' }
                email_address = $RecipientEmail
            }
        }
    )
    items              = @(
        @{
            name            = 'Consulting services'
            description     = 'Collecta smoke test line item'
            quantity        = '2'
            unit_amount     = @{ currency_code = $Currency; value = '50.00' }
            unit_of_measure = 'HOURS'
        }
    )
}
$r = Invoke-PayPal POST '/v2/invoicing/invoices' $invoiceBody $representation
$invoiceId = Get-Prop $r.Json 'id'
$viewUrl = Get-Prop $r.Json 'detail.metadata.recipient_view_url'
$created = Test-Call 'CreateDraftInvoice' $r @(201) "id=$invoiceId status=$(Get-Prop $r.Json 'status')"
if ($created -and (-not $invoiceId -or -not $viewUrl -or -not (Get-Prop $r.Json 'links'))) {
    Add-Result 'CreateDraftInvoice.Schema' 'FAIL' '201' 'response is missing id, detail.metadata.recipient_view_url or links'
}

if ($invoiceId) {
    $r = Invoke-PayPal GET "/v2/invoicing/invoices/$invoiceId"
    [void](Test-Call 'GetInvoice' $r @(200) "status=$(Get-Prop $r.Json 'status') due=$(Get-Prop $r.Json 'due_amount.value')")

    $r = Invoke-PayPal POST "/v2/invoicing/invoices/$invoiceId/send"
    $sent = Test-Call 'SendInvoice' $r @(200, 202) "rel=$(Get-Prop $r.Json 'rel')"
}
else {
    $sent = $false
    Add-Result 'GetInvoice' 'SKIP' '-' 'no invoice was created'
    Add-Result 'SendInvoice' 'SKIP' '-' 'no invoice was created'
}

$r = Invoke-PayPal GET '/v2/invoicing/invoices?page=1&page_size=5&total_required=true'
[void](Test-Call 'ListInvoices' $r @(200) "total_items=$(Get-Prop $r.Json 'total_items')")

$searchBody = @{
    status             = @('SENT', 'UNPAID', 'PARTIALLY_PAID')
    recipient_email    = $RecipientEmail
    invoice_date_range = @{
        start = (Get-Date).AddDays(-30).ToString('yyyy-MM-dd')
        end   = (Get-Date).AddDays(1).ToString('yyyy-MM-dd')
    }
    total_amount_range = @{
        lower_amount = @{ currency_code = $Currency; value = '1.00' }
        upper_amount = @{ currency_code = $Currency; value = '10000.00' }
    }
}
$r = Invoke-PayPal POST '/v2/invoicing/search-invoices?page=1&page_size=5&total_required=true' $searchBody
[void](Test-Call 'SearchInvoices' $r @(200) "total_items=$(Get-Prop $r.Json 'total_items')")

if ($sent) {
    $r = Invoke-PayPal POST "/v2/invoicing/invoices/$invoiceId/remind" @{ subject = 'Payment reminder'; note = 'Collecta smoke test reminder.' }
    [void](Test-Call 'RemindInvoice' $r @(204) 'reminder sent')

    $r = Invoke-PayPal POST "/v2/invoicing/invoices/$invoiceId/cancel" @{ subject = 'Invoice cancelled'; note = 'Collecta smoke test cleanup.' }
    [void](Test-Call 'CancelInvoice' $r @(204) 'invoice cancelled')
}
else {
    Add-Result 'RemindInvoice' 'SKIP' '-' 'the invoice was not sent'
    Add-Result 'CancelInvoice' 'SKIP' '-' 'the invoice was not sent'
}

# ---------------------------------------------------------------------------------------------------------------------
# Orders v2
# ---------------------------------------------------------------------------------------------------------------------
$orderBody = @{
    intent         = 'CAPTURE'
    purchase_units = @(
        @{
            amount      = @{ currency_code = $Currency; value = '100.00' }
            description = "Payment of invoice $invoiceNumber"
            invoice_id  = "COLLECTA-ORDER-$runId"
        }
    )
    payment_source = @{
        paypal = @{
            experience_context = @{
                return_url          = 'https://example.com/collecta/return'
                cancel_url          = 'https://example.com/collecta/cancel'
                brand_name          = 'Collecta'
                user_action         = 'PAY_NOW'
                shipping_preference = 'NO_SHIPPING'
            }
        }
    }
}
$r = Invoke-PayPal POST '/v2/checkout/orders' $orderBody $representation
$orderId = Get-Prop $r.Json 'id'
$payLink = $null
foreach ($l in @(Get-Prop $r.Json 'links')) {
    if ($l -and (@('approve', 'payer-action') -contains $l.rel)) { $payLink = $l.href }
}
[void](Test-Call 'CreateOrder' $r @(200, 201) "id=$orderId status=$(Get-Prop $r.Json 'status')")
if ($payLink) { Write-Host "       customer payment page: $payLink" -ForegroundColor DarkGray }

if ($orderId) {
    $r = Invoke-PayPal GET "/v2/checkout/orders/$orderId"
    [void](Test-Call 'GetOrder' $r @(200) "status=$(Get-Prop $r.Json 'status')")
}
else {
    Add-Result 'GetOrder' 'SKIP' '-' 'no order was created'
}

if ($ApprovedOrderId) {
    $r = Invoke-PayPal POST "/v2/checkout/orders/$ApprovedOrderId/capture" $null $representation
    [void](Test-Call 'CaptureOrder' $r @(200, 201) "status=$(Get-Prop $r.Json 'status')")
}
elseif ($orderId) {
    $r = Invoke-PayPal POST "/v2/checkout/orders/$orderId/capture" $null $representation
    $issue = $null
    $details = Get-Prop $r.Json 'details'
    if ($details) { $issue = Get-Prop (@($details)[0]) 'issue' }
    if ($r.Status -eq 422 -and $issue -eq 'ORDER_NOT_APPROVED') {
        Add-Result 'CaptureOrder' 'PASS' '422' 'ORDER_NOT_APPROVED as expected (no buyer approved it); use -ApprovedOrderId for a real capture'
    }
    else {
        [void](Test-Call 'CaptureOrder' $r @(200, 201) "status=$(Get-Prop $r.Json 'status')")
    }
}
else {
    Add-Result 'CaptureOrder' 'SKIP' '-' 'no order was created'
}

# ---------------------------------------------------------------------------------------------------------------------
# Payouts
# ---------------------------------------------------------------------------------------------------------------------
if ($SkipPayout) {
    Add-Result 'CreatePayout' 'SKIP' '-' 'skipped by -SkipPayout'
    Add-Result 'GetPayoutBatch' 'SKIP' '-' 'skipped by -SkipPayout'
}
else {
    $payoutBody = @{
        sender_batch_header = @{
            sender_batch_id = "COLLECTA-PAYOUT-$runId"
            email_subject   = 'You have a payout'
            email_message   = 'Collecta smoke test payout.'
        }
        items               = @(
            @{
                recipient_type = 'EMAIL'
                receiver       = $PayoutReceiver
                amount         = @{ currency = $Currency; value = '1.00' }
                note           = 'Collecta smoke test'
                sender_item_id = "ITEM-$runId"
            }
        )
    }
    $r = Invoke-PayPal POST '/v1/payments/payouts' $payoutBody
    $batchId = Get-Prop $r.Json 'batch_header.payout_batch_id'
    [void](Test-Call 'CreatePayout' $r @(201) "payout_batch_id=$batchId status=$(Get-Prop $r.Json 'batch_header.batch_status')")

    if ($batchId) {
        $r = Invoke-PayPal GET "/v1/payments/payouts/${batchId}?page=1&page_size=10&total_required=true"
        [void](Test-Call 'GetPayoutBatch' $r @(200) "status=$(Get-Prop $r.Json 'batch_header.batch_status')")
    }
    else {
        Add-Result 'GetPayoutBatch' 'SKIP' '-' 'no payout was created'
    }
}

# ---------------------------------------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------------------------------------
$utcNow = (Get-Date).ToUniversalTime()
$startDate = [Uri]::EscapeDataString($utcNow.AddDays(-7).ToString('yyyy-MM-ddTHH:mm:ssZ'))
$endDate = [Uri]::EscapeDataString($utcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))
$r = Invoke-PayPal GET "/v1/reporting/transactions?start_date=$startDate&end_date=$endDate&fields=all&page=1&page_size=20"
[void](Test-Call 'ListTransactions' $r @(200) "total_items=$(Get-Prop $r.Json 'total_items')")

# ---------------------------------------------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------------------------------------------
$passed = @($results | Where-Object { $_.Result -eq 'PASS' }).Count
$failed = @($results | Where-Object { $_.Result -eq 'FAIL' }).Count
$skipped = @($results | Where-Object { $_.Result -eq 'SKIP' }).Count

Write-Host ''
Write-Host "Passed: $passed  Failed: $failed  Skipped: $skipped" -ForegroundColor Cyan
if ($failed -gt 0) { exit 1 }
exit 0
