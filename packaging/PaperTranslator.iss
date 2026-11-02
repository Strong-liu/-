#ifndef PayloadDir
  #define PayloadDir "..\build\payload"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#define ProductVersion "0.3.2"
#if FileExists(PayloadDir + "\assets\branding\app.ico")
  #define ShortcutIcon "{app}\assets\branding\app.ico"
#else
  #define ShortcutIcon "{app}\PaperTranslator.exe"
#endif

[Setup]
AppId={{2B3F504D-3ECD-4D43-A4AE-A36C0317DB46}
AppName=橙意翻译助手
AppVersion={#ProductVersion}
AppPublisher=Strong-liu
AppPublisherURL=https://github.com/Strong-liu/-
AppSupportURL=https://github.com/Strong-liu/-/issues
DefaultDirName={localappdata}\Programs\PaperTranslator
DefaultGroupName=橙意翻译助手
UsePreviousGroup=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.10240
OutputDir={#OutputDir}
OutputBaseFilename=ChengyiTranslationAssistant-{#ProductVersion}-Windows-x64-Setup
Compression=lzma2/fast
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
UninstallDisplayIcon={#ShortcutIcon}
#if FileExists(PayloadDir + "\assets\branding\app.ico")
SetupIconFile={#PayloadDir}\assets\branding\app.ico
#endif
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
DisableProgramGroupPage=yes
LicenseFile={#PayloadDir}\engine\LICENSE
InfoAfterFile={#PayloadDir}\INSTALLATION_NOTES.txt

[Tasks]
Name: desktopicon; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式:"; Flags: checkedonce

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Excludes: "*.db,*.db-*,*.sqlite,*.sqlite3,*.log,*.pyc,*.tmp,trans.exe,TRANS_CLI_*,checksums.txt,direct_url.json,pyvenv.cfg,*.pth,engine_support\bin\*,__pycache__\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#PayloadDir}\PaperTranslator.exe"; DestDir: "{tmp}"; DestName: "ChengyiUpgradeHelper.exe"; Flags: dontcopy

[InstallDelete]
Type: files; Name: "{autodesktop}\Paper Translator.lnk"
Type: files; Name: "{userprograms}\Paper Translator\Paper Translator.lnk"
Type: files; Name: "{userprograms}\Paper Translator\Uninstall Paper Translator.lnk"
Type: dirifempty; Name: "{userprograms}\Paper Translator"

[Icons]
Name: "{group}\橙意翻译助手"; Filename: "{app}\PaperTranslator.exe"; WorkingDir: "{app}"; IconFilename: "{#ShortcutIcon}"
Name: "{autodesktop}\橙意翻译助手"; Filename: "{app}\PaperTranslator.exe"; WorkingDir: "{app}"; IconFilename: "{#ShortcutIcon}"; Tasks: desktopicon
Name: "{group}\卸载橙意翻译助手"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\PaperTranslator.exe"; Description: "打开橙意翻译助手"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\PaperTranslator.exe')) then
  begin
    ExtractTemporaryFile('ChengyiUpgradeHelper.exe');
    if not Exec(ExpandConstant('{tmp}\ChengyiUpgradeHelper.exe'),
      '--stop-for-upgrade "' + ExpandConstant('{app}') + '"', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
      Result := '无法检查正在运行的翻译助手，请关闭软件后重新安装。'
    else if ExitCode <> 0 then
      Result := '翻译助手仍有活动任务或无法确认后台服务空闲。请等待任务完成并退出旧版服务后，再继续安装。';
  end;
end;
