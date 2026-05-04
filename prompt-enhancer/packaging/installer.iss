; Inno Setup script — wraps the PyInstaller folder into a Windows installer.
;
; Build sequence (from repo root):
;     pyinstaller packaging/prompt-enhancer.spec --clean
;     iscc packaging/installer.iss
;
; Output: release/prompt-enhancer-setup.exe
;
; ─────────────────────────────────────────────────────────────────────
; EV-signed installer (deferred — wall-clock-bound on cert procurement).
;
; The v1.1 release plan called for an EV (Extended Validation) code-signing
; certificate, which costs ~$300-500/year and takes 3-7 business days to
; issue (CA does manual identity verification + ships a hardware token).
; Without it, Windows SmartScreen flags first-time downloaders with a "rep-
; utation unknown" warning until enough installs accumulate goodwill.
;
; To wire signing once the cert + token arrive:
;   1. Install the CA's hardware-token driver (eToken / SafeNet usually).
;   2. In Inno Setup IDE: Tools → Configure Sign Tools → add an entry
;      "ev_sign" with command e.g.
;        "C:\Program Files (x86)\Windows Kits\10\bin\10.0.22621.0\x64\signtool.exe"
;        sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 $f
;      The /n "<subject name>" or /sha1 "<thumbprint>" flag picks the cert
;      from the hardware token; consult the CA's docs.
;   3. Uncomment the SignTool directive below.
;   4. Rebuild via `pyinstaller packaging/prompt-enhancer.spec --clean`
;      then `iscc packaging/installer.iss`. The compiler invokes
;      ev_sign on the output exe and the bundled prompt-enhancer.exe
;      automatically when it sees the directive.
;
; Until then the unsigned installer at release/prompt-enhancer-setup.exe
; remains the v1.x and v2.x distributable — functionally identical, just
; without the publisher-verified UAC dialog.
; ─────────────────────────────────────────────────────────────────────

#define MyAppName       "Prompt Enhancer"
#define MyAppVersion    "2.0.1"
#define MyAppPublisher  "halkive"
#define MyAppExeName    "prompt-enhancer.exe"
; SignTool=ev_sign  ; ← uncomment when EV cert is provisioned (see header)

[Setup]
AppId={{F7A9C5F1-7B2E-4A13-9AE0-9F3B2A5E1F31}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\PromptEnhancer
DefaultGroupName={#MyAppName}
OutputDir=..\release
OutputBaseFilename=prompt-enhancer-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\prompt-enhancer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
