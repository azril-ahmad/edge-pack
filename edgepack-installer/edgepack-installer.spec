# edgepack-installer.spec — PyInstaller build spec
#
# Build with:  pyinstaller edgepack-installer.spec
#
# Must be built on Ubuntu 24.04 (Noble) to ensure glibc compatibility with
# the target Ubuntu 24.04 / 26.04 machines.

from PyInstaller.utils.hooks import collect_data_files

# Include Textual's bundled CSS/assets alongside the Python modules.
textual_datas = collect_data_files('textual')

a = Analysis(
    ['tui/__main__.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        # Project assets
        ('data/edgepacks-template.yml',            'data'),
        ('tui/app.tcss',                           'tui'),
        ('edgepack_shared/dep_resolver.py',       'edgepack_shared'),
        # Textual runtime assets (themes, default CSS, etc.)
        *textual_datas,
    ],
    hiddenimports=[
        # Textual widget modules loaded dynamically at runtime
        'textual.widgets._button',
        'textual.widgets._checkbox',
        'textual.widgets._footer',
        'textual.widgets._header',
        'textual.widgets._label',
        'textual.widgets._progress_bar',
        'textual.widgets._radio_button',
        'textual.widgets._radio_set',
        'textual.widgets._rich_log',
        'textual.widgets._rule',
        'textual.widgets._static',
        'textual.widgets._tabs',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'gi',        # GTK/GLib — not needed for TUI
        'tkinter',
        'matplotlib',
        'numpy',
        'readline',      # GPL-licensed libreadline.so must never ship in a closed-source binary
        'rlcompleter',   # only used for interactive shells; depends on readline
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='edgepack-installer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,         # strip ELF symbol tables from bundled binaries (smaller, less info for RE)
    upx=False,          # UPX can cause false-positive AV hits; disable for safety
    console=True,       # TUI requires a terminal
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
