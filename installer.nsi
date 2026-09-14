; GameBox installer
; Build with:  makensis installer.nsi
Unicode true

!define APP      "GameBox"
!define VERSION  "1.3.0"
!define PUBLISHER "That's Life Media"
!define WEBSITE  "https://github.com/zmaya13/gamebox"

Name "${APP} ${VERSION}"
OutFile "GameBox-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\GameBox"
InstallDirRegKey HKCU "Software\${APP}" "InstallDir"
RequestExecutionLevel user          ; per-user install, no UAC prompt
SetCompressor /SOLID lzma
BrandingText "${APP} ${VERSION}"

!include "MUI2.nsh"
!define MUI_ICON "GameBox.ico"
!define MUI_UNICON "GameBox.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\GameBox.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Open GameBox now"
!define MUI_WELCOMEPAGE_TITLE "Install ${APP}"
!define MUI_WELCOMEPAGE_TEXT "GameBox brings your Steam, GOG, Epic, EA and emulated games into one library, and launches them.$\r$\n$\r$\nIt installs for the current user only - no administrator rights needed."

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "GameBox" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  File "GameBox.exe"
  ; The library lives in library.json now, not inside the page, so the page is
  ; just program code and an upgrade can replace it like any other file. That
  ; retires the GameBox.blank.html copy-on-first-run dance this used to need.
  File "GameBox.html"
  File "GameBox.ico"
  File "scan-library.py"
  File "library.py"
  File "emulators.py"
  File "playtime.py"
  File "stores.py"
  File "emulators.json"
  File "migrate.py"
  File "README.md"
  File "LICENSE"

  CreateShortcut "$SMPROGRAMS\GameBox.lnk" "$INSTDIR\GameBox.exe" "" "$INSTDIR\GameBox.ico" 0
  CreateShortcut "$DESKTOP\GameBox.lnk"    "$INSTDIR\GameBox.exe" "" "$INSTDIR\GameBox.ico" 0

  WriteRegStr HKCU "Software\${APP}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}" "DisplayName" "${APP}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}" "Publisher" "${PUBLISHER}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}" "DisplayIcon" "$INSTDIR\GameBox.ico"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}" "URLInfoAbout" "${WEBSITE}"
  WriteRegStr HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}" "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}" "NoModify" 1
  WriteRegDWORD HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}" "NoRepair" 1
  WriteUninstaller "$INSTDIR\Uninstall.exe"
SectionEnd

Section "Uninstall"
  ; --- the app itself
  Delete "$INSTDIR\GameBox.exe"
  Delete "$INSTDIR\GameBox.html"
  Delete "$INSTDIR\GameBox.blank.html"   ; left by an install from before 1.3.0
  Delete "$INSTDIR\GameBox.ico"
  Delete "$INSTDIR\scan-library.py"
  Delete "$INSTDIR\library.py"
  Delete "$INSTDIR\emulators.py"
  Delete "$INSTDIR\playtime.py"
  Delete "$INSTDIR\stores.py"
  Delete "$INSTDIR\migrate.py"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\selftest.log"
  Delete "$INSTDIR\LICENSE"
  Delete "$SMPROGRAMS\GameBox.lnk"
  Delete "$DESKTOP\GameBox.lnk"
  DeleteRegKey HKCU "Software\${APP}"
  DeleteRegKey HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}"

  ; --- your data: library.json (the library, and everything you recorded about
  ;     your games), its artwork and backups, your name and settings, and the
  ;     browser profile. Removed unless you say otherwise; a silent uninstall
  ;     removes it without asking.
  StrCpy $0 "yes"
  IfSilent remove_data
  MessageBox MB_YESNO|MB_ICONQUESTION \
    "Also remove your GameBox data?$\r$\n$\r$\nThis deletes the scanned library and its artwork, everything you recorded about your games - favourites, tags, notes, playtime - your name and settings, and the window size GameBox remembers.$\r$\n$\r$\nYour games themselves are not touched - only what GameBox wrote about them." \
    IDYES remove_data
  StrCpy $0 "no"

remove_data:
  StrCmp $0 "yes" 0 keep_data
  Delete "$INSTDIR\library.json"
  Delete "$INSTDIR\gamebox-settings.json"
  Delete "$INSTDIR\emulators.json"
  Delete "$INSTDIR\launch.json"          ; retired in 1.3.0, folded into the library
  Delete "$INSTDIR\GameBox.pre-1.3.html" ; the page an upgrade migrated from
  RMDir /r "$INSTDIR\art"
  RMDir /r "$INSTDIR\library-backups"
  ; The WebView2 profile is usually still locked by the browser process that is
  ; only now shutting down, so give it a few tries rather than leaving it.
  StrCpy $1 0
webview_retry:
  RMDir /r "$LOCALAPPDATA\GameBox"
  IfFileExists "$LOCALAPPDATA\GameBox\*.*" 0 keep_data
  IntOp $1 $1 + 1
  IntCmp $1 12 keep_data 0 keep_data
  Sleep 500
  Goto webview_retry

keep_data:
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
SectionEnd
