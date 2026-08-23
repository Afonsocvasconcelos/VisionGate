# Security

VisionGate controls physical access equipment. Retain the door's independent obstruction sensors and safety timeout. The requested public mode exposes direct HTTP on TCP port 83, so network traffic is not encrypted; forward only port 83 and keep a unique application password.

Runtime credentials and personal data belong only in `.env`, `info.md`, `data/`, and local `backups/`; these paths are excluded from Git. The dashboard password is stored only as a salted scrypt hash and can be replaced only with `Configure Login.bat` on the VisionGate PC. Camera passwords and eWeLink device keys are write-only in web responses. Automation graphs and run history must never contain credentials.

Protect the PC and backups: camera credentials, eWeLink keys, and cloud authorization are not encrypted at rest. eWeLink account authorization may be initiated only from `http://127.0.0.1:83`. Before publishing a fork, review the complete commit history—not only the current tree—for credentials or personal media.

To report a vulnerability, use the repository's private GitHub security-advisory form when available. Do not include working credentials, camera URLs, device keys, or personal images in a public issue.
