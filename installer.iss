; =============================================================================
;  Captain's Sidestick — Inno Setup Installer Script
;  Requires Inno Setup 6.x  (https://jrsoftware.org/isinfo.php)
;
;  What this installer does, in order:
;   1. Silently installs the ViGEmBus kernel driver (Xbox 360 emulation)
;   2. Installs CaptainsSidestick.exe to Program Files
;   3. Creates a Start Menu shortcut
;   4. Creates a Desktop shortcut (optional, user-controlled)
;   5. Registers an Uninstall entry in Add/Remove Programs
;
;  To compile:
;   ISCC.exe installer.iss
;  or open installer.iss in the Inno Setup IDE and press F9.
;
;  ViGEmBus silent installer must be placed next to this .iss file:
;   ViGEmBusSetup_x64.exe
;  Download from: https://github.com/nefarius/ViGEmBus/releases
; =============================================================================

#define MyAppName        "Captain's Sidestick"
#define MyAppVersion     "1.0.0"
#define MyAppPublisher   "Captain's Sidestick Project"
#define MyAppURL         "https://github.com/CaptainsSidestick"
#define MyAppExeName     "CaptainsSidestick.exe"
#define ViGEmInstaller   "ViGEmBusSetup_x64.exe"

[Setup]
; Unique application GUID — change this if you fork the project
AppId={{A3F2B1C4-7E8D-4F0A-9B2C-1D3E5F6A7B8C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}

; Installation directory
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}

; Installer appearance
WizardStyle=modern
WizardImageFile=compiler:WizModernImage-IS.bmp
WizardSmallImageFile=compiler:WizModernSmallImage-IS.bmp

; Output settings
OutputDir=installer_output
OutputBaseFilename=CaptainsSidestick_Setup_{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes

; Windows requirements
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; Require administrator (ViGEmBus needs it)
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

; Uninstall support
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}

; Show changelog on finish
; ChangesAssociations=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Optional desktop shortcut
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Main application executable (built by PyInstaller)
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

; ViGEmBus driver installer — present only if the file exists
; Remove the next line if you are distributing without the ViGEmBus installer
Source: "{#ViGEmInstaller}"; DestDir: "{tmp}"; Flags: ignoreversion deleteafterinstall; Check: FileExists(ExpandConstant('{src}\{#ViGEmInstaller}'))

; README / licence (optional — add your own)
; Source: "README.txt"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
; Start Menu
Name: "{group}\{#MyAppName}";            Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}";  Filename: "{uninstallexe}"

; Desktop (only if task selected)
Name: "{autodesktop}\{#MyAppName}";      Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; ─── Step 1: Install ViGEmBus silently ────────────────────────────────────
; /install  = install the driver
; /quiet    = no UI
; /norestart = suppress reboot prompt (user decides later)
Filename: "{tmp}\{#ViGEmInstaller}"; \
    Parameters: "/install /quiet /norestart"; \
    StatusMsg: "Installing ViGEmBus virtual controller driver..."; \
    Flags: waituntilterminated; \
    Check: FileExists(ExpandConstant('{tmp}\{#ViGEmInstaller}'))

; ─── Step 2: Launch the app after install (optional) ─────────────────────
Filename: "{app}\{#MyAppExeName}"; \
    Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop any running instance before uninstall
Filename: "taskkill.exe"; Parameters: "/f /im {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
// ==========================================================================
//  Custom Inno Setup Pascal code
// ==========================================================================

// Check if ViGEmBus is already installed by looking for its uninstall key.
// Returns True if NOT installed (so the installer should run it).
function ViGEmBusNotInstalled(): Boolean;
var
  SubKey: String;
  Value:  String;
begin
  SubKey := 'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\ViGEmBus';
  Result := not RegQueryStringValue(HKLM, SubKey, 'DisplayName', Value);
end;

// Called by the [Files] Check function above.
// Only copies the ViGEmBus installer if it hasn't been installed yet.
function ShouldInstallViGEm(): Boolean;
begin
  Result := ViGEmBusNotInstalled();
end;

procedure InitializeWizard();
begin
  // Customise the welcome page message
  WizardForm.WelcomeLabel2.Caption :=
    'This will install ' + '{#MyAppName}' + ' version ' + '{#MyAppVersion}' + '.' + #13#10 + #13#10 +
    'The installer will also install the ViGEmBus virtual controller driver ' +
    'if it is not already present. This driver allows the app to emulate ' +
    'an Xbox 360 gamepad.' + #13#10 + #13#10 +
    'Administrator rights are required.';
end;

// Warn the user if a reboot may be needed for the driver
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    if ViGEmBusNotInstalled() then
    begin
      MsgBox(
        'ViGEmBus has been installed.' + #13#10 + #13#10 +
        'You may need to restart Windows before the virtual controller ' +
        'appears in Device Manager. The application will still launch now.',
        mbInformation, MB_OK
      );
    end;
  end;
end;
