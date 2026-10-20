; NSIS installer script for the Sentinel.
;
; Build with build_windows.bat, which passes the version from the VERSION file
; and the architecture it was built for:
;   makensis /DVERSION=1.0.1 /DARCH=amd64 /DOUTFILE="dist\Sentinel-Setup-1.0.1.exe" build/windows/installer.nsi
;
; The PyInstaller COLLECT output (dist\sentinel\) is wrapped
; into a single Setup.exe that installs to %ProgramFiles64%.
;
; ARCH is the architecture of the payload (amd64 or arm64), not of NSIS
; itself: makensis is a 32-bit/x64 program and runs under Windows on ARM's
; emulation, while the sentinel.exe it packages is native ARM64. It only
; labels the install, so Add/Remove Programs can tell the two apart.

Unicode True
SetCompressor /SOLID lzma
SetDatablockOptimize on
ShowInstDetails hide
ShowUninstDetails hide

!define APPNAME "Sentinel"
!define COMPANYNAME "Arena"
!define DESCRIPTION "Discord server management bot: moderation, applications, roles and a web dashboard."

!ifndef VERSION
    ; Only reached when makensis is run by hand: build_windows.bat always
    ; passes /DVERSION, taken from the VERSION file at the project root.
    !define VERSION "0.0.0+unknown"
!endif
!ifndef OUTFILE
    !define OUTFILE "dist\Sentinel-Setup-${VERSION}.exe"
!endif
!ifndef ARCH
    ; Only reached when makensis is run by hand: build_windows.bat always
    ; passes /DARCH.
    !define ARCH "amd64"
!endif

; The architecture suffix shown in Add/Remove Programs, so an arm64 install
; is distinguishable from an x64 one.
!if "${ARCH}" == "arm64"
    !define ARCHLABEL " (ARM64)"
!else
    !define ARCHLABEL ""
!endif

Name "${APPNAME} ${VERSION}${ARCHLABEL}"
OutFile "${OUTFILE}"
InstallDir "$PROGRAMFILES64\${APPNAME}"
InstallDirRegKey HKLM "Software\${COMPANYNAME}\${APPNAME}" "Install_Dir"
RequestExecutionLevel admin

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"

!define MUI_ABORTWARNING

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_WELCOME
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_UNPAGE_FINISH
!insertmacro MUI_LANGUAGE "English"

Section "Install"
    SectionIn RO

    ; Write install dir to registry for the uninstaller.
    WriteRegStr HKLM "Software\${COMPANYNAME}\${APPNAME}" "Install_Dir" "$INSTDIR"
    WriteRegStr HKLM "Software\${COMPANYNAME}\${APPNAME}" "Version" "${VERSION}"
    WriteRegStr HKLM "Software\${COMPANYNAME}\${APPNAME}" "Architecture" "${ARCH}"

    ; Copy the PyInstaller output.
    SetOutPath "$INSTDIR"
    File /r "..\..\dist\sentinel\*"

    ; Start Menu shortcuts.
    CreateDirectory "$SMPROGRAMS\${APPNAME}"
    CreateShortcut "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk" \
        "$INSTDIR\sentinel.exe" "" "$INSTDIR\sentinel.exe" 0
    CreateShortcut "$SMPROGRAMS\${APPNAME}\Uninstall.lnk" \
        "$INSTDIR\uninstall.exe"

    ; Desktop shortcut.
    CreateShortcut "$DESKTOP\${APPNAME}.lnk" \
        "$INSTDIR\sentinel.exe"

    ; Note: We do not add $INSTDIR to the system PATH because the
    ; EnVar plugin (which NSIS's PATH-modification macros use) is
    ; not bundled with the standard NSIS install. Users who want
    ; command-line access can add the install dir to their PATH
    ; manually, or symlink the binary into a directory already
    ; on PATH.

    ; Optional: install as a Windows Service via NSSM (if present).
    ;
    ; nsExec::ExecToLog '"$INSTDIR\sentinel.exe" --install-service'

    ; Write the uninstaller.
    WriteUninstaller "$INSTDIR\uninstall.exe"

    ; Register the uninstaller in Add/Remove Programs.
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "DisplayName" "${APPNAME}${ARCHLABEL}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "DisplayVersion" "${VERSION}"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "Publisher" "${COMPANYNAME}"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "NoModify" 1
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}" \
        "NoRepair" 1
SectionEnd

Section "Uninstall"
    ; Remove the install directory.
    RMDir /r "$INSTDIR"

    ; Remove shortcuts.
    Delete "$SMPROGRAMS\${APPNAME}\${APPNAME}.lnk"
    Delete "$SMPROGRAMS\${APPNAME}\Uninstall.lnk"
    RMDir "$SMPROGRAMS\${APPNAME}"
    Delete "$DESKTOP\${APPNAME}.lnk"

    ; No PATH removal needed since we don't add to PATH on install
    ; (the EnVar plugin is not bundled with the standard NSIS).

    ; Remove registry keys.
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\${COMPANYNAME} ${APPNAME}"
    DeleteRegKey HKLM "Software\${COMPANYNAME}\${APPNAME}"
SectionEnd
