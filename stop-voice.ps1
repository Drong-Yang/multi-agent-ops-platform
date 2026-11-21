param([switch]$StopInfrastructure)
# A single stop path owns both ordinary services and the private callback tunnel.
& (Join-Path $PSScriptRoot 'stop.ps1') -StopInfrastructure:$StopInfrastructure
