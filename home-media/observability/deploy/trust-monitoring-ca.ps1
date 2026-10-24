param(
    [Parameter(Mandatory=$true)][string]$CertificatePath,
    [Parameter(Mandatory=$true)][string]$StatusPath
)
$ErrorActionPreference = 'Stop'
try {
    @{ trusted = $false; pending = $true } | ConvertTo-Json | Set-Content -LiteralPath $StatusPath
    $certificate = [System.Security.Cryptography.X509Certificates.X509Certificate2]::new($CertificatePath)
    if (-not (Test-Path -LiteralPath "Cert:\CurrentUser\Root\$($certificate.Thumbprint)")) {
        # Native certutil can present Windows' root-certificate consent dialog.
        # PowerShell's Import-Certificate rejects that UI in a hidden host.
        & certutil.exe -user -addstore Root $CertificatePath | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Certificate installation did not complete.' }
    }
    if (-not (Test-Path -LiteralPath "Cert:\CurrentUser\Root\$($certificate.Thumbprint)")) {
        throw 'Certificate trust could not be verified.'
    }
    @{ trusted = $true } | ConvertTo-Json | Set-Content -LiteralPath $StatusPath
} catch {
    @{ trusted = $false; reason = 'Windows certificate trust was not approved or could not be installed.' } |
        ConvertTo-Json | Set-Content -LiteralPath $StatusPath
}
