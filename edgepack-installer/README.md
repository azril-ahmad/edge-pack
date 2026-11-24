# EdgePack TUI Installer

A terminal-based installer for Intel EdgePack software packages, built with [Textual](https://textual.textualize.io/).

The installer walks you through a 4-step wizard:

1. **Profile selection** — choose your base profile (standard or realtime kernel) and detect your hardware platform
2. **Add-on packages** — select optional add-on profiles to install alongside the base
3. **Installation summary** — review compatibility warnings and confirm what will be installed
4. **Installation** — live progress as packages are downloaded and installed via `apt`

---
## Requirements

### Supported Platforms

| Ecosystem | Support |
|---|---|
| Platform | Intel Panther Lake (PTL), Intel Wildcat Lake (WCL) |
| Linux Distro | Ubuntu 24.04.5 (Desktop), Ubuntu 26.04.1 (Desktop) |
| Kernel | Ubuntu Kernel 7.0+ generic, Ubuntu Kernel 7.0+ real-time (Ubuntu 26.04 only) |

### Supported SKUs

| Platform | Supported SKUs (CPU model) |
|---|---|
| Intel Panther Lake (PTL) | 315, 322, 325, 325H, 332, 335, 336H, 338H, 345H, 355, 355H, 356H, 358H, 360, 365, 366H, 368H, 375H, 386H, 388H |
| Intel Wildcat Lake (WCL) | 304, 305, 310, 320, 330, 350 |

## Getting started

For an overview of the installation options, see the
[Edge Pack installation guide](../docs/user-guide/get-started.md).

Download the installer binary from the
[public releases page](https://github.com/open-edge-platform/edge-pack/releases),
then follow the **[TUI installation guide](../docs/user-guide/get-started/install-tui.md)**.

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

## License

The installer is distributed under the [MIT License](LICENSE.txt).
