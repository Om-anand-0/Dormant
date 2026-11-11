# Dormant hook for PowerShell (Windows PowerShell 5.1 and pwsh 7). Install in $PROFILE:
#   dormant hook powershell | Out-String | Invoke-Expression
# Wraps the existing prompt function. When $PWD changed, walks up looking for
# .dormant\state with no external process; `dormant wake` starts only when found.

function global:__DormantCheck {
    if ($env:DORMANT_DISABLE -eq '1' -or $PWD.Provider.Name -ne 'FileSystem') { return }
    $dir = $PWD.ProviderPath
    $root = $null
    while ($dir) {
        # [IO.File]::Exists, not Test-Path: ~7x faster (no provider layer), false for dirs.
        if ([System.IO.File]::Exists([System.IO.Path]::Combine($dir, '.dormant', 'state'))) {
            $root = $dir
            break
        }
        $dir = [System.IO.Path]::GetDirectoryName($dir)
    }
    if (-not $root) {
        $global:__DormantLastRoot = $null
        return
    }
    # One attempt per visit: no retry until the user leaves this root.
    if ($root -eq $global:__DormantLastRoot) { return }
    $global:__DormantLastRoot = $root
    $exe = Get-Command dormant -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $exe) { return }
    # Start-Process -NoNewWindow keeps wake on the real console (progress bar, prompts)
    # instead of folding its output into the prompt string.
    # Start-Process joins arguments verbatim: quote the path, doubling a trailing '\' (C:\).
    $arg = $root
    if ($arg.EndsWith('\')) { $arg += '\' }
    Start-Process -FilePath $exe.Source -ArgumentList 'wake', '--quiet', ('"' + $arg + '"') -NoNewWindow -Wait
}

if (-not (Test-Path Variable:global:__DormantPrevPrompt)) {
    $global:__DormantPrevPrompt = $function:prompt
    function global:prompt {
        # Run the previous prompt first so it still sees the user's $? and $LASTEXITCODE.
        $out = if ($global:__DormantPrevPrompt) { & $global:__DormantPrevPrompt } else { "PS $PWD> " }
        if ($PWD.Path -ne $global:__DormantLastPwd) {
            $global:__DormantLastPwd = $PWD.Path
            $code = $global:LASTEXITCODE
            __DormantCheck
            $global:LASTEXITCODE = $code
        }
        $out
    }
}
