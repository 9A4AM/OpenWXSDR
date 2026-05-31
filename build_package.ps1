# OpenWXSDR Package Builder for Windows
# Creates distribution package for deployment to Raspberry Pi

$VERSION = "1.0.46"
$PACKAGE_NAME = "openwxsdr-$VERSION"
$BUILD_DIR = "build\$PACKAGE_NAME"

Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  OpenWXSDR v$VERSION - Build Distribution Package" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""

# Clean previous build
if (Test-Path "build") {
    Write-Host "Cleaning previous build..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force "build"
}

# Create build directory structure
Write-Host "Creating package structure..." -ForegroundColor Green
New-Item -ItemType Directory -Force -Path "$BUILD_DIR" | Out-Null
New-Item -ItemType Directory -Force -Path "$BUILD_DIR\src\decoders" | Out-Null
New-Item -ItemType Directory -Force -Path "$BUILD_DIR\src\sdr" | Out-Null
New-Item -ItemType Directory -Force -Path "$BUILD_DIR\src\output" | Out-Null
New-Item -ItemType Directory -Force -Path "$BUILD_DIR\src\webui" | Out-Null
New-Item -ItemType Directory -Force -Path "$BUILD_DIR\templates" | Out-Null
New-Item -ItemType Directory -Force -Path "$BUILD_DIR\scripts" | Out-Null
New-Item -ItemType Directory -Force -Path "$BUILD_DIR\docs" | Out-Null

# Copy source files
Write-Host "Copying source files..." -ForegroundColor Green
Copy-Item "src\*.py" "$BUILD_DIR\src\" -Force
Copy-Item "src\decoders\*.py" "$BUILD_DIR\src\decoders\" -Force
Copy-Item "src\sdr\*.py" "$BUILD_DIR\src\sdr\" -Force
Copy-Item "src\output\*.py" "$BUILD_DIR\src\output\" -Force
Copy-Item "src\webui\*.py" "$BUILD_DIR\src\webui\" -Force

# Copy templates
Write-Host "Copying templates..." -ForegroundColor Green
Copy-Item "templates\*.html" "$BUILD_DIR\templates\" -Force -ErrorAction SilentlyContinue

# Copy assets (images for map markers)
Write-Host "Copying assets..." -ForegroundColor Green
if (Test-Path "assets") {
    New-Item -ItemType Directory -Force -Path "$BUILD_DIR\assets\img" | Out-Null
    Copy-Item "assets\img\*" "$BUILD_DIR\assets\img\" -Force -ErrorAction SilentlyContinue
}

# Copy scripts
Write-Host "Copying scripts..." -ForegroundColor Green
Copy-Item "scripts\*" "$BUILD_DIR\scripts\" -Force -ErrorAction SilentlyContinue

# Copy documentation
Write-Host "Copying documentation..." -ForegroundColor Green
Copy-Item "docs\*.md" "$BUILD_DIR\docs\" -Force -ErrorAction SilentlyContinue
Copy-Item "*.md" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue

# Copy configuration and root files
Write-Host "Copying configuration files..." -ForegroundColor Green
Copy-Item "openwxsdr.py" "$BUILD_DIR\" -Force
Copy-Item "config.yaml" "$BUILD_DIR\" -Force
Copy-Item "requirements.txt" "$BUILD_DIR\" -Force
Copy-Item "LICENSE" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue

# Copy test scripts
Write-Host "Copying test scripts..." -ForegroundColor Green
Copy-Item "test_*.sh" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue
Copy-Item "diagnose_*.sh" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue
Copy-Item "check_*.sh" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue
Copy-Item "fix_*.sh" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue
Copy-Item "verify_*.sh" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue

# Copy install script (the one in build dir)
Write-Host "Copying installation script..." -ForegroundColor Green
Copy-Item "install.sh" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue
Copy-Item "install_flux242.sh" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue

# Copy RTL-SDR udev rules
Write-Host "Copying RTL-SDR udev rules..." -ForegroundColor Green
Copy-Item "rtl-sdr.rules" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue

# Copy release notes
Write-Host "Copying release documentation..." -ForegroundColor Green
Copy-Item "RELEASE_v$VERSION.txt" "$BUILD_DIR\" -Force -ErrorAction SilentlyContinue
Copy-Item "CHANGELOG.md" "$BUILD_DIR\" -Force

# Create package README
Write-Host "Creating package README..." -ForegroundColor Green
@"
================================================================================
  OpenWXSDR v$VERSION - Distribution Package
  Open Weather Data Receiver for Radiosondes
