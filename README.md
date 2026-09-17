# Edge Pack

[![License](https://img.shields.io/badge/License-MIT-blue.svg)](./LICENSE)

Edge Pack is a curated collection of Intel-validated software packages and drivers that unlock specific Intel platform capabilities — such as graphics and media acceleration, the Neural Processing Unit (NPU), platform manageability, and real-time kernel support — on top of stock Ubuntu LTS installations. Rather than hunting down individual drivers, PPAs, and kernel packages and figuring out how they fit together, EdgePack packages them into validated, ready-to-install profiles for supported Intel platforms.

*<mark>**[!NOTE]**</mark> Edge Pack is currently available as a **Tech Preview**.*

## What Edge Pack Provides

- **Base profiles** — the validated core package set for a given platform and OS, forming the required foundation before any add-on can be installed
- **Add-on profiles** — optional package groups layered on top of a base profile, each enabling a specific Intel platform capability
- **Platform and OS validation** — every profile and add-on is validated against specific hardware models, Ubuntu versions, and kernels, so only combinations Intel has tested are offered
- **Compatibility checks** — conflicting or unsupported combinations are flagged before anything is installed

### Base Profiles

| Profile | Name | Description |
|---|---|---|
| `base-standard` | Intel Edge Base Profile | Validated core Intel graphics/media packages, including the platform-enablement DKMS driver, for Ubuntu LTS |

### Add-on Profiles

| Profile | Name | Description |
|---|---|---|
| `ffmpeg` | FFmpeg | FFmpeg multimedia framework with Intel GPU support |
| `gstreamer` | GStreamer | GStreamer multimedia framework with Intel GPU support |
| `npu` | NPU | Enables Intel's Neural Processing Unit (NPU) technology on Edge platforms |
| `manageability` | Manageability | Enables Intel's vPRO Manageability on Edge platforms |


## Supported Platforms

| Ecosystem | Support |
|---|---|
| Platform | Intel Panther Lake (PTL), Intel Wildcat Lake (WCL) |
| Linux Distro | Ubuntu 24.04 (Desktop), Ubuntu 26.04 (Desktop) |
| Kernel | Ubuntu Kernel 7.0+ generic, Ubuntu Kernel 7.0+ real-time (Ubuntu 26.04 only) |

### Supported SKUs

| Platform | Supported SKUs (CPU model) |
|---|---|
| Intel Panther Lake (PTL) | 338H, 358H, 368H, 388H, 325H, 345H, 355H, 375H |
| Intel Wildcat Lake (WCL) | 320, 330, 350 |

## Getting Started

The recommended way to install Edge Pack is through the Edge Pack TUI Installer, a terminal-based wizard that detects your platform and OS, then guides you through profile selection, add-on packages, an installation summary, and live installation progress — without requiring you to hand-craft `apt` commands or repository configuration yourself.

```bash
# 1. Verify the downloaded binary against its published checksum
sha256sum -c edgepack-installer.sha256

# 2. Run the interactive wizard
sudo ./edgepack-installer
```
For detailed installation instructions, please refer to the video tutorial below:

https://github.com/user-attachments/assets/ef562165-f427-434d-a472-1244b3e430f6

## Contribute

Read the [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on submitting issues and pull requests.

## Community and Support

For support, submit your bug report and feature request to [Github Issues](https://github.com/open-edge-platform/edge-pack/issues).

## License Information

Edge Pack is licensed under the [MIT License](LICENSE.txt).
