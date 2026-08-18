# Security

VisionGate controls physical access equipment. Keep it on a trusted private network, retain the door's independent obstruction sensors and safety timeout, and never expose port 8000 directly to the internet. Public access must use **Configure Online Access.bat**, which places Caddy HTTPS in front of the app and trusts proxy headers only from localhost. Forward only TCP ports 80 and 443.

Runtime credentials and personal data belong only in `.env`, `info.md`, and `data/`; these paths are excluded from Git. The dashboard password is stored only as a salted scrypt hash and can be replaced only with `Configure Login.bat` on the VisionGate PC. Before publishing a fork, review the complete commit history for accidentally committed camera or eWeLink credentials.

To report a vulnerability, use the repository's private GitHub security-advisory form when available. Do not include working credentials, camera URLs, device keys, or personal images in a public issue.
