[Setup]
AppName=Lightbucket Astro Planner
AppVersion=1.0.0
AppPublisher=Lightbucket Astro [Gerald Walters]
DefaultDirName={localappdata}\Programs\LightbucketAstroPlanner
DefaultGroupName=Lightbucket Astro Planner
OutputDir=installer
OutputBaseFilename=LightbucketAstroPlanner-1.0.0-setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
SetupIconFile=..\logo.ico
WizardStyle=modern

[Files]
Source: "dist\AstroPlannerBeta7\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Lightbucket Astro Planner"; Filename: "{app}\AstroPlanner.exe"
Name: "{autodesktop}\Lightbucket Astro Planner"; Filename: "{app}\AstroPlanner.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked
