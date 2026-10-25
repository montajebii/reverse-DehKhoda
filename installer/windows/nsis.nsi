; =============================================================================
; installer/windows/nsis.nsi — standalone NSIS installer for Dehkhoda RAG
; (alternative to the Tauri-bundled NSIS; identical layout).
;
; Produces: Start-Menu shortcut + per-user writable data dir at runtime
; (%LOCALAPPDATA%\Dehkhoda, created by the app itself).
;
; Inputs (defined with !if / -D on the makensis command line):
;   makensis -DAPP_EXE=...\dehkhoda-rag.exe ^
;            -DBACKEND_DIR=...\resources\dehkhoda-server ^
;            -DQDRANT_DIR=...\resources\sidecars\qdrant ^
;            -DVERSION=1.2.0 installer\windows\nsis.nsi
;
; Signing: if the certificate env is provided in CI (WINDOWS_CERTIFICATE +
; WINDOWS_CERTIFICATE_PASSWORD), signtool signs the built installer after
; compilation. Without a certificate the README documents the SmartScreen
; "unsigned" warning users will see.
; =============================================================================

!ifndef APP_EXE
  !error "pass -DAPP_EXE=<path to dehkhoda-rag.exe>"
!endif
!ifndef BACKEND_DIR
  !error "pass -DBACKEND_DIR=<resources/dehkhoda-server dir>"
!endif
!ifndef VERSION
  !define VERSION "0.0.0"
!endif

!include "MUI2.nsh"
!include "FileFunc.nsh"

Name "Dehkhoda RAG ${VERSION}"
OutFile "dehkhoda-setup-win-${VERSION}.exe"
InstallDir "$PROGRAMFILES64\Dehkhoda RAG"
InstallDirRegKey HKLM "Software\DehkhodaRAG" "InstallDir"
RequestExecutionLevel admin
SetCompressor /SOLID lzma

!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath "$INSTDIR"
  File "/oname=dehkhoda-rag.exe" "${APP_EXE}"

  ; PyInstaller onedir backend
  SetOutPath "$INSTDIR\dehkhoda-server"
  File /nonfatal /a /r "${BACKEND_DIR}\*.*"

  ; Qdrant fallback sidecar (optional at build time)
  !ifdef QDRANT_DIR
    SetOutPath "$INSTDIR\sidecars\qdrant"
    File /nonfatal /a /r "${QDRANT_DIR}\*.*"
  !endif

  WriteRegStr HKLM "Software\DehkhodaRAG" "InstallDir" "$INSTDIR"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\DehkhodaRAG" \
                   "DisplayName" "Dehkhoda RAG"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\DehkhodaRAG" \
                   "UninstallString" "$INSTDIR\Uninstall.exe"
  WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\DehkhodaRAG" \
                   "DisplayVersion" "${VERSION}"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\Dehkhoda RAG"
  CreateShortCut "$SMPROGRAMS\Dehkhoda RAG\Dehkhoda RAG.lnk" "$INSTDIR\dehkhoda-rag.exe"
  CreateShortCut "$DESKTOP\Dehkhoda RAG.lnk" "$INSTDIR\dehkhoda-rag.exe"
SectionEnd

Section "Uninstall"
  RMDir /r "$INSTDIR"
  Delete "$SMPROGRAMS\Dehkhoda RAG\Dehkhoda RAG.lnk"
  RMDir "$SMPROGRAMS\Dehkhoda RAG"
  Delete "$DESKTOP\Dehkhoda RAG.lnk"
  DeleteRegKey HKLM "Software\DehkhodaRAG"
  DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\DehkhodaRAG"
SectionEnd
