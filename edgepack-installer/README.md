# EdgePack TUI Installer


A terminal-based installer for Intel EdgePack software packages, built with [Textual](https://textual.textualize.io/).

The installer walks you through a 4-step wizard:

1. **Profile selection** — choose your base profile (standard or realtime kernel) and detect your hardware platform
2. **Add-on packages** — select optional add-on profiles to install alongside the base
3. **Installation summary** — review compatibility warnings and confirm what will be installed
4. **Installation** — live progress as packages are downloaded and installed via `apt`

> EdgePack [installation setup guide](doc/installation_setup.md). Follow this guide to install EdgePack using TUI installer, guide below shows how to build the installer.
---

## Running from source

### Requirements

- Python **3.10** or newer
- A terminal that supports 256 colours

### Steps

```bash
# 1. Clone the repository
git clone https://github.com/open-edge-platform/edge-pack.git
cd edge-pack/edgepack-installer

# 2. (Recommended) Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run the TUI wizard (Ubuntu 24.04 / 26.04 only, requires sudo)
python3 -m tui

# Or install a profile directly from the CLI, no wizard (requires sudo)
sudo python3 -m tui install base-standard npu

# List available base profiles and add-ons
python3 -m tui list
```

---

## Building a standalone executable (PyInstaller)

The installer can be packaged into a single self-contained binary that requires no Python or dependencies on the target machine.

> **Important:** Build on **Ubuntu 24.04 (Noble)** to ensure glibc compatibility with target machines running Ubuntu 24.04 or 26.04.

```bash
# 1. Install binutils
sudo apt install binutils

# 2. Install PyInstaller
pip install pyinstaller

# 3. Build (uses edgepack-installer.spec)
./build.sh

# Output: dist/edgepack-installer
```

The `build.sh` script installs PyInstaller, runs the spec, and reports the output path.

### Distributing the binary

Copy `dist/edgepack-installer` to the target machine and run:

```bash
# Run the TUI wizard
./edgepack-installer

# Install a profile directly from the CLI, no wizard (requires sudo)
sudo ./edgepack-installer install base-standard npu

# List available base profiles and add-ons
./edgepack-installer list
```

No Python, no pip, no dependencies needed on the target.

---

## Installation log

Every installation run writes a log to:

```
/var/log/edgepack/edgepack-installer.log
```

The log includes a timestamp, the packages being installed, all `apt` output, and the final exit code. The path can be changed in `tui/screens/step4.py` (`LOG_PATH` constant).

---

## Project structure

```
.
├── tui/                        # Textual TUI application
│   ├── app.py                  #   Root app class and shared wizard state
│   ├── app.tcss                #   All CSS styles
│   ├── main.py                 #   CLI entry point (argparse)
│   ├── package_logic.py        #   Package tree logic (no UI dependencies)
│   └── screens/
│       ├── step1.py            #   Profile & platform selection
│       ├── step2.py            #   Add-on package selection
│       ├── step3.py            #   Installation summary & warnings
│       ├── step4.py            #   Installation progress & log
│       └── validation.py       #   Post-install validation (future)
├── edgepack_shared/            # UI-agnostic business logic
│   ├── processor.py            #   Parses edgepacks-template.yml
│   ├── host.py                 #   OS / CPU auto-detection
│   ├── install_logic.py        #   Builds and runs the apt script
│   └── package_status.py       #   Queries dpkg for installed versions
├── data/
│   └── edgepacks-template.yml  #   Package, profile, and platform definitions
├── edgepack-installer.spec     # PyInstaller build spec
├── build.sh                    # One-command build script
└── requirements.txt
```

---

## Keyboard navigation

| Key | Action |
|---|---|
| `Tab` / `Shift+Tab` | Move focus between sections |
| `↑` / `↓` | Move between items within a list (Step 2 add-ons) |
| `←` / `→` | Move between buttons |
| `Space` | Toggle a checkbox / press the focused button |
| `Enter` | Press the focused button |
| `Ctrl+Q` | Quit |

---
