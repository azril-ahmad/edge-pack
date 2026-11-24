<!--hide_directive ```{eval-rst}
.. meta::
   :description: Install Edge Pack using the terminal-based TUI installer wizard.
   :keywords: Edge Pack, TUI installer, terminal user interface, installation
``` hide_directive-->

# Install with the TUI

The TUI (terminal user interface) is the recommended installation method. It is a
terminal-based wizard built with [Textual](https://textual.textualize.io/) that
detects the host platform, Ubuntu release, and kernel before presenting compatible base
profiles and add-ons. No need for hand-crafting `apt` commands or configuring the repository.
To start, follow the steps:

1. Download the package: [Releases](https://github.com/open-edge-platform/edge-pack/releases).
   You can use the [interactive installation selector](../get-started.md) to obtain specific
   release URLs.
2. Run the installer.

   ```bash
   sudo chmod +x ./edgepack-installer
   sudo ./edgepack-installer
   ```

   📌 **Note:** The public installer uses signed repositories at `https://apt.repos.intel.com/edgepack`.

3. Go through a 4-step wizard.

   - Confirm the detected platform and choose a base profile.
   - Select compatible optional add-ons.
   - Review the installation summary and warnings.
   - Confirm the installation and monitor package progress.

   | Keyboard Navigation              | Action                                            |
   |----------------------------------|---------------------------------------------------|
   | `Tab` / `Shift+Tab`              | Move focus between sections                       |
   | `↑` / `↓`                        | Move between items within a list (Step 2 add-ons) |
   | `←` / `→`                        | Move between buttons                              |
   | `Space`                          | Toggle a checkbox / press the focused button      |
   | `Enter`                          | Press the focused button                          |
   | `Ctrl+Q`                         | Quit                                              |



See what the process looks like in the video:
https://github.com/user-attachments/assets/ef562165-f427-434d-a472-1244b3e430f6

A checksum is published with the binary, so you can verify the download integrity:

```bash
sha256sum -c edgepack-installer.sha256
```


## Finding the Logs

All Edge Pack Installer activities is written to:

```text
/var/log/edgepack/edgepack-installer-<timestamp>.log
/var/log/edgepack/edgepack-packages-<timestamp>.log
```

📌 **Note:** `edgepack-installer-<timestamp>.log` consist status of APT installation log while `edgepack-packages-<timestamp>.log` includes list of installed packages by Edge Pack TUI installer. Include these logs when reporting an installation failure, after reviewing it for deployment-specific information.