================================================================================

QUICK START:
------------
1. Transfer to Raspberry Pi:
   scp openwxsdr-$VERSION.tar.gz pi@raspberry-pi:~/

2. Extract on Pi:
   tar -xzf openwxsdr-$VERSION.tar.gz
   cd openwxsdr-$VERSION

3. Run installer:
   chmod +x install.sh
   ./install.sh

CONTENTS:
---------
- openwxsdr.py          Main application
- src/                  Source code modules
- templates/            Web UI templates
- scripts/              Utility scripts
- docs/                 Documentation
- config.yaml           Configuration file
- requirements.txt      Python dependencies
- install.sh            Installation script
- test_*.sh             Test scripts

REQUIREMENTS:
-------------
- Raspberry Pi (3B+ or newer)
- RTL-SDR USB dongle
- Raspbian/Debian OS
- Python 3.7+
- rtl-sdr and sox packages
- rs1729 decoder binaries (separate download)

DOCUMENTATION:
--------------
See docs/ folder for full documentation.

SUPPORT:
--------
Check docs/TROUBLESHOOTING.md for common issues.

VERSION: $VERSION
RELEASE: 2026-05-01
ARCHITECTURE: Named pipe WAV format streaming

================================================================================
"@ | Out-File -FilePath "$BUILD_DIR\README_PACKAGE.txt" -Encoding UTF8

# Create quick reference
Write-Host "Creating quick reference..." -ForegroundColor Green
@"
OpenWXSDR v$VERSION Quick Reference

INSTALLATION:
  ./install.sh

SERVICE CONTROL:
  sudo systemctl start openwxsdr
  sudo systemctl stop openwxsdr
  sudo systemctl restart openwxsdr
  sudo systemctl status openwxsdr

MONITORING:
  sudo journalctl -u openwxsdr -f

TESTING:
  ./test_v1.0.17_fifo.sh
  ./diagnose_pipeline.sh

WEB INTERFACE:
  http://<raspberry-pi-ip>:8080

KEY FILES:
  /home/pi/OpenWXSDR/config.yaml
  /etc/systemd/system/openwxsdr.service

See README_PACKAGE.txt for full documentation.
"@ | Out-File -FilePath "$BUILD_DIR\QUICKREF.txt" -Encoding UTF8

# Create manifest
Write-Host "Creating manifest..." -ForegroundColor Green
$fileCount = (Get-ChildItem -Path $BUILD_DIR -Recurse -File | Measure-Object).Count
$dirSize = (Get-ChildItem -Path $BUILD_DIR -Recurse | Measure-Object -Property Length -Sum).Sum / 1MB

@"
OpenWXSDR v$VERSION - Package Manifest
Generated: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")

PACKAGE CONTENTS:
  Files: $fileCount
  Size: $([math]::Round($dirSize, 2)) MB

