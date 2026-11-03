; Windows installer. Built by .github/workflows/build.yml: iscc /DAppVersion=1.0.5 installer\ugc-cutter.iss
; Installs for the current user, so it never asks for an admin password.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7C2B1E54-3A9F-4D6E-9B18-5E0F2A7C4D31}
AppName=UGC Cutter
AppVersion={#AppVersion}
AppPublisher=drewdisciplined
DefaultDirName={autopf}\UGC Cutter
DefaultGroupName=UGC Cutter
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ugc-cutter-setup
Compression=lzma2/fast
SolidCompression=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\ugc-cutter.exe
UninstallDisplayName=UGC Cutter
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\ugc-cutter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; a new version must not keep libraries from the old one
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{group}\UGC Cutter"; Filename: "{app}\ugc-cutter.exe"
Name: "{autodesktop}\UGC Cutter"; Filename: "{app}\ugc-cutter.exe"; Tasks: desktopicon

[Run]
; no skipifsilent: after a silent update from the app's button, this opens the new version
Filename: "{app}\ugc-cutter.exe"; Description: "Open UGC Cutter"; Flags: nowait postinstall
