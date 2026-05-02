; Inno Setup script — wraps the PyInstaller folder into a Windows installer.
;
; Build sequence (from repo root):
;     pyinstaller packaging/prompt-enhancer.spec --clean
;     iscc packaging/installer.iss
;
; Output: release/prompt-enhancer-setup.exe

#define MyAppName       "Prompt Enhancer"
#define MyAppVersion    "0.1.0"
#define MyAppPublisher  "halkive"
#define MyAppExeName    "prompt-enhancer.exe"

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
