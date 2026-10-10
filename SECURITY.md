# Security Policy

Please report vulnerabilities privately through GitHub's "Report a
vulnerability" (private security advisory) on this repository rather than a
public issue. Include the version/commit, what you did and what you saw.

Scope notes: the bridge is meant for a trusted home LAN. The web UI is
read-only and off by default; set `ZUI_TOKEN` if you enable it. Do not expose
the bridge's ports to the internet. zapperbridge only relays the unencrypted stream
the M1 outputs; it contains no DRM or decryption code and does not handle keys
or licences, and will not accept contributions that add any.
