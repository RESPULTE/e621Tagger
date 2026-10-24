param(
    [Parameter(Mandatory=$true)][ValidateRange(1, 1000000)][int]$Count,
    [Parameter(Mandatory=$true)][ValidateRange(1, 1000)][int]$VideoFrames
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$bodyFont = [System.Drawing.Font]::new('Segoe UI', 16)
$headingFont = [System.Drawing.Font]::new('Segoe UI', 22, [System.Drawing.FontStyle]::Bold)
$hintFont = [System.Drawing.Font]::new('Segoe UI', 12)
$form = [System.Windows.Forms.Form]::new()
$form.Text = 'Home media processing'
$form.Font = $bodyFont
$form.AutoScaleMode = [System.Windows.Forms.AutoScaleMode]::Dpi
$form.AutoSize = $true
$form.AutoSizeMode = [System.Windows.Forms.AutoSizeMode]::GrowAndShrink
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$layout = [System.Windows.Forms.TableLayoutPanel]::new()
$layout.ColumnCount = 1
$layout.AutoSize = $true
$layout.Dock = [System.Windows.Forms.DockStyle]::Fill
$layout.Padding = [System.Windows.Forms.Padding]::new(24)

$heading = [System.Windows.Forms.Label]::new()
$heading.Text = "Tag $Count files?"
$heading.Font = $headingFont
$heading.AutoSize = $true
$heading.Margin = [System.Windows.Forms.Padding]::new(0, 0, 0, 12)
$layout.Controls.Add($heading)

$details = [System.Windows.Forms.Label]::new()
$details.Text = "RTX 4070 | $VideoFrames frames/video"
$details.AutoSize = $true
$details.Margin = [System.Windows.Forms.Padding]::new(0, 0, 0, 12)
$layout.Controls.Add($details)

$hint = [System.Windows.Forms.Label]::new()
$hint.Text = 'Later: ask again after reconnecting.'
$hint.Font = $hintFont
$hint.AutoSize = $true
$hint.Margin = [System.Windows.Forms.Padding]::new(0)
$layout.Controls.Add($hint)

$buttons = [System.Windows.Forms.FlowLayoutPanel]::new()
$buttons.AutoSize = $true
$buttons.Dock = [System.Windows.Forms.DockStyle]::Fill
$buttons.FlowDirection = [System.Windows.Forms.FlowDirection]::RightToLeft
$buttons.Padding = [System.Windows.Forms.Padding]::new(0, 20, 0, 0)

$later = [System.Windows.Forms.Button]::new()
$later.Text = 'Later'
$later.AutoSize = $true
$later.Padding = [System.Windows.Forms.Padding]::new(14, 6, 14, 6)
$later.DialogResult = [System.Windows.Forms.DialogResult]::No
$buttons.Controls.Add($later)

$start = [System.Windows.Forms.Button]::new()
$start.Text = 'Start'
$start.AutoSize = $true
$start.Padding = [System.Windows.Forms.Padding]::new(14, 6, 14, 6)
$start.DialogResult = [System.Windows.Forms.DialogResult]::Yes
$buttons.Controls.Add($start)
$layout.Controls.Add($buttons)
$form.Controls.Add($layout)
$form.AcceptButton = $later
$form.CancelButton = $later

try {
    $choice = $form.ShowDialog()
} finally {
    $form.Dispose()
    $bodyFont.Dispose()
    $headingFont.Dispose()
    $hintFont.Dispose()
}
if ($choice -eq [System.Windows.Forms.DialogResult]::Yes) { 'APPROVED' } else { 'DEFERRED' }
