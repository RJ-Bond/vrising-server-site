# Tiny static file server for local screenshot/preview (no node/python needed).
# Serves the given folder so absolute-path assets (/theme.css, /components.css,
# /tailwind.min.css, /common.js) resolve correctly. /api/* is not handled, so
# dynamic data is absent — this is for layout/CSS verification only.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts/serve.ps1 -Port 8977 -Root frontend
param([int]$Port = 8977, [string]$Root = "frontend")
$Root = (Resolve-Path $Root).Path
$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://127.0.0.1:$Port/")
$listener.Start()
Write-Host "serving $Root at http://127.0.0.1:$Port/  (Ctrl+C to stop)"
$mime = @{
  '.html'='text/html; charset=utf-8'; '.css'='text/css'; '.js'='application/javascript';
  '.json'='application/json'; '.png'='image/png'; '.jpg'='image/jpeg'; '.jpeg'='image/jpeg';
  '.svg'='image/svg+xml'; '.ico'='image/x-icon'; '.webp'='image/webp'; '.woff2'='font/woff2'
}
while ($listener.IsListening) {
  try {
    $ctx = $listener.GetContext()
    $rel = [uri]::UnescapeDataString($ctx.Request.Url.LocalPath).TrimStart('/')
    if ($rel -eq '') { $rel = 'index.html' }
    $file = Join-Path $Root $rel
    if (Test-Path $file -PathType Leaf) {
      $bytes = [IO.File]::ReadAllBytes($file)
      $ext = [IO.Path]::GetExtension($file).ToLower()
      $ctx.Response.ContentType = $(if ($mime.ContainsKey($ext)) { $mime[$ext] } else { 'application/octet-stream' })
      $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    } else {
      $ctx.Response.StatusCode = 404
    }
    $ctx.Response.Close()
  } catch { }
}
