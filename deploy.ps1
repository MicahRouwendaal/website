param(
  [string]$Domain = ""
)

if (!(Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
}

if ($Domain -ne "") {
  $content = Get-Content ".env" -Raw
  if ($content -match "(?m)^DOMAIN=") {
    $content = [regex]::Replace($content, "(?m)^DOMAIN=.*$", "DOMAIN=$Domain")
  } else {
    $content += "`r`nDOMAIN=$Domain`r`n"
  }
  Set-Content ".env" $content -Encoding UTF8
}

docker compose up -d --build
