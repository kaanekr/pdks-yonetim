param(
    [ValidateSet('Core', 'Firmware', 'SdFirmware', 'SoftSdFirmware', 'TxFirmware', 'PublicFirmware', 'Filesystem', 'All')]
    [string]$Target = 'All'
)

# Local checks only: this script NEVER uploads or erases a connected board.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$env:PLATFORMIO_CORE_DIR = Join-Path $root '.platformio'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = 'python' }

Push-Location $root
try {
    if ($Target -in @('Core', 'All')) {
        & $python 'tools/test_core.py'
        if ($LASTEXITCODE -ne 0) { throw 'Mesai cekirdegi testleri basarisiz.' }
        & $python 'tools/test_card_gate.py'
        if ($LASTEXITCODE -ne 0) { throw 'Kart cekme / tek okutma testleri basarisiz.' }
        & $python 'tools/test_network.py'
        if ($LASTEXITCODE -ne 0) { throw 'Wi-Fi ayar / kalicilik / AP-STA testleri basarisiz.' }
        & $python 'tools/test_journal.py'
        if ($LASTEXITCODE -ne 0) { throw 'Kayit kodlayici testleri basarisiz.' }
        & $python 'tools/test_management.py'
        if ($LASTEXITCODE -ne 0) { throw 'Yonetim/kurtarma kalicilik testleri basarisiz.' }
        & $python 'tools/test_manual_attendance.py'
        if ($LASTEXITCODE -ne 0) { throw 'Manuel mesai kalicilik/cakisma testleri basarisiz.' }
        & $python 'test/test_windows_app.py'
        if ($LASTEXITCODE -ne 0) { throw 'Windows istemci testleri basarisiz.' }
        & $python 'test/test_windows_gui.py'
        if ($LASTEXITCODE -ne 0) { throw 'Windows arayuz/onay testleri basarisiz.' }
        & $python 'test/test_manual_windows.py'
        if ($LASTEXITCODE -ne 0) { throw 'Manuel mesai Windows istemci testleri basarisiz.' }
        & $python -B 'test/test_windows_settings.py'
        if ($LASTEXITCODE -ne 0) { throw 'Windows baglanti ayarlari testleri basarisiz.' }
        & $python -B 'test/test_settings_gui.py'
        if ($LASTEXITCODE -ne 0) { throw 'Windows baglanti hatirlama arayuz testleri basarisiz.' }
        foreach ($suite in @('model', 'engine', 'edges', 'store', 'export', 'planning', 'reports', 'integration')) {
            & $python -B "test/test_workforce_$suite.py"
            if ($LASTEXITCODE -ne 0) { throw "Windows puantaj A paketi testi basarisiz: $suite" }
        }
        & $python -B 'test/test_refresh_selection.py'
        if ($LASTEXITCODE -ne 0) { throw 'Windows yenilemede secim koruma testleri basarisiz.' }
        foreach ($suite in @('named_client', 'named_store', 'named_reports', 'journal_actions', 'delete_gui', 'deletion_smoke')) {
            & $python -B "test/test_$suite.py"
            if ($LASTEXITCODE -ne 0) { throw "Isimli kayit/kalici silme testi basarisiz: $suite" }
        }
        foreach ($suite in @('named_core', 'named_storage', 'named_sd', 'delete_api')) {
            & $python -B "tools/test_$suite.py"
            if ($LASTEXITCODE -ne 0) { throw "Cihaz isimli kayit/kalici silme testi basarisiz: $suite" }
        }
        & $python 'tools/test_buzzer.py'
        if ($LASTEXITCODE -ne 0) { throw 'Buzzer surucu testleri basarisiz.' }
        & $python 'tools/test_feedback.py'
        if ($LASTEXITCODE -ne 0) { throw 'Ses dizisi / yesil LED testleri basarisiz.' }
        & $python 'tools/test_attendance_reset.py'
        if ($LASTEXITCODE -ne 0) { throw 'Tek seferlik personel/gecmis sifirlama testleri basarisiz.' }
        & $python 'tools/test_console.py'
        if ($LASTEXITCODE -ne 0) { throw 'UART/TX konsol testleri basarisiz.' }
        & $python 'tools/test_soft_sd_spi.py'
        if ($LASTEXITCODE -ne 0) { throw 'Yazilimsal SPI surucu testleri basarisiz.' }
        & $python 'tools/test_panel.py'
        if ($LASTEXITCODE -ne 0) { throw 'Panel kontrolleri basarisiz (Node.js gerekli).' }
        & $python 'tools/test_display.py'
        if ($LASTEXITCODE -ne 0) { throw 'OLED yerlesim testleri basarisiz.' }
        foreach ($suite in @('device_auth', 'ota_policy', 'ota_server')) {
            & $python -B "tools/test_$suite.py"
            if ($LASTEXITCODE -ne 0) { throw "OTA/kimlik testi basarisiz: $suite" }
        }
        foreach ($suite in @('updates', 'self_update', 'update_integration', 'release_packaging')) {
            & $python -B "test/test_$suite.py"
            if ($LASTEXITCODE -ne 0) { throw "Windows guncelleme testi basarisiz: $suite" }
        }
    }
    if ($Target -in @('Firmware', 'All')) {
        & $python -m platformio run -e nodemcuv2
        if ($LASTEXITCODE -ne 0) { throw 'Firmware derlemesi basarisiz.' }
    }
    if ($Target -in @('SdFirmware', 'All')) {
        & $python -m platformio run -e nodemcuv2_sd
        if ($LASTEXITCODE -ne 0) { throw 'SD secenekli firmware derlemesi basarisiz.' }
    }
    if ($Target -in @('SoftSdFirmware', 'All')) {
        & $python -m platformio run -e nodemcuv2_sd_soft
        if ($LASTEXITCODE -ne 0) { throw 'Ayri MISO yazilimsal SD derlemesi basarisiz.' }
    }
    if ($Target -in @('Filesystem', 'All')) {
        & $python -m platformio run -e nodemcuv2 -t buildfs
        if ($LASTEXITCODE -ne 0) { throw 'Dosya sistemi imaji olusturulamadi.' }
    }
    if ($Target -in @('TxFirmware', 'All')) {
        & $python -m platformio run -e nodemcuv2_sd_soft_tx
        if ($LASTEXITCODE -ne 0) { throw 'TX buzzer firmware derlemesi basarisiz.' }
    }
    if ($Target -eq 'PublicFirmware') {
        & $python -m platformio run -e nodemcuv2_sd_soft_tx_public
        if ($LASTEXITCODE -ne 0) { throw 'Herkese acik imzali OTA firmware derlemesi basarisiz.' }
    }
} finally {
    Pop-Location
}