SOURCE FILES:
$(Get-ChildItem -Path "$BUILD_DIR\src" -Recurse -File -Filter "*.py" | Select-Object -ExpandProperty FullName | ForEach-Object { "  " + $_.Replace("$BUILD_DIR\", "") })

DOCUMENTATION:
$(Get-ChildItem -Path "$BUILD_DIR" -File -Filter "*.md" | Select-Object -ExpandProperty Name | ForEach-Object { "  $_" })
$(Get-ChildItem -Path "$BUILD_DIR\docs" -File -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Name | ForEach-Object { "  docs/$_" })

TEST SCRIPTS:
$(Get-ChildItem -Path "$BUILD_DIR" -File -Filter "*.sh" | Select-Object -ExpandProperty Name | ForEach-Object { "  $_" })

BUILD INFO:
  Version: $VERSION
  Build Date: $(Get-Date -Format "yyyy-MM-dd")
  Platform: Windows PowerShell
  Target: Raspberry Pi (Debian/Raspbian)
"@ | Out-File -FilePath "$BUILD_DIR\MANIFEST.txt" -Encoding UTF8

# Convert line endings to Unix format (LF only)
Write-Host ""
Write-Host "Converting line endings to Unix format (LF)..." -ForegroundColor Green
$textExtensions = @('*.py', '*.sh', '*.md', '*.txt', '*.yaml', '*.yml', '*.html', '*.css', '*.js', '*.json')
$convertedCount = 0

foreach ($ext in $textExtensions) {
    $files = Get-ChildItem -Path $BUILD_DIR -Recurse -File -Filter $ext -ErrorAction SilentlyContinue
    foreach ($file in $files) {
        try {
            $content = [System.IO.File]::ReadAllText($file.FullName)
            # Convert CRLF to LF
            $content = $content -replace "`r`n", "`n"
            # Write with UTF8 encoding without BOM
            $utf8NoBom = New-Object System.Text.UTF8Encoding $false
            [System.IO.File]::WriteAllText($file.FullName, $content, $utf8NoBom)
            $convertedCount++
        } catch {
            Write-Host "  Warning: Could not convert $($file.Name): $_" -ForegroundColor Yellow
        }
    }
}

Write-Host "Converted $convertedCount files to Unix line endings (LF)" -ForegroundColor Green

# Check for tar command
Write-Host ""
Write-Host "Checking for tar command..." -ForegroundColor Yellow

$tarAvailable = $false
try {
    $tarVersion = tar --version 2>&1
    if ($tarVersion -match "tar") {
        $tarAvailable = $true
        Write-Host "tar command found - creating tar.gz archive..." -ForegroundColor Green
        
        Push-Location "build"
        tar -czf "$PACKAGE_NAME.tar.gz" $PACKAGE_NAME 2>&1
        Pop-Location
        
        if (Test-Path "build\$PACKAGE_NAME.tar.gz") {
            $archiveSize = (Get-Item "build\$PACKAGE_NAME.tar.gz").Length / 1MB
            Write-Host "Archive created: build\$PACKAGE_NAME.tar.gz ($([math]::Round($archiveSize, 2)) MB)" -ForegroundColor Green
            
            # Generate checksums
            Write-Host "Generating checksums..." -ForegroundColor Green
            $sha256 = (Get-FileHash "build\$PACKAGE_NAME.tar.gz" -Algorithm SHA256).Hash
            $md5 = (Get-FileHash "build\$PACKAGE_NAME.tar.gz" -Algorithm MD5).Hash
            
            "$sha256  $PACKAGE_NAME.tar.gz" | Out-File -FilePath "build\$PACKAGE_NAME.tar.gz.sha256" -Encoding ASCII
            "$md5  $PACKAGE_NAME.tar.gz" | Out-File -FilePath "build\$PACKAGE_NAME.tar.gz.md5" -Encoding ASCII
        }
    }
} catch {
    $tarAvailable = $false
}

if (-not $tarAvailable) {
    Write-Host "tar command not available - package directory created without archive" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "To create tar.gz archive:" -ForegroundColor Yellow
    Write-Host "  1. Install WSL or Git Bash" -ForegroundColor White
    Write-Host "  2. Run: tar -czf openwxsdr-$VERSION.tar.gz -C build openwxsdr-$VERSION" -ForegroundColor White
    Write-Host ""
    Write-Host "Or transfer the build\$PACKAGE_NAME directory directly to Raspberry Pi" -ForegroundColor White
}

# Display results
Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host "  Package Build Complete!" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Package directory: build\$PACKAGE_NAME" -ForegroundColor Green
Write-Host "Files: $fileCount" -ForegroundColor White
Write-Host "Size: $([math]::Round($dirSize, 2)) MB" -ForegroundColor White
Write-Host ""

if (Test-Path "build\$PACKAGE_NAME.tar.gz") {
    Write-Host "Archive: build\$PACKAGE_NAME.tar.gz" -ForegroundColor Green
    Write-Host "SHA256: " -NoNewline -ForegroundColor White
    Write-Host (Get-Content "build\$PACKAGE_NAME.tar.gz.sha256" | ForEach-Object { $_.Split(' ')[0] }) -ForegroundColor Gray
    Write-Host ""
    
    Write-Host "To deploy to Raspberry Pi:" -ForegroundColor Yellow
    Write-Host "  scp build\$PACKAGE_NAME.tar.gz pi@raspberry-pi:~/" -ForegroundColor White
    Write-Host "  ssh pi@raspberry-pi" -ForegroundColor White
    Write-Host "  tar -xzf $PACKAGE_NAME.tar.gz" -ForegroundColor White
    Write-Host "  cd $PACKAGE_NAME" -ForegroundColor White
    Write-Host "  ./install.sh" -ForegroundColor White
} else {
    Write-Host "To deploy directory to Raspberry Pi:" -ForegroundColor Yellow
    Write-Host "  scp -r build\$PACKAGE_NAME pi@raspberry-pi:~/" -ForegroundColor White
    Write-Host "  ssh pi@raspberry-pi" -ForegroundColor White
    Write-Host "  cd $PACKAGE_NAME" -ForegroundColor White
    Write-Host "  chmod +x *.sh" -ForegroundColor White
    Write-Host "  ./install.sh" -ForegroundColor White
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Cyan
