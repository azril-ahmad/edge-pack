<!--hide_directive ```{eval-rst}
.. meta::
   :description: Install Edge Pack non-interactively using the installer CLI.
   :keywords: Edge Pack, installer CLI, automation, installation
``` hide_directive-->

# Install with the Installer CLI

The installer CLI is the non-interactive counterpart to the TUI (terminal user interface).
It performs the same host, profile, add-on, repository, and dependency checks, making it
the preferred automation interface when a terminal wizard is unsuitable.

## Verify the Release Download

Download the installer and its published SHA-256 checksum from the latest [GitHub release](https://github.com/open-edge-platform/edge-pack/releases). Always verify the binary before running it:

```bash
sha256sum -c edgepack-installer.sha256
```

📌 **Note:** Do not run the installer when its checksum does not match.

## List Profiles

```bash
./edgepack-installer list
```

## Install Profiles

Pass exactly one base profile followed by zero or more add-ons:

```bash
sudo ./edgepack-installer install base-standard
sudo ./edgepack-installer install base-standard ffmpeg gstreamer
```

Use the [interactive installation selector](../index.md)
to generate a command containing only add-ons supported by the selected Ubuntu
release and platform.

The command exits without changing packages when host validation or profile
compatibility fails. It does not prompt for confirmation, so review the command
before using it in automation.
