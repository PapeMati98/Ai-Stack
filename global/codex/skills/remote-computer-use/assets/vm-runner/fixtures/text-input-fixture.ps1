[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Remote Computer Use Text Fixture'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object System.Drawing.Size(760, 440)
$form.MinimumSize = New-Object System.Drawing.Size(520, 320)

$status = New-Object System.Windows.Forms.Label
$status.Dock = 'Bottom'
$status.Height = 36
$status.Padding = New-Object System.Windows.Forms.Padding(12, 8, 12, 8)
$status.Text = 'Characters: 0'

$inputBox = New-Object System.Windows.Forms.TextBox
$inputBox.Name = 'inputBox'
$inputBox.AccessibleName = 'Test input'
$inputBox.Dock = 'Fill'
$inputBox.Multiline = $true
$inputBox.AcceptsReturn = $true
$inputBox.Font = New-Object System.Drawing.Font('Segoe UI', 16)
$inputBox.ScrollBars = 'Vertical'

$inputBox.Add_TextChanged({
    $status.Text = 'Characters: ' + $inputBox.Text.Length
})
$form.Add_Shown({
    [void]$form.Activate()
    [void]$inputBox.Focus()
})

[void]$form.Controls.Add($inputBox)
[void]$form.Controls.Add($status)
[System.Windows.Forms.Application]::Run($form)
