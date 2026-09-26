param(
    [string]$Gradle = (Join-Path $PSScriptRoot 'gradlew.bat'),
    [string]$JavaHome,
    [string]$AndroidSdk,
    [string]$CacheDirectory
)
$ErrorActionPreference = 'Stop'
$savedEnvironment = @{}
foreach ($name in @('JAVA_HOME', 'ANDROID_HOME', 'ANDROID_SDK_ROOT', 'GRADLE_USER_HOME')) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}
try {
    # Toolchain paths affect this process only. The script does not install the SDK or accept licenses.
    if ($JavaHome) {
        $env:JAVA_HOME = (Resolve-Path -LiteralPath $JavaHome).Path
        if (-not (Test-Path -LiteralPath (Join-Path $env:JAVA_HOME 'bin/java.exe'))) {
            throw 'JavaHome must point to a JDK directory containing bin/java.exe'
        }
    }
    if ($AndroidSdk) {
        $env:ANDROID_HOME = (Resolve-Path -LiteralPath $AndroidSdk).Path
        $env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
    }
    if ($CacheDirectory) {
        $env:GRADLE_USER_HOME = [IO.Path]::GetFullPath($CacheDirectory)
    }
    if (Test-Path -LiteralPath $Gradle) { $Gradle = (Resolve-Path -LiteralPath $Gradle).Path }
    & $Gradle --version
    if ($LASTEXITCODE -ne 0) { throw 'Gradle unavailable; see ../README.md' }
    & $Gradle --project-dir $PSScriptRoot --no-daemon --console=plain :app:assembleDebug :app:lintDebug
    if ($LASTEXITCODE -ne 0) { throw 'Android build or lint failed' }
    $apk = Join-Path $PSScriptRoot 'app/build/outputs/apk/debug/app-debug.apk'
    Get-FileHash -LiteralPath $apk -Algorithm SHA256
} finally {
    foreach ($name in $savedEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
    }
}
